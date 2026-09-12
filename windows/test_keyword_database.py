from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from keyword_database import consume, load_database, merge, save_database, words


class KeywordDatabaseTests(unittest.TestCase):
    def test_migrates_legacy_prunes_old_and_consumes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keywords.json"
            now = datetime(2026, 9, 12, tzinfo=timezone.utc)
            path.write_text(json.dumps(["기존 문자열", {"keyword": "오래된 값", "first_seen": "2026-01-01T00:00:00+00:00", "last_seen": "2026-01-01T00:00:00+00:00"}], ensure_ascii=False), encoding="utf-8")
            records = load_database(path, now=now)
            self.assertEqual(words(records), ["기존 문자열"])
            records = consume(merge(records, ["새 키워드"], now=now), ["기존 문자열"])
            save_database(path, records)
            self.assertEqual(words(load_database(path, now=now)), ["새 키워드"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)


if __name__ == "__main__": unittest.main()
