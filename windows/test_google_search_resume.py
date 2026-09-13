import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from blog_controls import BlogWorkflowControls
from blog_preferences import atomic_json_write
from blog_workflow import WorkflowError


class GoogleSearchResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        clock_patch = patch('blog_google_budget.time.time', return_value=1000.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        precheck = patch("blog_controls._GoogleSearchJob._precheck", side_effect=lambda planner, items, number: items)
        precheck.start()
        self.addCleanup(precheck.stop)
        self.run = self.root / "blog-runs" / "same-article"
        self.app = object.__new__(BlogWorkflowControls)
        self.app.cli_app_dir = self.root
        self.app.cli_bridge = Mock()
        self.app.events = Mock()
        self.app.full_auto_stop = threading.Event()
        self.app._naver_log = Mock()
        self.app._ensure_topic_allowed = Mock()
        self.app._preflight_cli_accounts = Mock()
        self.app.naver_bot = Mock()
        self.app.naver_bot.stop_event = threading.Event()
        self.app.naver_bot.capture_google_reference_candidates.return_value = []
        self.config = {"steps": ["chatgpt"], "models": {}, "include_google": True,
                       "base_prompt": "사용자 지침", "review_mode": "단계별 교차 검수",
                       "blog_id": "owner", "google_reference_count": 4}
        mocked = patch("blog_controls.BlogWorkflow")
        self.workflow = mocked.start().return_value
        self.addCleanup(mocked.stop)
        self.workflow.plan_google_image_search.return_value = {
            "query": "electricity meter photo", "queries": ["power socket photo", "solar panel photo"]}
        self.workflow.prepare.side_effect = self.prepare
        self.fail_article = False

    def prepare(self, topic, keywords, *args, **kwargs):
        run = Path(kwargs.get("resume_run_dir", self.run))
        atomic_json_write(run / "request.json", {"topic": topic, "keywords": keywords,
                                                "google_candidates": kwargs["google_candidates"]})
        atomic_json_write(run / "manifest.json", {"status": "preparing"})
        kwargs["on_run_created"](str(run))
        candidates = kwargs["resolve_google_candidates"]()
        atomic_json_write(run / "request.json", {"topic": topic, "keywords": keywords, "google_candidates": candidates})
        if self.fail_article:
            raise WorkflowError("cover image rejected", run)
        return {"topic": topic, "run_dir": str(run)}

    def prepare_article(self, topic="전기요금", keywords=None, config=None):
        return self.app._prepare_cli_worker(topic, keywords or ["전기요금 절약"], config or self.config)

    def resume_config(self, **changes):
        return {**copy.deepcopy(self.config), "resume_run_dir": str(self.run), **changes}

    def reset_capture_calls(self):
        self.workflow.plan_google_image_search.reset_mock()
        self.app.naver_bot.capture_google_reference_candidates.reset_mock()

    def test_completed_empty_search_survives_later_article_failure_and_resume(self):
        self.fail_article = True
        with self.assertRaisesRegex(WorkflowError, "cover image rejected"):
            self.prepare_article()
        receipt = json.loads((self.run / "google-search-checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["candidate_count"], 0)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(len(receipt["queries"]), 1)
        self.fail_article = False
        self.reset_capture_calls()
        self.prepare_article(config=self.resume_config())
        self.workflow.plan_google_image_search.assert_not_called()
        self.app.naver_bot.capture_google_reference_candidates.assert_not_called()
        self.assertEqual(self.workflow.prepare.call_args.kwargs["google_candidates"], [])

    def test_changed_settings_do_not_repeat_google_for_the_same_article(self):
        self.prepare_article()
        original_receipt = (self.run / "google-search-checkpoint.json").read_bytes()
        variations = [
            ("전기요금 인상", ["전기요금 절약"], {}),
            ("전기요금", ["전기요금 계산"], {}),
            ("전기요금", ["전기요금 절약"], {"google_reference_count": 6}),
            ("전기요금", ["전기요금 절약"], {"models": {"chatgpt": "other-model"}}),
            ("전기요금", ["전기요금 절약"], {"steps": ["antigravity"]}),
            ("전기요금", ["전기요금 절약"], {"stage_configs": [{"provider": "chatgpt", "model": "new"}]}),
        ]
        for topic, keywords, changes in variations:
            with self.subTest(topic=topic, keywords=keywords, changes=changes):
                (self.run / "google-search-checkpoint.json").write_bytes(original_receipt)
                self.reset_capture_calls()
                self.prepare_article(topic, keywords, self.resume_config(**changes))
                self.workflow.plan_google_image_search.assert_not_called()
                self.app.naver_bot.capture_google_reference_candidates.assert_not_called()

    def test_new_run_cannot_inherit_another_runs_completed_search(self):
        self.prepare_article()
        next_run = self.root / "blog-runs" / "next-article"
        next_run.mkdir()
        for filename in ("google-search-checkpoint.json", "request.json"):
            (next_run / filename).write_bytes((self.run / filename).read_bytes())
        self.reset_capture_calls()
        self.clock.return_value += 61
        self.prepare_article(config=self.resume_config(resume_run_dir=str(next_run)))
        self.workflow.plan_google_image_search.assert_called_once()

    def test_query_exception_finishes_optional_work_without_searching_again(self):
        self.app.naver_bot.capture_google_reference_candidates.side_effect = RuntimeError("navigation failed")
        self.prepare_article()
        receipt = json.loads((self.run / "google-search-checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["skip_reason"], "capture_failed")
        self.app.naver_bot.capture_google_reference_candidates.side_effect = None
        self.reset_capture_calls()
        self.prepare_article(config=self.resume_config())
        self.workflow.plan_google_image_search.assert_not_called()
        self.assertTrue((self.run / "google-search-checkpoint.json").is_file())

    def test_planner_failure_does_not_cache_a_completed_search(self):
        self.workflow.plan_google_image_search.side_effect = RuntimeError("CLI failed")
        self.prepare_article()
        self.app.naver_bot.capture_google_reference_candidates.assert_not_called()
        self.assertNotEqual(json.loads((self.run / "google-search-checkpoint.json").read_text(encoding="utf-8"))["status"], "completed")

    def test_cancel_during_capture_never_saves_completed_search_or_returns_article(self):
        def cancel(*args, **kwargs):
            self.app.full_auto_stop.set()
            raise RuntimeError("cancelled")
        self.app.naver_bot.capture_google_reference_candidates.side_effect = cancel
        with self.assertRaisesRegex(WorkflowError, "중지"):
            self.prepare_article()
        self.workflow.prepare.assert_called_once()
        self.assertNotEqual(json.loads((self.run / "google-search-checkpoint.json").read_text(encoding="utf-8"))["status"], "completed")

    def test_corrupt_receipt_does_not_override_persisted_request_limit(self):
        self.prepare_article()
        (self.run / "google-search-checkpoint.json").write_text("{invalid", encoding="utf-8")
        self.reset_capture_calls()
        self.prepare_article(config=self.resume_config())
        self.workflow.plan_google_image_search.assert_not_called()

    def test_cached_positive_candidate_still_requires_english_evidence_and_matching_hash(self):
        image = self.root / "google-reference-candidates" / "original" / "photo.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"captured photo")
        candidate = {"path": str(image), "english_source_verified": True, "license_verified": True,
                     "capture_sha256": hashlib.sha256(image.read_bytes()).hexdigest()}
        self.app.naver_bot.capture_google_reference_candidates.return_value = [candidate]
        self.prepare_article()
        self.assertTrue((self.run / "google-search-checkpoint.json").exists())
        self.reset_capture_calls()
        self.prepare_article(config=self.resume_config())
        self.workflow.plan_google_image_search.assert_not_called()
        self.assertEqual(self.workflow.prepare.call_args.kwargs["google_candidates"], [candidate])
        image.write_bytes(b"different image")
        self.app.naver_bot.capture_google_reference_candidates.return_value = []
        self.prepare_article(config=self.resume_config())
        self.workflow.plan_google_image_search.assert_not_called()
        self.assertEqual(self.workflow.prepare.call_args.kwargs["google_candidates"], [])

    def test_disabling_google_does_not_search_or_write_a_completion_receipt(self):
        self.prepare_article(config={**self.config, "include_google": False})
        self.workflow.plan_google_image_search.assert_not_called()
        self.assertFalse((self.run / "google-search-checkpoint.json").exists())


if __name__ == "__main__":
    unittest.main()
