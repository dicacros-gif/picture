"""Shared 30-day topic exclusions and atomic reservations for writer accounts."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import copy
import json

from blog_preferences import atomic_json_write
from blog_topic_history import (TopicHistory, TopicHistoryError, topic_key, title_terms,
                                normalize_topic, _confirmed, _uncertain, _published_url)


class TopicReservationConflict(TopicHistoryError):
    """Another account already owns this topic; select another candidate."""


class AccountTopicHistory(TopicHistory):
    def __init__(self, path, owner, *, days=30, clock=None):
        super().__init__(path)
        self.owner = str(owner)
        self.days = days
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reservation_path = self.path.with_name("writer-topic-reservations.json")

    def _recent(self, entry):
        # Unknown dates stay blocked, rather than guessing that they expired.
        try:
            stamp = datetime.fromisoformat(entry.get("submitted_at") or entry["recorded_at"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp > self.clock() - timedelta(days=self.days)
        except (ValueError, KeyError, TypeError):
            return True

    def _reservations(self):
        if not self.reservation_path.exists():
            return {}
        try:
            values = json.loads(self.reservation_path.read_text(encoding="utf-8"))
            if not isinstance(values, dict) or any(
                    not isinstance(owner, str) or not owner.strip() or not isinstance(v, dict)
                    or not isinstance(v.get('topic'), str) or not topic_key(v['topic'])
                    or not isinstance(v.get("keywords"), list)
                    or any(not isinstance(word, str) or not topic_key(word) for word in v['keywords'])
                    for owner, v in values.items()):
                raise ValueError("Invalid topic reservations")
            return values
        except (OSError, ValueError) as exc:
            raise TopicHistoryError("계정 공통 주제 예약을 읽지 못했습니다. 예약 파일을 보존합니다.") from exc

    @staticmethod
    def _keys(entry):
        return {topic_key(v) for v in [entry.get("topic", ""), *entry.get("keywords", [])] if topic_key(v)}

    def _blocked_entries(self, data, *, include_pending=True, reservations=True):
        result = [entry for entry in data["published"].values() if self._recent(entry)]
        if include_pending:
            result.extend(data["pending"].values())  # Uncertain submissions do not expire.
        if reservations:
            result.extend(entry for owner, entry in self._reservations().items() if owner != self.owner)
        return result

    def _excluded_keys(self, include_pending):
        with self._locked():
            return set().union(*(self._keys(entry) for entry in self._blocked_entries(
                self._read(), include_pending=include_pending, reservations=include_pending)))

    def blocked_topics(self):
        with self._locked():
            return list(dict.fromkeys(v for entry in self._blocked_entries(self._read())
                                     for v in [entry.get("topic", ""), *entry.get("keywords", [])] if v))

    def is_duplicate(self, topic, keywords=None, title="", *, keyword_threshold=.4, title_threshold=.5):
        candidate = self._keys({"topic": topic, "keywords": keywords or []})
        terms = set(title_terms(title or topic))
        with self._locked():
            for entry in self._blocked_entries(self._read()):
                # A keyword used by either account is unavailable to both.
                if candidate & self._keys(entry):
                    return True
                old_terms = set(entry.get("title_terms") or title_terms(entry.get("title", "")))
                union = terms | old_terms
                if union and len(terms & old_terms) / len(union) >= float(title_threshold):
                    return True
        return False

    def reserve(self, topic, keywords=None, *, run_dir=""):
        candidate = {"topic": normalize_topic(topic), "keywords": self._filtered(keywords or [], set())}
        wanted = self._keys(candidate)
        if not wanted:
            raise TopicReservationConflict("예약할 주제가 없습니다.")
        with self._locked():
            data = self._read()
            if any(wanted & self._keys(entry) for entry in self._blocked_entries(data)):
                raise TopicReservationConflict(f"'{topic}'은 다른 계정에서 작성 중이거나 최근 30일 안에 사용한 키워드입니다.")
            reservations = self._reservations()
            reservations[self.owner] = {**candidate, "run_dir": str(run_dir), "updated_at": self.clock().isoformat()}
            atomic_json_write(self.reservation_path, reservations)

    def release(self):
        with self._locked():
            reservations = self._reservations()
            if self.owner in reservations:
                del reservations[self.owner]
                atomic_json_write(self.reservation_path, reservations)

    def recent_publications(self, limit=30):
        with self._locked():
            entries = [entry for entry in self._read()['published'].values() if self._recent(entry)]
        entries.sort(key=lambda entry: entry.get('recorded_at', ''), reverse=True)
        return [{'title': entry.get('title', ''), 'topic': entry['topic'], 'keywords': list(entry.get('keywords', []))}
                for entry in entries[:max(0, limit)]]

    def pending_topics(self):
        with self._locked():
            return [entry['topic'] for entry in self._read()['pending'].values()]

    def record_uncertain(self, topic, result, run_dir=''):
        key = topic_key(topic)
        if not key or not _uncertain(result):
            return False
        with self._locked():
            data = self._read()
            if key in data['pending'] or (key in data['published'] and self._recent(data['published'][key])):
                return False
            data['pending'][key] = self._entry(topic, result, run_dir)
            self._write(data)
            return True

    def record_publication(self, topic, result, run_dir="", keywords=None, title=""):
        if not topic_key(topic) or not _confirmed(result):
            return False
        key = topic_key(topic)
        with self._locked():
            data = self._read()
            previous = data["published"].get(key)
            fresh = previous is None or (_published_url(previous.get("url")) != _published_url(result.get("url"))
                                         and not self._recent(previous))
            if fresh:
                if previous:
                    data.setdefault("archive", []).append(copy.deepcopy(previous))
                data["published"][key] = self._entry(topic, result, run_dir, keywords, title)
            else:
                self._enrich_entry(previous, result, run_dir, keywords, title)
            data["pending"].pop(key, None)
            self._write(data)
            reservations = self._reservations()
            reservations.pop(self.owner, None)
            atomic_json_write(self.reservation_path, reservations)
            return fresh
