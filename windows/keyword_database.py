"""Versioned keyword queue with retention and confirmed-publication consumption."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from blog_topic_history import normalize_topic, topic_key


def _now(value=None):
    return value or datetime.now(timezone.utc)


def load_database(path: Path, now=None, max_age_days=30, limit=500):
    path, now = Path(path), _now(now)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError, TypeError):
        raw = []
    if isinstance(raw, dict):
        raw = raw.get("keywords", [])
    records = {}
    for item in raw if isinstance(raw, list) else []:
        item = {"keyword": item} if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        word, key = normalize_topic(item.get("keyword", "")), topic_key(item.get("keyword", ""))
        try:
            last = datetime.fromisoformat(str(item.get("last_seen", "")).replace("Z", "+00:00"))
            if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
        except ValueError:
            last = now
        if key and last >= now - timedelta(days=max_age_days):
            records[key] = {"keyword": word, "first_seen": item.get("first_seen") or now.isoformat(), "last_seen": last.isoformat()}
    return dict(sorted(records.items(), key=lambda pair: pair[1]["last_seen"])[-limit:])


def merge(records, values, now=None, limit=500):
    now = _now(now).isoformat()
    result = dict(records)
    for value in values or []:
        word, key = normalize_topic(value), topic_key(value)
        if key:
            result[key] = {"keyword": word, "first_seen": result.get(key, {}).get("first_seen", now), "last_seen": now}
    return dict(list(result.items())[-limit:])


def consume(records, values):
    removed = {topic_key(value) for value in values or []}
    return {key: item for key, item in records.items() if key not in removed}


def save_database(path: Path, records):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 2, "keywords": list(records.values())}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def words(records):
    return [item["keyword"] for item in records.values()]
