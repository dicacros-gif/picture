"""Persistent keyword consumption based only on confirmed browser publication.

Drafts, editor input and failed runs never consume a topic. An uncertain browser
submission can be reserved separately, so automation can avoid attempting the
same topic again until its browser receipt has been reconciled.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

from blog_preferences import atomic_json_write


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_EMPTY = {"version": 2, "published": {}, "pending": {}}
_TITLE_STOP = frozenset(("은", "는", "이", "가", "을", "를", "의", "에", "에서", "로", "으로", "와", "과", "도", "만", "부터", "까지", "에게", "처럼", "보다", "하고", "하는", "하면", "할", "수", "있는", "있을", "무엇", "왜", "어떻게"))


class TopicHistoryError(RuntimeError):
    pass


def normalize_topic(value: str) -> str:
    """NFC display text with collapsed whitespace; preserve readable punctuation."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def topic_key(value: str) -> str:
    """Match case, Unicode composition, spacing and punctuation variants.

    Symbols such as the + in C++ remain significant; they are not punctuation.
    A published parent topic does not consume distinct, longer related queries.
    """
    return "".join(character for character in normalize_topic(value).casefold()
                   if not character.isspace() and unicodedata.category(character)[0] not in {"P", "Z"})


def title_terms(value) -> list[str]:
    """Return stable title terms with punctuation and common Korean function words removed."""
    if isinstance(value, (list, tuple, set)):
        tokens = [normalize_topic(item).casefold() for item in value]
    else:
        tokens = re.findall(r"[0-9A-Za-z가-힣]+", normalize_topic(value).casefold())
    cleaned = []
    for token in tokens:
        if token.startswith(("무엇", "어떻게")):
            continue
        for suffix in ("에서", "으로", "부터", "까지", "에게", "처럼", "보다", "은", "는", "이", "가", "을", "를", "의", "에", "도", "만"):
            if len(token) > len(suffix) + 1 and token.endswith(suffix):
                token = token[:-len(suffix)]
                break
        if len(token) >= 2 and token not in _TITLE_STOP:
            cleaned.append(token)
    return list(dict.fromkeys(cleaned))


