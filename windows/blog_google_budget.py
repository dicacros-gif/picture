"""A shared, non-waiting budget for optional Google image browser work."""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from blog_preferences import atomic_json_write


class GoogleImageChallengeError(RuntimeError):
    """Google requested human verification; stop requests instead of retrying."""


@dataclass(frozen=True)
class SearchReservation:
    allowed: bool
    reason: str = ""
    retry_after: float = 0
    token: str = ""


class _SharedBudget:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = ""
        self.state = None
        self.load_error = ""


_registry_lock = threading.Lock()
_registry: dict[str, _SharedBudget] = {}


class GoogleSearchBudget:
    """Accounts share one request slot; skipped work never delays an article.

    The lock covers only small state updates. No browser, CLI or image generation
    operation runs while it is held. CAPTCHA cooldown and previous attempts also
    survive application restarts. A reservation must be released in ``finally``.
    """
    MIN_SPACING_SECONDS = 60
    CHALLENGE_COOLDOWN_SECONDS = 3600
    ARTICLE_RETENTION_SECONDS = 7 * 86400

    def __init__(self, root, *, clock=None):
        self.root = Path(root).resolve()
        self.path = self.root / "google-image-budget.json"
        self.clock = clock or time.time
        self.persistence_error = ""
        with _registry_lock:
            self.shared = _registry.setdefault(str(self.root).casefold(), _SharedBudget())

    @staticmethod
    def _timestamp(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    def _state(self):
        if self.shared.state is not None:
            return self.shared.state
        state = {"version": 1, "last_request_at": 0, "cooldown_until": 0, "articles": {}}
        try:
            if self.path.exists():
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if (not isinstance(value, dict) or value.get("version") != 1
                        or not self._timestamp(value.get("last_request_at"))
                        or not self._timestamp(value.get("cooldown_until"))
                        or not isinstance(value.get("articles"), dict)):
                    raise ValueError("invalid Google image budget")
                for record in value["articles"].values():
                    if (not isinstance(record, dict) or not self._timestamp(record.get("at"))
                            or type(record.get("count")) is not int or record["count"] < 0):
                        raise ValueError("invalid Google image article budget")
                state = value
        except (OSError, ValueError, TypeError) as exc:
            # A broken receipt must not silently reset the request counter.
            self.shared.load_error = str(exc)
        self.shared.state = state
        return state

    def _save(self, state):
        try:
            atomic_json_write(self.path, state)
            return True
        except OSError as exc:
            self.persistence_error = str(exc)
            return False

    def reserve(self, article_id, *, max_searches=1):
        """Return immediately with a reservation or a reason to omit Google."""
        if not self.shared.lock.acquire(blocking=False):
            return SearchReservation(False, "busy")
        try:
            now = self.clock()
            state = self._state()
            if self.shared.load_error:
                return SearchReservation(False, "state_unavailable")
            if state["cooldown_until"] > now:
                return SearchReservation(False, "challenge_cooldown", state["cooldown_until"] - now)
            if self.shared.active:
                return SearchReservation(False, "busy")
            article = hashlib.sha256(str(article_id).encode("utf-8")).hexdigest()
            entries = {key: value for key, value in state["articles"].items()
                       if now - value["at"] < self.ARTICLE_RETENTION_SECONDS}
            if entries.get(article, {}).get("count", 0) >= max(1, int(max_searches)):
                return SearchReservation(False, "article_limit")
            remaining = state["last_request_at"] + self.MIN_SPACING_SECONDS - now
            if state["articles"] and remaining > 0:
                return SearchReservation(False, "spacing", remaining)
            entries[article] = {"at": now, "count": entries.get(article, {}).get("count", 0) + 1}
            entries = dict(sorted(entries.items(), key=lambda item: item[1]["at"], reverse=True)[:1024])
            changed = {**state, "last_request_at": now, "articles": entries}
            if not self._save(changed):
                return SearchReservation(False, "state_unavailable")
            self.shared.state = changed
            self.shared.active = uuid.uuid4().hex
            return SearchReservation(True, token=self.shared.active)
        finally:
            self.shared.lock.release()

    def release(self, reservation):
        with self.shared.lock:
            if reservation.token and self.shared.active == reservation.token:
                # The planner and visual shortlist may take time. Space the next
                # session from this browser work's end, not its reservation time.
                state = self._state()
                changed = {**state, "last_request_at": max(state["last_request_at"], self.clock())}
                self.shared.state = changed
                self._save(changed)
                self.shared.active = ""

    def record_challenge(self):
        """Persist a shared cooldown without solving or bypassing the challenge."""
        with self.shared.lock:
            state = self._state()
            changed = {**state, "cooldown_until": max(state["cooldown_until"],
                       self.clock() + self.CHALLENGE_COOLDOWN_SECONDS)}
            self.shared.state = changed
            return self._save(changed)
