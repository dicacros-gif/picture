from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

from blog_topic_history import TopicHistory, TopicHistoryError, normalize_topic, topic_key, title_terms


def confirmed(log_no="223123456789", **extra):
    return {"published": True, "status": "published", "url": f"https://blog.naver.com/example/{log_no}",
            "title": "발행 확인된 글", "article_key": "a" * 64, **extra}


def uncertain(**extra):
    return {"published": False, "status": "uncertain", "url": "", "article_key": "b" * 64,
            "submitted_at": "2026-09-12T20:00:00", **extra}


class TopicHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="topic-history-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "topic-history.json"
        self.history = TopicHistory(self.path)

    def test_normalizes_composition_case_whitespace_and_punctuation(self):
        self.assertEqual(topic_key("휴대폰, 사진 정리!"), topic_key(unicodedata.normalize("NFD", "휴대폰사진-정리")))
        self.assertEqual(topic_key(" USB-C 사용법 "), topic_key("usb c 사용법"))
        self.assertEqual(normalize_topic("\t사진\n  정리  "), "사진 정리")
        self.assertNotEqual(topic_key("C++"), topic_key("C"))

    def test_initial_history_is_empty(self):
        self.assertEqual(self.history.published_topics(), [])
        self.assertEqual(self.history.pending_topics(), [])
        self.assertFalse(self.path.exists())

    def test_only_confirmed_publication_consumes_topic_and_survives_restart(self):
        self.assertTrue(self.history.record_publication("사진 정리", confirmed(), run_dir="D:/owned/run"))
        restarted = TopicHistory(self.path)
        self.assertEqual(restarted.published_topics(), ["사진 정리"])
        self.assertEqual(restarted.filter_keywords(["사진-정리", "사진 정리 방법", "다음 주제"]), ["사진 정리 방법", "다음 주제"])
        record = json.loads(self.path.read_text(encoding="utf-8"))["published"][topic_key("사진 정리")]
        self.assertEqual(record["run_dir"], "D:/owned/run")
        self.assertEqual(record["url"], confirmed()["url"])

    def test_repeated_receipt_is_idempotent(self):
        self.assertTrue(self.history.record_publication("사진 정리", confirmed()))
        initial = self.path.read_bytes()
        self.assertFalse(self.history.record_publication("사진-정리!", confirmed()))
        self.assertEqual(self.path.read_bytes(), initial)
        self.assertEqual(len(self.history.published_topics()), 1)

    def test_consumes_related_keywords_and_detects_similar_titles(self):
        self.history.record_publication("전기요금 절약", confirmed(), keywords=["전기요금 절약 방법", "에어컨 전기요금"],
                                        title="전기요금 절약 방법은 무엇일까요?")
        self.assertEqual(self.history.filter_keywords(["에어컨 전기요금", "새 주제"]), ["새 주제"])
        self.assertTrue(self.history.is_duplicate("다른 표현", ["에어컨 전기요금", "전기요금 절약 방법"], "완전히 다른 제목"))
        self.assertTrue(self.history.is_duplicate("다른 표현", ["새 연관어"], "전기요금 절약 방법"))
        self.assertFalse(self.history.is_duplicate("사진 정리", ["사진 정리 방법"], "사진 정리 순서는 어떻게 정할까요?"))

    def test_version_one_history_is_migrated_in_memory(self):
        self.history.record_publication("기존", confirmed())
        data = json.loads(self.path.read_text(encoding="utf-8")); data["version"] = 1
        for entry in data["published"].values():
            entry.pop("keywords", None); entry.pop("title_terms", None)
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(TopicHistory(self.path).recent_publications()[0]["keywords"], [])
        self.assertTrue(title_terms("전기요금 절약 방법은 무엇일까요"))

    def test_draft_prepared_uncertain_and_failed_results_do_not_consume(self):
        for result in (
            {"published": False, "saved": True, "status": "draft_saved"},
            {"published": False, "status": "prepared"}, uncertain(),
            {"published": False, "status": "failed"}, {}, None,
            confirmed(published="true"), confirmed(status="uncertain"),
        ):
            with self.subTest(result=result):
                self.assertFalse(self.history.record_publication("사진 정리", result))
        self.assertEqual(self.history.published_topics(), [])
        self.assertEqual(self.history.filter_keywords(["사진 정리"]), ["사진 정리"])
        self.assertFalse(self.path.exists())

    def test_false_success_or_non_post_url_is_not_consumed(self):
        for url in ("", "https://blog.naver.com/example", "https://blog.naver.com/PostWriteForm.naver?blogId=example",
                    "https://blog.naver.com.example.org/example/123", "https://example.org/example/123",
                    "javascript:alert(1)", "https://user:pass@blog.naver.com/example/123",
                    "https://blog.naver.com/example/0", "https://blog.naver.com/example/not-a-post"):
            with self.subTest(url=url):
                self.assertFalse(self.history.record_publication("주제", confirmed(url=url)))
        self.assertEqual(self.history.published_topics(), [])

    def test_mobile_postview_url_is_canonicalized(self):
        result = confirmed(url="https://m.blog.naver.com/PostView.naver?blogId=example&logNo=223987654321&tracking=test")
        self.assertTrue(self.history.record_publication("주제", result))
        entry = next(iter(json.loads(self.path.read_text(encoding="utf-8"))["published"].values()))
        self.assertEqual(entry["url"], "https://blog.naver.com/example/223987654321")

    def test_filter_all_realtime_groups_without_mutating_input(self):
        self.history.record_publication("사진 정리", confirmed())
        groups = {"네이버": ["사진정리", "다음 주제"], "구글": ["사진, 정리!", "다른 것"], "다음": []}
        filtered = self.history.filter_groups(groups)
        self.assertEqual(filtered, {"네이버": ["다음 주제"], "구글": ["다른 것"], "다음": []})
        self.assertEqual(groups["네이버"], ["사진정리", "다음 주제"])

    def test_next_ranked_candidate_is_remaining_candidate_across_runs(self):
        from blog_workflow import rank_topics
        groups = {"네이버": ["사진 정리", "옷 정리"], "구글": ["사진 정리", "옷 정리"]}
        related = {"사진 정리": ["사진 정리 방법", "사진 정리 주의"], "옷 정리": ["옷 정리 방법", "옷 정리 주의"]}
        first = rank_topics(groups, related)[0]["topic"]
        self.history.record_publication(first, confirmed())
        next_ranked = rank_topics(TopicHistory(self.path).filter_groups(groups), related)
        self.assertEqual(next_ranked[0]["topic"], "옷 정리")

    def test_uncertain_receipt_is_reserved_separately_without_consumption(self):
        self.assertTrue(self.history.record_uncertain("사진 정리", uncertain(), "D:/run"))
        restarted = TopicHistory(self.path)
        self.assertEqual(restarted.published_topics(), [])
        self.assertEqual(restarted.pending_topics(), ["사진 정리"])
        self.assertEqual(restarted.blocked_topics(), ["사진 정리"])
        self.assertEqual(restarted.filter_keywords(["사진 정리", "다음"]), ["사진 정리", "다음"])
        self.assertEqual(restarted.filter_keywords(["사진-정리", "다음"], include_pending=True), ["다음"])
        self.assertEqual(restarted.filter_groups({"source": ["사진정리", "다음"]}, include_pending=True), {"source": ["다음"]})

    def test_uncertain_without_submission_receipt_is_not_reserved(self):
        for result in (uncertain(article_key=""), uncertain(submitted_at=""), {"published": False, "status": "uncertain"},
                       {"published": False, "status": "failed"}, confirmed()):
            self.assertFalse(self.history.record_uncertain("사진 정리", result))
        self.assertEqual(self.history.pending_topics(), [])

    def test_uncertain_repeated_receipt_does_not_create_duplicate(self):
        self.history.record_uncertain("사진 정리", uncertain())
        before = self.path.read_bytes()
        self.assertFalse(self.history.record_uncertain("사진-정리", uncertain()))
        self.assertEqual(self.path.read_bytes(), before)

    def test_confirmed_receipt_promotes_pending_and_removes_reservation(self):
        self.history.record_uncertain("사진 정리", uncertain())
        self.assertTrue(self.history.record_publication("사진정리", confirmed()))
        self.assertEqual(self.history.pending_topics(), [])
        self.assertEqual(self.history.published_topics(), ["사진정리"])
        self.assertFalse(self.history.record_uncertain("사진 정리", uncertain()))

    def test_legacy_import_only_confirmed_publications(self):
        records = [
            {"topic": "확정", "publication": confirmed(), "draft_only": False},
            {"topic": "확-정", "publication": confirmed()},
            {"topic": "구버전저장", "saved_at": "2026-09-01", "draft_only": False},
            {"topic": "임시", "publication": {"published": False, "saved": True}},
            {"topic": "불확실", "publication": uncertain()},
            {"topic": "모순된임시", "draft_only": True, "publication": confirmed()},
            {"topic": "잘못된주소", "publication": confirmed(url="https://example.org")},
        ]
        self.assertEqual(self.history.import_legacy(records), 1)
        self.assertEqual(self.history.import_legacy(records), 0)
        self.assertEqual(self.history.published_topics(), ["확정"])
        self.assertEqual(self.history.pending_topics(), [])

    def test_invalid_or_empty_keywords_do_not_consume(self):
        for keyword in ("", "  ", "-._!?", None, 5):
            self.assertFalse(self.history.record_publication(keyword, confirmed()))
        self.assertEqual(self.history.filter_keywords(["다음", "다-음", "", None]), ["다음"])

    def test_corrupted_history_fails_closed_without_overwriting_it(self):
        self.path.write_text("corrupt", encoding="utf-8")
        with self.assertRaises(TopicHistoryError):
            self.history.published_topics()
        with self.assertRaises(TopicHistoryError):
            self.history.record_publication("다음", confirmed())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "corrupt")

    def test_atomic_replacement_failure_preserves_previous_history(self):
        self.history.record_publication("첫째", confirmed())
        initial = self.path.read_bytes()
        with patch("blog_topic_history.os.replace", side_effect=OSError("unavailable")), self.assertRaises(TopicHistoryError):
            self.history.record_publication("둘째", confirmed("223000000002"))
        self.assertEqual(self.path.read_bytes(), initial)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertEqual(self.history.published_topics(), ["첫째"])

    def test_concurrent_instances_merge_successful_publications(self):
        def record(index):
            return TopicHistory(self.path).record_publication(f"주제{index}", confirmed(str(223100000000 + index)))
        with ThreadPoolExecutor(max_workers=6) as executor:
            self.assertTrue(all(executor.map(record, range(24))))
        self.assertEqual(len(self.history.published_topics()), 24)

    def test_separate_processes_do_not_lose_history_updates(self):
        script = (
            "import sys; from blog_topic_history import TopicHistory; "
            "history=TopicHistory(sys.argv[1]); prefix=sys.argv[2]; "
            "[history.record_publication(prefix+str(i), {'published':True,'status':'published',"
            "'url':'https://blog.naver.com/example/'+str(223100000000+i)}) for i in range(4)]"
        )
        children = [subprocess.Popen([sys.executable, "-c", script, str(self.path), prefix],
                    cwd=Path(__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for prefix in ("first", "second")]
        for child in children:
            _, error = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, error.decode(errors="replace"))
        self.assertEqual(len(self.history.published_topics()), 8)


if __name__ == "__main__":
    unittest.main()