def _published_url(value) -> str:
    """Accept a Naver post address, never the blog home, editor or arbitrary host."""
    if not isinstance(value, str) or len(value) > 3000:
        return ""
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme not in {"https", "http"} or parsed.hostname not in {"blog.naver.com", "m.blog.naver.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            return ""
        path = unquote(parsed.path).strip("/")
        match = re.fullmatch(r"([A-Za-z0-9_-]+)/(\d+)", path)
        if match:
            blog_id, log_no = match.groups()
        elif path.casefold() == "postview.naver":
            query = {key.casefold(): values for key, values in parse_qs(parsed.query).items()}
            blog_id = (query.get("blogid") or [""])[0]
            log_no = (query.get("logno") or [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", blog_id) or not re.fullmatch(r"\d+", log_no):
                return ""
        else:
            return ""
        if not log_no.strip("0"):
            return ""
        return f"https://blog.naver.com/{blog_id}/{log_no}"
    except (ValueError, TypeError):
        return ""


def _confirmed(result) -> bool:
    return (isinstance(result, dict) and result.get("published") is True
            and result.get("status") == "published" and bool(_published_url(result.get("url"))))


def _uncertain(result) -> bool:
    return (isinstance(result, dict) and result.get("published") is False
            and result.get("status") == "uncertain"
            and isinstance(result.get("article_key"), str)
            and bool(re.fullmatch(r"[A-Za-z0-9_-]{8,160}", result["article_key"]))
            and isinstance(result.get("submitted_at"), str) and bool(result["submitted_at"].strip()))


class TopicHistory:
    """Thread/process-safe atomic keyword history independent of legacy run logs."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        with _PATH_LOCKS_GUARD:
            self._thread_lock = _PATH_LOCKS.setdefault(os.path.normcase(str(self.path)), threading.RLock())

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with self._thread_lock:
            try:
                stream = lock_path.open("a+b")
            except OSError as exc:
                raise TopicHistoryError("발행 주제 이력을 잠글 수 없어 중복 발행 방지를 위해 중단했습니다.") from exc
            acquired = False
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                started = time.monotonic()
                while True:
                    stream.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except OSError as exc:
                        if time.monotonic() - started > 8:
                            raise TopicHistoryError("다른 실행에서 발행 주제 이력을 저장 중입니다. 중복 실행을 확인하세요.") from exc
                        time.sleep(0.025)
                yield
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 2, "published": {}, "pending": {}}
        try:
            if self.path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("History is too large")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") not in {1, 2}
                    or not isinstance(data.get("published"), dict) or not isinstance(data.get("pending"), dict)):
                raise ValueError("Unsupported history format")
            for state in ("published", "pending"):
                for key, entry in data[state].items():
                    if not isinstance(entry, dict) or not key or key != topic_key(entry.get("topic", "")):
                        raise ValueError("Invalid keyword entry")
                    if state == "published" and not _published_url(entry.get("url")):
                        raise ValueError("Missing confirmed post URL")
            if data.get("version") == 1:
                for entry in data["published"].values():
                    entry["keywords"] = []
                    entry["title_terms"] = title_terms(entry.get("title", ""))
                data["version"] = 2
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise TopicHistoryError("발행 주제 이력을 읽을 수 없어 중복 발행 방지를 위해 중단했습니다. 이력 파일을 확인하세요.") from exc

    def _write(self, data: dict) -> None:
        try:
            atomic_json_write(self.path, data)
        except OSError as exc:
            raise TopicHistoryError("발행 주제 이력을 저장하지 못했습니다. 다음 자동 발행 전에 저장 경로를 확인하세요.") from exc

    @staticmethod
    def _entry(topic: str, result: dict, run_dir: str = "", keywords=None, title="") -> dict:
        clean_keywords = TopicHistory._filtered(keywords or [], set())
        resolved_title = normalize_topic(title or result.get("title", ""))[:500]
        return {"topic": normalize_topic(topic), "url": _published_url(result.get("url")),
                "keywords": clean_keywords, "title": resolved_title, "title_terms": title_terms(resolved_title),
                "article_key": str(result.get("article_key", ""))[:160],
                "submitted_at": str(result.get("submitted_at", ""))[:100],
                "recorded_at": datetime.now(timezone.utc).isoformat(), "run_dir": str(run_dir)[:2000]}

    def published_topics(self) -> list[str]:
        with self._locked():
            return [entry["topic"] for entry in self._read()["published"].values()]

    def pending_topics(self) -> list[str]:
        with self._locked():
            data = self._read()
            return [entry["topic"] for key, entry in data["pending"].items() if key not in data["published"]]

    def blocked_topics(self) -> list[str]:
        """Topics automation must skip: published plus unresolved submissions."""
        with self._locked():
            data = self._read()
            combined = {**data["pending"], **data["published"]}
            return [entry["topic"] for entry in combined.values()]

    def record_publication(self, topic: str, result: dict, run_dir: str = "", keywords=None, title="") -> bool:
        """Return True only when a new keyword is consumed; repeat receipts are idempotent."""
        key = topic_key(topic)
        if not key or not _confirmed(result):
            return False
        with self._locked():
            data = self._read()
            new = key not in data["published"]
            changed = new or key in data["pending"]
            if new:
                data["published"][key] = self._entry(topic, result, run_dir, keywords, title)
            else:
                changed = self._enrich_entry(data["published"][key], result, run_dir, keywords, title) or changed
            data["pending"].pop(key, None)
            if changed:
                self._write(data)
            return new

    @classmethod
    def _enrich_entry(cls, entry, result, run_dir="", keywords=None, title=""):
        """Recover missing metadata only for the same confirmed publication URL."""
        if _published_url(entry.get("url")) != _published_url(result.get("url")):
            return False
        changed = False
        previous = cls._filtered(entry.get("keywords", []), set())
        combined = cls._filtered([*previous, *cls._filtered(keywords or [], set())], set())
        if combined != previous:
            entry["keywords"] = combined
            changed = True
        supplied_title = normalize_topic(title or result.get("title", ""))[:500]
        if not entry.get("title") and supplied_title:
            entry["title"], entry["title_terms"] = supplied_title, title_terms(supplied_title)
            changed = True
        if not entry.get("run_dir") and run_dir:
            entry["run_dir"] = str(run_dir)[:2000]
            changed = True
        return changed

    def record_uncertain(self, topic: str, result: dict, run_dir: str = "") -> bool:
        """Reserve only an actual browser submission receipt; do not consume its topic."""
        key = topic_key(topic)
        if not key or not _uncertain(result):
            return False
        with self._locked():
            data = self._read()
            if key in data["published"] or key in data["pending"]:
                return False
            data["pending"][key] = self._entry(topic, result, run_dir)
            self._write(data)
            return True

    def import_legacy(self, records) -> int:
        """Import only legacy runs with a confirmed publication receipt, never draft logs."""
        if not isinstance(records, (list, tuple)):
            return 0
        candidates = [record for record in records if isinstance(record, dict)
                      and record.get("draft_only") is not True and topic_key(record.get("topic", ""))
                      and _confirmed(record.get("publication"))]
        if not candidates:
            return 0
        imported = 0
        with self._locked():
            data = self._read()
            changed = False
            for record in candidates:
                key = topic_key(record["topic"])
                keywords = self._filtered([record.get("source_topic", ""),
                                          *self._filtered(record.get("keywords", []), set())], set())
                if key not in data["published"]:
                    data["published"][key] = self._entry(record["topic"], record["publication"], record.get("run_dir", ""),
                                                         keywords, record.get("title", ""))
                    imported += 1
                    changed = True
                else:
                    changed = self._enrich_entry(data["published"][key], record["publication"], record.get("run_dir", ""),
                                                keywords, record.get("title", "")) or changed
                if key in data["pending"]:
                    del data["pending"][key]
                    changed = True
            if changed:
                self._write(data)
        return imported

    def _excluded_keys(self, include_pending: bool) -> set[str]:
        with self._locked():
            data = self._read()
            excluded = set(data["published"])
            for entry in data["published"].values():
                excluded.update(topic_key(item) for item in entry.get("keywords", []) if topic_key(item))
            return excluded | (set(data["pending"]) if include_pending else set())

    def recent_publications(self, limit: int = 30) -> list[dict]:
        with self._locked():
            entries = list(self._read()["published"].values())
        entries.sort(key=lambda item: item.get("recorded_at", ""), reverse=True)
        return [{"title": item.get("title", ""), "topic": item.get("topic", ""),
                 "keywords": list(item.get("keywords", []))} for item in entries[:max(0, limit)]]

    def is_duplicate(self, topic: str, keywords=None, title="", *, keyword_threshold=.4, title_threshold=.5) -> bool:
        candidate_keywords = {topic_key(item) for item in [topic, *(keywords or [])] if topic_key(item)}
        candidate_title = set(title_terms(title or topic))
        with self._locked():
            entries = self._read()["published"].values()
            for entry in entries:
                old_keywords = {topic_key(item) for item in [entry.get("topic", ""), *entry.get("keywords", [])] if topic_key(item)}
                overlap = len(candidate_keywords & old_keywords) / max(1, min(len(candidate_keywords), len(old_keywords)))
                old_title = set(entry.get("title_terms") or title_terms(entry.get("title", "")))
                union = candidate_title | old_title
                similarity = len(candidate_title & old_title) / len(union) if union else 0
                if overlap >= float(keyword_threshold) or similarity >= float(title_threshold):
                    return True
        return False

    @staticmethod
    def _filtered(values, excluded: set[str]) -> list[str]:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            return []
        remaining, seen = [], set()
        for value in values:
            key = topic_key(value)
            if key and key not in excluded and key not in seen:
                seen.add(key)
                remaining.append(normalize_topic(value))
        return remaining

    def filter_keywords(self, values: list[str], *, include_pending: bool = False) -> list[str]:
        """Return an unconsumed copy of a keyword queue; never mutate the caller's list."""
        return self._filtered(values, self._excluded_keys(include_pending))

    def filter_groups(self, groups: dict, *, include_pending: bool = False) -> dict:
        """Filter every realtime source with one consistent history snapshot."""
        if not isinstance(groups, dict):
            return {}
        excluded = self._excluded_keys(include_pending)
        return {source: self._filtered(values, excluded) for source, values in groups.items()}
