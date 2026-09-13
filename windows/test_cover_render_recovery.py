import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import blog_workflow
import test_blog_workflow as support
from blog_workflow import COVER_RENDER_VERSION, WorkflowError


class CoverRenderRecoveryTests(unittest.TestCase):
    prepare = support.BlogWorkflowTests.prepare
    latest_manifest = support.BlogWorkflowTests.latest_manifest
    assert_blocked = support.BlogWorkflowTests.assert_blocked

    def setUp(self):
        support.BlogWorkflowTests.setUp(self)
        self.render_version = "legacy-outline"
        original_export = blog_workflow.clean_export.side_effect

        def export(*args, **kwargs):
            result = original_export(*args, **kwargs)
            result["cover_render_version"] = self.render_version if kwargs.get("headline") else ""
            return result

        blog_workflow.clean_export.side_effect = export

    def failed_cover(self):
        failed = self.assert_blocked("첫 사진", steps=["chatgpt"], image_retry_limit=2)
        self.assertEqual(len(self.bridge.generations), 10)
        self.assertEqual(failed["image_generation_attempts"]["0"], 3)
        return failed

    def test_resume_reexports_same_original_and_locally_checks_new_file_without_generation(self):
        failed = self.failed_cover()
        before = copy.deepcopy(failed["image_candidates"])
        cover = before[0]
        original = Path(cover["original_path"]).read_bytes()
        previous_upload = Path(cover["path"]).read_bytes()
        calls_before = len(self.bridge.calls)
        self.render_version = COVER_RENDER_VERSION
        self.bridge.image_callback = None

        result = self.workflow.resume(failed["run_dir"])

        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(self.bridge.generations), 10)
        self.assertEqual(result["image_generation_attempts"], failed["image_generation_attempts"])
        new_cover = result["image_candidates"][0]
        self.assertEqual(new_cover["generation_attempts"], 3)
        self.assertEqual(new_cover["cover_render_version"], COVER_RENDER_VERSION)
        self.assertNotEqual(new_cover["path"], cover["path"])
        self.assertEqual(new_cover["original_path"], cover["original_path"])
        self.assertIn(new_cover["previous_cover_renders"][0]["reviews"], (None, []))
        self.assertEqual(Path(cover["original_path"]).read_bytes(), original)
        self.assertEqual(Path(cover["path"]).read_bytes(), previous_upload)
        image_calls = [call for call in self.bridge.calls[calls_before:] if call["images"]]
        self.assertEqual(image_calls, [])
        self.assertTrue(new_cover["local_file_validated"])
        self.assertEqual(new_cover["reviews"], [])
        self.assertEqual(result["image_candidates"][1:], before[1:])

    def test_current_renderer_is_locally_accepted_without_visual_calls(self):
        self.render_version = COVER_RENDER_VERSION
        result = self.prepare(steps=["chatgpt"], image_retry_limit=2)
        self.assertTrue(result["images"][0]["local_file_validated"])
        self.assertFalse([call for call in self.bridge.calls if call["images"]])
        self.assertEqual(len(self.bridge.generations), 8)

    def test_recovery_never_uses_unverified_or_outside_original(self):
        failed = self.failed_cover()
        article = json.loads(Path(failed["run_dir"], "article.json").read_text(encoding="utf-8"))
        cover = failed["image_candidates"][0]
        outside = self.root / "outside.png"
        outside.write_bytes(Path(cover["original_path"]).read_bytes())
        variants = [
            {"original_path": str(outside)},
            {"original_pixel_hash": "0" * 64},
            {"original_dhash": "0" * 16},
            {"sha256": "0" * 64},
            {"image_context_sha256": "0" * 64},
        ]
        for changes in variants:
            with self.subTest(changes=changes), patch("blog_workflow.clean_export") as export:
                candidate = {**copy.deepcopy(cover), **changes}
                refreshed = self.workflow._refresh_rejected_cover(Path(failed["run_dir"]), article, candidate)
                self.assertIs(refreshed, candidate)
                export.assert_not_called()

    def test_approved_cover_and_body_images_are_preserved(self):
        self.render_version = COVER_RENDER_VERSION
        result = self.prepare(steps=["chatgpt"], image_retry_limit=2)
        article = json.loads(Path(result["run_dir"], "article.json").read_text(encoding="utf-8"))
        for candidate in result["image_candidates"]:
            with self.subTest(index=candidate["paragraph_index"]), patch("blog_workflow.clean_export") as export:
                self.assertIs(self.workflow._refresh_rejected_cover(Path(result["run_dir"]), article, candidate), candidate)
                export.assert_not_called()

    def test_export_failure_preserves_rejected_file_and_its_review(self):
        failed = self.failed_cover()
        article = json.loads(Path(failed["run_dir"], "article.json").read_text(encoding="utf-8"))
        candidate = failed["image_candidates"][0]
        unchanged = copy.deepcopy(candidate)
        data = Path(candidate["path"]).read_bytes()
        with patch("blog_workflow.clean_export", side_effect=OSError("disk full")):
            refreshed = self.workflow._refresh_rejected_cover(Path(failed["run_dir"]), article, candidate)
        self.assertIs(refreshed, candidate)
        self.assertEqual(candidate, unchanged)
        self.assertEqual(Path(candidate["path"]).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
