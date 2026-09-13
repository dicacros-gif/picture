import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from blog_artifact_cleanup import ArtifactCleanup, CleanupError
from blog_controls import BlogWorkflowControls
from blog_preferences import atomic_json_write
from blog_topic_history import TopicHistory, TopicHistoryError


class ArtifactCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 1000000
        self.log = Mock()
        self.history = TopicHistory(self.root / "published-topic-history.json")
        self.cleanup = ArtifactCleanup(self.root, self.log, self.history, lambda: self.now)
        self.run = self.root / "blog-runs" / "confirmed-run"
        atomic_json_write(self.run / "request.json", {"topic": "한글 주제"})
        self.result = {"published": True, "status": "published", "content_verified": True,
                       "url": "https://blog.naver.com/testowner/123456", "article_key": "a" * 64}
        self.article = {"topic": "한글 주제", "title": "검수한 한글 제목", "run_dir": str(self.run),
                        "publication": self.result, "auxiliary_dirs": []}

    def receipt(self, **changes):
        atomic_json_write(self.root / "publication_receipts" / ("a" * 64 + ".json"), {**self.result, **changes})

    def verified(self):
        self.receipt()
        self.history.record_publication(self.article["topic"], self.result, str(self.run), ["실제 연관어"], self.article["title"])

    def queue(self):
        return json.loads(self.cleanup.path.read_text(encoding="utf-8"))

    def first_target(self):
        return next(iter(self.queue()["entries"].values()))["targets"][0]

    def test_request_is_durable_before_deletion_and_transient_failure_survives_restart(self):
        self.verified()
        self.cleanup.enqueue_publication(self.article)

        def fail_once(path):
            saved = self.first_target()
            self.assertEqual(saved["path"], str(path))
            self.assertEqual(saved["attempts"], 1)
            raise OSError(errno.EBUSY, "owned image still open")

        with patch("blog_artifact_cleanup.shutil.rmtree", side_effect=fail_once) as remove:
            self.cleanup.retry()
            self.cleanup.retry()
            remove.assert_called_once()
        self.assertTrue(self.run.exists())
        self.assertEqual(self.first_target()["status"], "pending")
        self.now += 3601
        restarted = ArtifactCleanup(self.root, self.log, self.history, lambda: self.now)
        restarted.retry()
        self.assertFalse(self.run.exists())
        self.assertEqual(self.first_target()["status"], "done")
        self.assertTrue(self.history.path.exists())
        self.assertTrue((self.root / "publication_receipts" / ("a" * 64 + ".json")).exists())

    def test_already_deleted_run_does_not_prevent_auxiliary_retry(self):
        self.verified()
        auxiliary = self.root / "blog-runs" / "google-search-plan-lookup"
        auxiliary.mkdir()
        self.article["auxiliary_dirs"] = [str(auxiliary)]
        self.cleanup.enqueue_publication(self.article)
        import shutil
        actual = shutil.rmtree

        def fail_auxiliary(path):
            if path == auxiliary:
                raise OSError(errno.EBUSY, "temporary handle")
            actual(path)

        with patch("blog_artifact_cleanup.shutil.rmtree", side_effect=fail_auxiliary):
            self.cleanup.retry()
        self.assertFalse(self.run.exists())
        self.assertTrue(auxiliary.exists())
        self.now += 3601
        self.cleanup.retry()
        self.assertFalse(auxiliary.exists())

    def test_missing_or_false_content_verification_never_enqueues(self):
        self.verified()
        for value in (False, None):
            self.article["publication"] = {**self.result, "content_verified": value}
            with self.assertRaises(CleanupError):
                self.cleanup.enqueue_publication(self.article)
        self.assertFalse(self.cleanup.path.exists())
        self.assertTrue(self.run.exists())

    def test_history_and_receipt_must_independently_match_the_exact_publication(self):
        self.receipt()
        self.history.record_publication(self.article["topic"], {**self.result, "url": "https://blog.naver.com/testowner/987654"}, str(self.run))
        with self.assertRaisesRegex(CleanupError, "발행 이력"):
            self.cleanup.enqueue_publication(self.article)
        self.assertTrue(self.run.exists())

    def test_receipt_changed_after_enqueue_preserves_every_target(self):
        self.verified()
        self.cleanup.enqueue_publication(self.article)
        self.receipt(content_verified=False)
        with patch("blog_artifact_cleanup.shutil.rmtree") as remove:
            self.cleanup.retry()
            remove.assert_not_called()
        self.assertEqual(self.first_target()["attempts"], 0)

    def test_utf8_history_is_readable_and_corruption_is_not_repaired_or_deleted(self):
        self.verified()
        before = self.history.path.read_bytes()
        self.assertIn("한글 주제".encode("utf-8"), before)
        self.assertIn("실제 연관어".encode("utf-8"), before)
        self.cleanup.enqueue_publication(self.article)
        corrupt = b'{"published": "\xff"}'
        self.history.path.write_bytes(corrupt)
        with patch("blog_artifact_cleanup.shutil.rmtree") as remove:
            self.cleanup.retry()
            remove.assert_not_called()
        self.assertEqual(self.history.path.read_bytes(), corrupt)
        self.assertTrue(self.run.exists())

    def test_monitor_historical_policy_denial_is_never_retried_after_log_rotation(self):
        self.verified()
        monitor = self.root / "logs" / "monitor-state.json"
        atomic_json_write(monitor, {"checks": [{"cleanup_blocked_by_policy": True, "cleanup_blocked_run": str(self.run)}]})
        self.cleanup.enqueue_publication(self.article)
        atomic_json_write(monitor, {"checks": []})
        with patch("blog_artifact_cleanup.shutil.rmtree") as remove:
            self.cleanup.retry()
            self.cleanup.enqueue_publication(self.article)
            self.cleanup.retry()
            remove.assert_not_called()
        self.assertEqual(self.first_target()["status"], "blocked")
        self.assertIn(str(self.run), self.queue()["blocked_paths"])

    def test_policy_denial_added_after_enqueue_blocks_run_and_its_auxiliaries(self):
        self.verified()
        auxiliary = self.root / "google-reference-candidates" / "owned-batch"
        auxiliary.mkdir(parents=True)
        self.article["auxiliary_dirs"] = [str(auxiliary)]
        self.cleanup.enqueue_publication(self.article)
        atomic_json_write(self.root / "cleanup-preserve.json", {"cleanup_preserved_paths": [str(self.run)]})
        with patch("blog_artifact_cleanup.shutil.rmtree") as remove:
            self.cleanup.retry()
            remove.assert_not_called()
        self.assertTrue(auxiliary.exists())

    def test_os_access_denial_becomes_permanent_block_not_hourly_retry(self):
        self.verified()
        self.cleanup.enqueue_publication(self.article)
        with patch("blog_artifact_cleanup.shutil.rmtree", side_effect=PermissionError(errno.EACCES, "access denied")) as remove:
            self.cleanup.retry()
            self.now += 7200
            self.cleanup.retry()
            remove.assert_called_once()
        self.assertEqual(self.first_target()["status"], "blocked")

    def test_old_unpublished_and_failed_runs_and_google_photos_are_preserved(self):
        failed = self.root / "blog-runs" / "failed"
        photos = self.root / "google-reference-candidates" / "unpublished"
        orphan_review = self.root / "blog-runs" / "topic-review-orphan"
        orphan_plan = self.root / "blog-runs" / "google-search-plan-orphan"
        atomic_json_write(failed / "manifest.json", {"status": "failed"})
        for path in (self.run, failed, photos, orphan_review, orphan_plan):
            path.mkdir(parents=True, exist_ok=True)
            os.utime(path, (1, 1))
        self.cleanup.collect_orphan_plans()
        self.cleanup.retry()
        for path in (self.run, failed, photos):
            self.assertTrue(path.exists())
        for path in (orphan_review, orphan_plan):
            self.assertFalse(path.exists())

    def test_orphan_scan_never_collects_past_published_runs_from_history(self):
        self.verified()
        os.utime(self.run, (1, 1))
        self.cleanup.collect_orphan_plans()
        self.cleanup.retry()
        self.assertTrue(self.run.exists())
        self.assertFalse(self.cleanup.path.exists())

    def test_failed_run_and_pending_topic_protect_their_search_plans(self):
        failed_plan = self.root / "blog-runs" / "google-search-plan-failed-owner"
        pending_plan = self.root / "blog-runs" / "topic-review-pending-owner"
        for path in (failed_plan, pending_plan):
            path.mkdir()
            os.utime(path, (1, 1))
        atomic_json_write(self.run / "google-search-checkpoint.json", {"status": "failed", "auxiliary_dirs": [str(failed_plan)]})
        atomic_json_write(self.root / "pending-blog-topic.json", {"choice": {"selection_run_dir": str(pending_plan)}})
        self.cleanup.collect_orphan_plans()
        self.cleanup.retry()
        self.assertTrue(failed_plan.exists())
        self.assertTrue(pending_plan.exists())

    def test_auxiliary_referenced_by_another_unpublished_run_is_preserved(self):
        self.verified()
        auxiliary = self.root / "google-reference-candidates" / "shared"
        auxiliary.mkdir(parents=True)
        self.article["auxiliary_dirs"] = [str(auxiliary)]
        atomic_json_write(self.root / "blog-runs" / "failed-other" / "request.json", {"google_candidates": [{"path": str(auxiliary / "photo.jpg")}]})
        self.cleanup.enqueue_publication(self.article)
        self.cleanup.retry()
        self.assertFalse(self.run.exists())
        self.assertTrue(auxiliary.exists())

    def test_malformed_pending_or_retained_manifest_stops_orphan_deletion(self):
        orphan = self.root / "blog-runs" / "topic-review-orphan"
        orphan.mkdir()
        os.utime(orphan, (1, 1))
        atomic_json_write(self.root / "pending-blog-topic.json", {"prepared_article": {"auxiliary_dirs": "invalid"}})
        with self.assertRaises(CleanupError):
            self.cleanup.collect_orphan_plans()
        self.assertTrue(orphan.exists())

    def test_replaced_target_directory_never_inherits_a_cleanup_request(self):
        self.verified()
        self.cleanup.enqueue_publication(self.article)
        self.run.rename(self.run.with_name("preserved-original"))
        atomic_json_write(self.run / "request.json", {"topic": "한글 주제"})
        self.cleanup.retry()
        self.assertTrue(self.run.exists())
        self.assertEqual(self.first_target()["status"], "blocked")

    def test_new_draft_in_the_same_directory_is_preserved_after_cleanup_was_queued(self):
        self.verified()
        self.cleanup.enqueue_publication(self.article)
        atomic_json_write(self.run / "manifest.json", {"status": "preparing", "paragraphs": ["새 미발행 원고"]})
        with patch("blog_artifact_cleanup.shutil.rmtree") as remove:
            self.cleanup.retry()
            remove.assert_not_called()
        self.assertTrue(self.run.exists())
        self.assertIn("원고가 변경", next(iter(self.queue()["entries"].values()))["last_error"])

    def test_restored_google_original_is_queued_from_the_saved_request(self):
        self.verified()
        photo = self.root / "google-reference-candidates" / "saved-batch" / "photo.jpg"
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b"source photo fixture")
        atomic_json_write(self.run / "request.json", {"topic": self.article["topic"], "google_candidates": [{"path": str(photo)}]})
        self.cleanup.enqueue_publication(self.article)
        self.cleanup.retry()
        self.assertFalse(photo.parent.exists())

    def test_cleanup_budget_leaves_remaining_targets_for_the_next_hook(self):
        self.verified()
        auxiliary = self.root / "blog-runs" / "google-search-plan-later"
        auxiliary.mkdir()
        self.article["auxiliary_dirs"] = [str(auxiliary)]
        self.cleanup.enqueue_publication(self.article)
        self.cleanup.retry(max_targets=1)
        self.assertEqual(sum(path.exists() for path in (self.run, auxiliary)), 1)
        self.cleanup.retry(max_targets=1)
        self.assertFalse(self.run.exists())
        self.assertFalse(auxiliary.exists())

    def test_unrelated_run_or_parent_cannot_be_injected_as_auxiliary(self):
        self.verified()
        for raw in (str(self.root), str(self.root / "blog-runs"), str(self.root / "blog-runs" / "another-run"), str(self.root.parent)):
            with self.subTest(raw=raw):
                self.article["auxiliary_dirs"] = [raw]
                with self.assertRaises(CleanupError):
                    self.cleanup.enqueue_publication(self.article)
        self.assertTrue(self.run.exists())

    def test_plan_name_does_not_override_an_article_manifest(self):
        plan = self.root / "blog-runs" / "topic-review-has-article"
        atomic_json_write(plan / "manifest.json", {"status": "failed"})
        with self.assertRaises(CleanupError):
            self.cleanup.enqueue_discarded_plan(str(plan))
        self.assertTrue(plan.exists())

    def test_queue_corruption_and_write_failure_never_trigger_deletion(self):
        self.verified()
        original = b'{"broken": true}'
        self.cleanup.path.write_bytes(original)
        with self.assertRaises(CleanupError):
            self.cleanup.retry()
        self.assertEqual(self.cleanup.path.read_bytes(), original)
        self.cleanup.path.unlink()
        with patch("blog_artifact_cleanup.atomic_json_write", side_effect=OSError("queue disk full")), patch("blog_artifact_cleanup.shutil.rmtree") as remove:
            with self.assertRaises(OSError):
                self.cleanup.enqueue_publication(self.article)
            remove.assert_not_called()
        self.assertTrue(self.run.exists())

    def test_controls_do_not_report_bookkeeping_failure_when_cleanup_is_deferred(self):
        self.verified()
        app = object.__new__(BlogWorkflowControls)
        app.cli_app_dir, app.topic_history, app._naver_log = self.root, self.history, self.log
        with patch("blog_artifact_cleanup.shutil.rmtree", side_effect=OSError(errno.EBUSY, "image handle")):
            app._cleanup_published_artifacts(self.article)
        self.assertTrue(self.run.exists())
        self.assertEqual(self.first_target()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
