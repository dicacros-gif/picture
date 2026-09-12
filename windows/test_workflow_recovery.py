import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_blog_workflow as support
from blog_cli_bridge import BlogCliError
from blog_workflow import BlogWorkflow, REVIEW_MODES, WorkflowError


class WorkflowRecoveryTests(unittest.TestCase):
    setUp = support.BlogWorkflowTests.setUp
    prepare = support.BlogWorkflowTests.prepare
    latest_manifest = support.BlogWorkflowTests.latest_manifest
    assert_blocked = support.BlogWorkflowTests.assert_blocked

    def configured_recovery(self):
        original = self.bridge.run_text
        denied = []
        def run(provider, prompt, **kwargs):
            if provider == "antigravity" and not kwargs.get("images"):
                denied.append(prompt)
                raise BlogCliError("permission_required", "command denied", provider=provider)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        stages = [{"provider": "chatgpt", "role": "작성", "model": "writer"},
                  {"provider": "antigravity", "role": "팩트·최신 정보 보강", "model": "facts"}]
        return stages, denied

    def test_rejected_editorial_audit_stops_before_any_image_generation(self):
        sentence = "이 선택은 가능합니다."
        self.bridge.article["paragraphs"][0] += "\n" + sentence
        original = self.bridge.run_text
        def run(provider, prompt, **kwargs):
            if prompt.startswith("EDITORIAL_TARGETED_REPAIR"):
                return json.dumps({"paragraph_patches": [{"index": 0, "old": sentence, "new": "이 선택은 가능해요."}]})
            if prompt.startswith("FINAL_ARTICLE_REVIEW"):
                review = support.valid_article()["review"]
                review.update(approved=False, facts_verified=False, issues=["수정 문장의 근거 부족"])
                return json.dumps(review)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        issue = {"index": 0, "code": "ending", "text": sentence, "detail": "어미 반복"}
        with patch("blog_workflow.inspect_article", side_effect=[[issue], [], [], [], []]):
            result = self.assert_blocked("근거 부족", steps=["chatgpt"], quality_checks=True)
        self.assertFalse(self.bridge.generations)
        self.assertEqual(result["final_reviews"], [])
        self.assertFalse(result["final_review_attempts"][0]["review"]["approved"])

    def test_recovery_checkpoint_reuses_actual_provider_model_after_image_failure(self):
        stages, denied = self.configured_recovery()
        self.bridge.missing_image_index = 2
        failed = self.assert_blocked("1장 실패", steps=["chatgpt", "antigravity"], stage_configs=stages)
        checkpoint = json.loads(Path(failed["run_dir"], "stage-2-antigravity.checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["actual_route"]["provider"], "chatgpt")
        self.assertEqual(checkpoint["actual_route"]["model"], "writer")
        self.assertEqual(checkpoint["response_name"], "stage-2-antigravity-recovery")
        for key in ("upstream_sha256", "article_sha256", "request_sha256"):
            self.assertEqual(len(checkpoint[key]), 64)
        self.bridge.missing_image_index = None
        result = self.workflow.resume(failed["run_dir"])
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(denied), 1)
        self.assertEqual(len(self.bridge.generations), 9)
        self.assertTrue(all(stage.get("reused") for stage in result["reviews"]))
        self.assertEqual(result["reviews"][1]["model"], "writer")

    def test_changed_upstream_checkpoint_is_not_reused(self):
        stages, denied = self.configured_recovery()
        self.bridge.missing_image_index = 2
        failed = self.assert_blocked("1장 실패", steps=["chatgpt", "antigravity"], stage_configs=stages)
        path = Path(failed["run_dir"], "stage-2-antigravity.checkpoint.json")
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        checkpoint["upstream_sha256"] = "0" * 64
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        self.bridge.missing_image_index = None
        result = self.workflow.resume(failed["run_dir"])
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(denied), 2)
        self.assertFalse(result["reviews"][1].get("reused", False))

    def test_final_and_image_reviews_keep_distinct_models_on_same_provider(self):
        stages = [{"provider": "chatgpt", "model": "draft-model", "role": "작성"},
                  {"provider": "chatgpt", "model": "review-model", "role": "교차 검수"}]
        result = self.prepare(steps=["chatgpt", "chatgpt"], stage_configs=stages, review_mode=REVIEW_MODES[2])
        self.assertEqual([item["model"] for item in result["final_reviews"]], ["draft-model", "review-model"])
        self.assertEqual([item["model"] for item in result["images"][0]["reviews"]], ["draft-model", "review-model"])
        self.assertEqual(len([call for call in self.bridge.calls if call["images"]]), 16)

    def test_failed_text_provider_is_not_reused_for_final_review(self):
        stages, denied = self.configured_recovery()
        result = self.prepare(steps=["chatgpt", "antigravity"], stage_configs=stages, review_mode=REVIEW_MODES[1])
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(denied), 1)
        self.assertEqual(result["final_reviews"][0]["provider"], "chatgpt")
        self.assertEqual(result["final_reviews"][0]["model"], "writer")
        self.assertEqual([g["provider"] for g in self.bridge.generations], ["antigravity", "chatgpt"] * 4)

    def test_only_rejected_image_is_regenerated_and_visually_checked_again(self):
        seen = {}
        def review(result, index):
            seen[index] = seen.get(index, 0) + 1
            if index == 2 and seen[index] == 1:
                result.update(approved=False, anatomy_ok=False, issues=["손가락 왜곡"])
        self.bridge.image_callback = review
        result = self.prepare(steps=["chatgpt"], image_retry_limit=2)
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(self.bridge.generations), 9)
        self.assertEqual(self.bridge.generations[-1]["provider"], "antigravity")
        self.assertEqual(seen[2], 2)
        self.assertTrue(all(count == 1 for index, count in seen.items() if index != 2))
        self.assertEqual(result["image_generation_attempts"]["2"], 2)

    def test_generation_failure_retries_only_that_file(self):
        self.bridge.missing_image_index = 1
        result = self.prepare(steps=["chatgpt"], image_retry_limit=2)
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(self.bridge.generations), 9)
        self.assertEqual(self.bridge.generations[-1]["provider"], "chatgpt")
        self.assertEqual(result["image_generation_attempts"]["1"], 2)

    def test_cover_retry_budget_is_not_reset_by_resume(self):
        self.bridge.bad_image_indices = {0}
        failed = self.assert_blocked("첫 사진", steps=["chatgpt"], image_retry_limit=2)
        self.assertEqual(len(self.bridge.generations), 10)
        self.assertEqual(failed["image_generation_attempts"]["0"], 3)
        with self.assertRaises(WorkflowError):
            self.workflow.resume(failed["run_dir"])
        self.assertEqual(len(self.bridge.generations), 10)
        request = json.loads(Path(failed["run_dir"], "request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["image_retry_limit"], 2)

    def test_natural_prompt_keeps_total_length_without_density_or_section_floor(self):
        prompt = BlogWorkflow._article_prompt(support.TOPIC, support.KEYWORDS, "내 문체", editorial_mode="natural")
        self.assertNotIn("각 구역은 650자 이상", prompt)
        self.assertNotIn("2~3%를 목표", prompt)
        self.assertIn("4000자 이상", prompt)
        self.assertIn("밀도의 하한", prompt)

    def google_candidates(self, count):
        result = []
        for index in range(count):
            path = self.root / f"source-{index}.png"
            support.make_image(path, seed=400 + index)
            result.append({"path": str(path), "source_url": f"https://example.com/photo/{index}",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/", "license": "CC0",
                "license_verified": True, "commercial_use_allowed": True, "modification_allowed": True,
                "attribution_required": False})
        return result

    def test_ten_google_images_are_checked_before_and_after_caption(self):
        result = self.prepare(steps=["chatgpt"], google_candidates=self.google_candidates(10))
        self.assertEqual(len(result["images"]), 6)
        self.assertEqual(len(result["google_images"]), 10)
        self.assertEqual(len([call for call in self.bridge.calls if call["images"]]), 28)
        for item in result["google_images"]:
            self.assertTrue(item["original_text_free"])
            self.assertTrue(item["source_reviews"][0]["text_free"])
            self.assertTrue(item["reviews"][0]["caption_exact"])
            self.assertFalse(item["reviews"][0]["text_free"])
            self.assertLessEqual(len(item["caption_text"]), 10)

    def test_licensed_google_can_replace_failed_generation_slot(self):
        self.bridge.bad_image_indices = {1, 2, 3}
        result = self.prepare(steps=["chatgpt"], google_candidates=self.google_candidates(2))
        self.assertTrue(result["ready_to_publish"])
        self.assertEqual(len(result["images"]), 6)
        self.assertEqual(sum(item["provider"] == "google" for item in result["images"]), 1)
        self.assertEqual(len(result["google_images"]), 1)
        self.assertEqual(len({item["paragraph_index"] for item in result["images"]}), 6)

    def test_google_source_text_rejection_prevents_caption_publication(self):
        def review(result, index):
            if index == 99 and result["text_free"]:
                result.update(text_free=False, approved=False, issues=["원본에 글자 있음"])
        self.bridge.image_callback = review
        result = self.prepare(steps=["chatgpt"], google_candidates=self.google_candidates(1))
        self.assertEqual(result["google_images"], [])
        self.assertFalse(result["google_candidates"][0]["original_text_free"])
        self.assertEqual(len([call for call in self.bridge.calls if call["images"]]), 9)

    def test_google_caption_ocr_mismatch_is_rejected(self):
        def review(result, index):
            if index == 99 and result.get("caption_exact"):
                result["detected_text"] = "다른 문구"
        self.bridge.image_callback = review
        result = self.prepare(steps=["chatgpt"], google_candidates=self.google_candidates(1))
        self.assertTrue(result["google_candidates"][0]["original_text_free"])
        self.assertEqual(result["google_images"], [])

    def test_missing_captions_use_one_configured_cli_request(self):
        self.bridge.article.pop("google_captions")
        original = self.bridge.run_text
        caption_calls = []
        def run(provider, prompt, **kwargs):
            if prompt.startswith("GOOGLE_IMAGE_CAPTIONS"):
                caption_calls.append((provider, kwargs.get("model")))
                return json.dumps({"google_captions": [f"관리 기준{i}" for i in range(8)]})
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        result = self.prepare(steps=["chatgpt"], stage_configs=[{"provider": "chatgpt", "model": "my-model", "role": "작성"}],
                              google_candidates=self.google_candidates(2))
        self.assertEqual(caption_calls, [("chatgpt", "my-model")])
        self.assertEqual(len(result["google_images"]), 2)

    def test_explicit_duplicate_feedback_can_revise_ready_unpublished_run(self):
        ready = self.prepare(steps=["chatgpt"])
        self.bridge.article["title"] = "배터리 관리의 새로운 관점은 무엇일까?"
        self.bridge.calls.clear()
        request = json.loads(Path(ready["run_dir"], "request.json").read_text(encoding="utf-8"))
        request["resume_run_dir"] = ready["run_dir"]
        request["revision_feedback"] = "기존 발행 제목과 중복입니다. 같은 주제에서 관리 기준 관점을 구체화하세요."
        revised = self.workflow.prepare(**request)
        self.assertEqual(revised["title"], self.bridge.article["title"])
        writing = [call for call in self.bridge.calls if not call["images"]]
        self.assertEqual(len(writing), 1)
        self.assertIn("revision_feedback", writing[0]["prompt"])
        self.assertIn(ready["title"], writing[0]["prompt"])

    def test_editorial_approval_checkpoint_prevents_rewriting_before_image_resume(self):
        self.bridge.bad_image_indices = {0}
        with patch("blog_workflow.inspect_article", return_value=[]), \
             patch.object(self.workflow, "_repair_editorial", wraps=self.workflow._repair_editorial) as editorial:
            failed = self.assert_blocked("첫 사진", steps=["chatgpt"], image_retry_limit=2, quality_checks=True)
            self.assertTrue(Path(failed["run_dir"], "editorial.checkpoint.json").is_file())
            visual_count = len([call for call in self.bridge.calls if call["images"]])
            with self.assertRaises(WorkflowError):
                self.workflow.resume(failed["run_dir"])
        self.assertEqual(editorial.call_count, 1)
        self.assertEqual(len(self.bridge.generations), 10)
        self.assertEqual(len([call for call in self.bridge.calls if call["images"]]), visual_count)

    def test_final_auth_failure_uses_another_successful_text_route_once(self):
        original = self.bridge.run_text
        failed = []
        def run(provider, prompt, **kwargs):
            if provider == "antigravity" and prompt.startswith("FINAL_ARTICLE_REVIEW"):
                failed.append(prompt)
                raise BlogCliError("authentication_required", "login expired", provider=provider)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        result = self.prepare(steps=["chatgpt", "antigravity"], review_mode=REVIEW_MODES[1])
        self.assertEqual(len(failed), 1)
        self.assertEqual(result["final_reviews"][0]["provider"], "chatgpt")
        self.assertEqual(result["final_reviews"][0]["requested_provider"], "antigravity")
        # Successful image reading remains available despite later text auth failure.
        self.assertTrue(all(item["reviews"][0]["provider"] == "antigravity" for item in result["images"]))

    def test_english_photo_search_uses_configured_model_and_preserves_korean_copy(self):
        original_copy = json.loads(json.dumps(self.bridge.article))
        self.workflow._text_call = Mock(return_value={"query": "laptop battery charging home desk photo"})
        result = self.workflow.plan_google_image_search(support.TOPIC, support.KEYWORDS, ["chatgpt"],
            {"chatgpt": "provider-default"}, [{"provider": "chatgpt", "model": "stage-model"}])
        self.assertEqual(result["query"], "laptop battery charging home desk photo")
        self.assertEqual(result["model"], "stage-model")
        self.assertEqual(self.workflow._text_call.call_args.args[4]["chatgpt"], "stage-model")
        self.assertEqual(self.bridge.article, original_copy)
        self.assertTrue(Path(result["run_dir"], "search-plan.json").is_file())

    def test_english_photo_query_rejects_invalid_values(self):
        values = ["", " ", "desk", "laptop battery", "one two three four five six seven eight nine",
                  "노트북 battery charging photo", "site:example.com laptop battery photo",
                  "https://example.com laptop battery photo", "laptop OR battery photo", "laptop battery photo\n",
                  '"laptop battery" charging photo', "laptop battery 2026 photo", "people comparing home appliances photo"]
        for query in values:
            with self.subTest(query=query):
                self.workflow._text_call = Mock(return_value={"query": query})
                with self.assertRaises(WorkflowError):
                    self.workflow.plan_google_image_search(support.TOPIC, support.KEYWORDS, ["chatgpt"], {})
                self.workflow._text_call.assert_called_once()

    def test_english_photo_query_fallback_keeps_provider_model_order(self):
        stages = [{"provider": "chatgpt", "model": "first-model"},
                  {"provider": "chatgpt", "model": "second-model"},
                  {"provider": "antigravity", "model": "third-model"}]
        self.workflow._text_call = Mock(side_effect=[BlogCliError("permission_required", "command denied"),
            {"query": "women choosing a laptop photo"}, {"query": "Korean women choosing laptop photo"}])
        result = self.workflow.plan_google_image_search("노트북 고르기", ["노트북 선택 기준"],
            ["chatgpt", "chatgpt", "antigravity"], {}, stages)
        self.assertEqual(result["provider"], "antigravity")
        self.assertEqual(result["query"], "Korean women choosing laptop photo")
        calls = self.workflow._text_call.call_args_list
        self.assertEqual([(call.args[2], call.args[4][call.args[2]]) for call in calls],
            [("chatgpt", "first-model"), ("chatgpt", "second-model"), ("antigravity", "third-model")])

    def test_english_photo_query_failure_is_optional_workflow_error_without_generation(self):
        self.workflow._text_call = Mock(side_effect=BlogCliError("authentication_required", "login needed"))
        with self.assertRaises(WorkflowError) as caught:
            self.workflow.plan_google_image_search(support.TOPIC, support.KEYWORDS, ["chatgpt", "claude"], {})
        self.assertEqual(self.workflow._text_call.call_count, 2)
        self.assertTrue(Path(caught.exception.run_dir, "search-plan-errors.json").is_file())
        self.assertFalse(self.bridge.generations)

    def test_revised_paragraphs_replace_stale_highlight_style(self):
        ready = self.prepare(steps=["chatgpt"])
        old = self.bridge.article["highlight_phrases"][0]
        new = "배터리를 오래 쓰려면 현재 사용하는 제품의 충전 설정부터 차분하게 확인해야 해요."
        self.bridge.article["paragraphs"][0] = self.bridge.article["paragraphs"][0].replace(old, new)
        self.bridge.article["highlight_phrases"] = [new]
        self.bridge.article["bold_phrases"] = [new]
        request = json.loads(Path(ready["run_dir"], "request.json").read_text(encoding="utf-8"))
        request["revision_feedback"] = "같은 주제의 관리 기준 설명을 구체화하세요."
        revised = self.workflow.prepare(**request, resume_run_dir=ready["run_dir"])
        phrases = revised["visual_style"]["highlight_phrases"]
        self.assertIn(new, str(phrases))
        self.assertNotIn(old, str(phrases))

    def test_title_only_revision_preserves_existing_body_style(self):
        ready = self.prepare(steps=["chatgpt"])
        self.bridge.article["title"] = "배터리 관리의 새로운 관점은 무엇일까?"
        request = json.loads(Path(ready["run_dir"], "request.json").read_text(encoding="utf-8"))
        request["revision_feedback"] = "본문은 유지하고 제목의 관점을 구체화하세요."
        revised = self.workflow.prepare(**request, resume_run_dir=ready["run_dir"])
        self.assertEqual(revised["visual_style"], ready["visual_style"])

    def test_cancel_before_resume_preserves_image_budget_and_approved_files(self):
        self.bridge.bad_image_indices = {0}
        failed = self.assert_blocked("첫 사진", steps=["chatgpt"], image_retry_limit=2)
        self.assertEqual(len(self.bridge.generations), 10)
        approved_hash = failed["image_candidates"][7]["sha256"]
        self.cancel.set()
        with self.assertRaises(WorkflowError):
            self.workflow.resume(failed["run_dir"])
        cancelled = self.latest_manifest()
        self.assertEqual(cancelled["image_generation_attempts"]["0"], 3)
        self.assertEqual(cancelled["image_candidates"][7]["sha256"], approved_hash)
        self.assertTrue(cancelled["image_candidates"][7]["approved"])
        self.cancel.clear()
        with self.assertRaises(WorkflowError):
            self.workflow.resume(failed["run_dir"])
        self.assertEqual(len(self.bridge.generations), 10)

    def test_cancel_during_early_image_slot_keeps_later_approved_slots(self):
        self.bridge.bad_image_indices = {0}
        failed = self.assert_blocked("첫 사진", steps=["chatgpt"], image_retry_limit=2)
        tail = failed["image_candidates"][7]
        def interrupt_after_first_image(message):
            if "이미지 1/8 · 변경 없는 기존 생성 파일 재사용" in message:
                self.cancel.set()
        self.workflow.log = interrupt_after_first_image
        with self.assertRaises(WorkflowError):
            self.workflow.resume(failed["run_dir"])
        cancelled = self.latest_manifest()
        self.assertEqual(len(cancelled["image_candidates"]), 8)
        self.assertEqual(cancelled["image_candidates"][7], tail)
        self.assertEqual(cancelled["image_generation_attempts"]["0"], 3)
        self.cancel.clear()
        self.workflow.log = lambda _: None
        with self.assertRaises(WorkflowError):
            self.workflow.resume(failed["run_dir"])
        self.assertEqual(len(self.bridge.generations), 10)

    def test_failed_text_revision_preserves_previous_image_budget(self):
        self.bridge.bad_image_indices = {0}
        failed = self.assert_blocked("첫 사진", steps=["chatgpt"], image_retry_limit=2)
        request = json.loads(Path(failed["run_dir"], "request.json").read_text(encoding="utf-8"))
        request["revision_feedback"] = "같은 주제의 설명을 다시 확인하세요."
        self.bridge.article["review"]["facts_verified"] = False
        with self.assertRaises(WorkflowError):
            self.workflow.prepare(**request, resume_run_dir=failed["run_dir"])
        interrupted = self.latest_manifest()
        self.assertEqual(interrupted["image_generation_attempts"]["0"], 3)
        self.assertTrue(interrupted["image_candidates"][7]["approved"])
        self.bridge.article["review"]["facts_verified"] = True
        with self.assertRaises(WorkflowError):
            self.workflow.resume(failed["run_dir"])
        self.assertEqual(len(self.bridge.generations), 10)

    def test_changed_paragraph_does_not_inherit_preserved_image_approval_after_cancel(self):
        ready = self.prepare(steps=["chatgpt"], image_retry_limit=2)
        old_hash = ready["image_candidates"][7]["sha256"]
        self.bridge.article["paragraphs"][7] = self.bridge.article["paragraphs"][7].replace(
            "사용 환경과 기기별 지원 기능이 다르므로", "새로운 작업 장소와 기기의 지원 기능을 함께 살펴봐야 하므로")
        request = json.loads(Path(ready["run_dir"], "request.json").read_text(encoding="utf-8"))
        request["revision_feedback"] = "같은 주제의 마지막 구역 설명을 보완하세요."
        def interrupt_after_first_image(message):
            if "이미지 1/8 · 변경 없는 기존 생성 파일 재사용" in message:
                self.cancel.set()
        self.workflow.log = interrupt_after_first_image
        with self.assertRaises(WorkflowError):
            self.workflow.prepare(**request, resume_run_dir=ready["run_dir"])
        interrupted = self.latest_manifest()
        self.assertEqual(interrupted["image_candidates"][7]["sha256"], old_hash)
        self.cancel.clear()
        self.workflow.log = lambda _: None
        revised = self.workflow.resume(ready["run_dir"])
        self.assertTrue(revised["ready_to_publish"])
        self.assertEqual(len(self.bridge.generations), 9)
        self.assertNotEqual(revised["image_candidates"][7]["sha256"], old_hash)
        self.assertEqual(revised["image_generation_attempts"]["7"], 2)


if __name__ == "__main__":
    unittest.main()
