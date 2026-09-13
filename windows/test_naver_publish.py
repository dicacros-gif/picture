"""Offline publication/rights regression tests. Never connects to or posts to Naver."""
import copy
import hashlib
import io
import json
import re
import tempfile
import subprocess
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException

from naver_automation import NaverAutomation
from blog_visual_style import IMAGE_POLICY


class ImmediateWait:
    def __init__(self, driver, *_args, **_kwargs):
        self.driver = driver

    def until(self, callback):
        result = callback(self.driver)
        if not result:
            raise TimeoutException("offline wait did not satisfy condition")
        return result


def document(paragraphs=None, image_ids=()):
    paragraphs = paragraphs or ["본문 템플릿"]
    return {"document": {"components": [
        {"id": "title", "@ctype": "documentTitle", "title": [{"value": "제목"}]},
        {"id": "text", "@ctype": "text", "value": [
            {"id": f"p{i}", "@ctype": "paragraph", "nodes": [
                {"id": f"n{i}", "@ctype": "textNode", "value": value}
            ]} for i, value in enumerate(paragraphs)
        ]},
        *[{"id": value, "@ctype": "image", "src": f"https://local.invalid/{value}"} for value in image_ids],
    ]}}


class ArticlePlacementTests(unittest.TestCase):
    def setUp(self):
        self.paragraphs = [f"문단 {index + 1}. 원문 전체를 보존한다." for index in range(8)]
        self.ids = [f"image-{index}" for index in range(6)]
        self.positions = [0, 1, 3, 4, 6, 7]

    def test_eight_exact_paragraphs_and_semantic_image_positions(self):
        source = document(image_ids=self.ids)
        original = copy.deepcopy(source)
        result = NaverAutomation._arrange_article_document(source, self.paragraphs, self.ids, self.positions)
        self.assertTrue(NaverAutomation._verify_article_document(result, self.paragraphs, self.ids, self.positions))
        self.assertEqual(source, original, "Arrangement must not mutate the supplied snapshot")
        components = result["document"]["components"]
        self.assertEqual(len([x for x in components if x["@ctype"] == "text"]), 8)
        self.assertEqual([x["src"] for x in components if x["@ctype"] == "image"],
                         [f"https://local.invalid/{value}" for value in self.ids])

    def test_multiple_images_after_one_paragraph_keep_identity(self):
        positions = [0, 0, 3, 4, 6, 7]
        result = NaverAutomation._arrange_article_document(document(image_ids=self.ids), self.paragraphs, self.ids, positions)
        self.assertTrue(NaverAutomation._verify_article_document(result, self.paragraphs, self.ids, positions))

    def test_missing_or_extra_uploaded_images_stop_arrangement(self):
        for ids in [self.ids[:-1], self.ids + ["extra"]]:
            with self.subTest(ids=ids), self.assertRaises(RuntimeError):
                NaverAutomation._arrange_article_document(document(image_ids=ids), self.paragraphs, self.ids, self.positions)

    def test_exact_text_edit_image_swap_or_extra_paragraph_fails_verification(self):
        arranged = NaverAutomation._arrange_article_document(document(image_ids=self.ids), self.paragraphs, self.ids, self.positions)
        changed = copy.deepcopy(arranged)
        changed["document"]["components"][1]["value"][0]["nodes"][0]["value"] += " 추가"
        self.assertFalse(NaverAutomation._verify_article_document(changed, self.paragraphs, self.ids, self.positions))
        self.assertFalse(NaverAutomation._verify_article_document(arranged, self.paragraphs, list(reversed(self.ids)), self.positions))
        changed = copy.deepcopy(arranged)
        changed["document"]["components"].append(copy.deepcopy(changed["document"]["components"][1]))
        self.assertFalse(NaverAutomation._verify_article_document(changed, self.paragraphs, self.ids, self.positions))

    def test_unknown_components_are_not_silently_published(self):
        data = document(image_ids=self.ids)
        data["document"]["components"].append({"@ctype": "video", "id": "v"})
        with self.assertRaisesRegex(RuntimeError, "예상하지"):
            NaverAutomation._arrange_article_document(data, self.paragraphs, self.ids, self.positions)

    def test_multiline_sections_preserve_blank_rows_and_eight_components(self):
        sections = [f"구역 {index + 1}\n\n첫 문장입니다.\n\n다음 문장입니다." for index in range(8)]
        result = NaverAutomation._arrange_article_document(document(image_ids=self.ids), sections, self.ids, self.positions)
        components = [item for item in result["document"]["components"] if item["@ctype"] == "text"]
        self.assertEqual(len(components), 8)
        self.assertEqual([len(item["value"]) for item in components], [5] * 8)
        self.assertEqual(components[0]["value"][1]["nodes"][0]["value"], "\u200b")
        self.assertTrue(NaverAutomation._verify_article_document(result, sections, self.ids, self.positions))
        components[0]["value"].pop(1)
        self.assertFalse(NaverAutomation._verify_article_document(result, sections, self.ids, self.positions))

    def test_native_heading_and_selected_term_bold_preserve_literal_text(self):
        sections = ["❝금리 변화, 지금 확인할 점❞\n\n금리 기준을 먼저 보고 금리 변화를 살펴봅니다."] * 8
        # Runtime passes the style delta observed from the editor's own bold command.
        observed_style = {"bold": True}
        result = NaverAutomation._arrange_article_document(
            document(image_ids=self.ids), sections, self.ids, self.positions,
            bold_terms=["금리"], bold_style=observed_style,
        )
        rows = next(item for item in result["document"]["components"] if item["@ctype"] == "text")["value"]
        self.assertTrue(rows[0]["nodes"][0]["style"]["bold"])
        self.assertNotIn("bold", rows[1]["nodes"][0]["style"])
        self.assertEqual([node["value"] for node in rows[2]["nodes"]], ["금리", " 기준을 먼저 보고 ", "금리", " 변화를 살펴봅니다."])
        self.assertTrue(NaverAutomation._verify_article_document(result, sections, self.ids, self.positions,
                         bold_terms=["금리"], bold_style=observed_style))
        rows[0]["nodes"][0]["style"].pop("bold")
        self.assertFalse(NaverAutomation._verify_article_document(result, sections, self.ids, self.positions,
                          bold_terms=["금리"], bold_style=observed_style))

    def test_bold_headings_cannot_guess_unknown_editor_style(self):
        sections = ["❝소제목❞\n\n본문입니다."] * 8
        with self.assertRaisesRegex(RuntimeError, "실제 굵게 서식"):
            NaverAutomation._arrange_article_document(document(image_ids=self.ids), sections, self.ids, self.positions)

    def test_bold_style_delta_is_learned_from_matching_real_node(self):
        before = document(["소제목"])
        after = copy.deepcopy(before)
        after["document"]["components"][1]["value"][0]["nodes"][0]["style"] = {"@ctype": "nodeStyle", "bold": True}
        self.assertEqual(NaverAutomation._observed_bold_style(before, after), {"bold": True})
        after["document"]["components"][1]["value"][0]["nodes"][0]["value"] = "다른 글"
        self.assertEqual(NaverAutomation._observed_bold_style(before, after), {})


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = NaverAutomation(self.root, lambda message: None)
        self.article = {"title": "테스트 제목", "paragraphs": [f"본문 {i + 1}입니다." for i in range(8)], "images": []}
        self.article.update({"ready_to_publish": True, "reviews": [{"stage": 1, "provider": "claude", "review": {
            "approved": True, "facts_verified": True, "sources_verified": True,
            "search_intent_satisfied": True, "natural_korean": True, "issues": [],
        }}]})
        self._set_reviewed_content_hash()
        for i in range(8):
            path = self.root / f"image-{i}.png"
            Image.new("RGB", (20, 20), (i * 25, 10, 30)).save(path)
            if i < 6:
                self.article["images"].append({
                    "path": str(path), "paragraph_index": [0, 1, 3, 4, 6, 7][i],
                    "provider": "chatgpt" if i < 3 else "antigravity", "approved": True,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "reviews": [{"provider": "claude", "approved": True}],
                })
        self.driver = MagicMock()
        self.driver.current_url = "https://blog.naver.com/testblog/postwrite"
        self.opener = MagicMock()
        self.final = MagicMock()
        self.publish_panel_open = False
        self.opener.click.side_effect = lambda: setattr(self, "publish_panel_open", True)
        self.app._driver = MagicMock(return_value=self.driver)
        self.app._prepare_article_in_writer = MagicMock(return_value=[f"id-{i}" for i in range(6)])
        self.app._article_ready_to_publish = MagicMock(return_value=True)
        self.app._find_publish_control = MagicMock(side_effect=lambda _driver, final=False:
            (self.final if self.publish_panel_open else None) if final else self.opener)
        self.app._published_article_url = MagicMock(return_value="https://blog.naver.com/testblog/123456789012")
        self.app.inspect_published_naver_article = MagicMock(return_value={"verified": True})
        self.wait = patch("naver_automation.WebDriverWait", ImmediateWait)
        self.wait.start()
        self.addCleanup(self.wait.stop)

    def _set_reviewed_content_hash(self):
        self.article["reviewed_content_sha256"] = hashlib.sha256(json.dumps(
            {"title": self.article["title"], "paragraphs": self.article["paragraphs"]},
            ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()

    def _append_google_images(self, count, *, captions=False):
        for index in range(count):
            path = self.root / f"reference-{index}.png"
            Image.new("RGB", (20, 20), (15, index * 17, 110)).save(path)
            item = {"path": str(path), "provider": "google", "paragraph_index": index % 8,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "approved": True,
                    "license_verified": True, "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                    "commercial_use_allowed": True, "modification_allowed": True, "attribution_required": False,
                    "reviews": [{"provider": "claude", "approved": True, "text_free": True}]}
            if captions:
                item.update(caption_text="방문 전 확인", caption_applied=True, caption_placement="top",
                            caption_layout="separate_band", caption_band_height=100, original_text_free=True)
                item["reviews"] = [{"provider": "claude", "approved": True, "text_free": False,
                                    "caption_exact": True, "caption_legible": True, "no_other_text": True,
                                    "detected_text": "방문 전 확인"}]
            self.article["images"].append(item)

    def test_six_generated_plus_ten_references_publish_sixteen_images_and_reuse_receipt(self):
        self._append_google_images(10)
        self.app._prepare_article_in_writer.return_value = [f"id-{index}" for index in range(16)]
        result = self.app.publish_naver_article("testblog", self.article)
        self.assertEqual(result["image_count"], 16)
        self.assertEqual(len(self.app._prepare_article_in_writer.call_args.args[4]), 16)
        self.assertEqual(result["image_positions"], sorted(item["paragraph_index"] for item in self.article["images"]))
        self.assertEqual(self.app.publication_receipt_for("testblog", self.article)["article_key"], result["article_key"])
        self.assertTrue(self.app.publish_naver_article("testblog", self.article)["reused_receipt"])
        self.final.click.assert_called_once()

    def test_sixteen_images_use_one_document_placement_pass_and_preserve_order(self):
        self._append_google_images(10)
        title, paragraphs, images = self.app._validate_publish_article(self.article)
        state = {"data": document(paragraphs), "uploaded": 0}
        self.app._open_writer = MagicMock(return_value=(MagicMock(), MagicMock()))
        self.app._editor_has_existing_content = MagicMock(return_value=False)
        self.app._replace_editor_text = MagicMock()
        self.app._read_article_document = MagicMock(side_effect=lambda _driver: copy.deepcopy(state["data"]))
        self.app._set_article_document = MagicMock(side_effect=lambda _driver, value: state.update(data=copy.deepcopy(value)))
        self.app._focus_body_image_position = MagicMock()
        def upload(_driver, paths):
            for path in paths:
                index = state["uploaded"]
                self.assertEqual(path, images[index]["path"])
                state["data"]["document"]["components"].append({"@ctype": "image", "id": f"uploaded-{index}",
                                                                    "src": f"https://local.invalid/{index}"})
                state["uploaded"] += 1
        self.app._upload_blog_images = MagicMock(side_effect=upload)
        ids = NaverAutomation._prepare_article_in_writer(self.app, self.driver, "testblog", title, paragraphs, images)
        self.assertEqual(ids, [f"uploaded-{index}" for index in range(16)])
        self.app._upload_blog_images.assert_called_once_with(self.driver, [item["path"] for item in images])
        self.app._focus_body_image_position.assert_not_called()
        self.app._set_article_document.assert_called_once()
        positions = [item["paragraph_index"] for item in images]
        self.assertTrue(NaverAutomation._verify_article_document(state["data"], paragraphs, ids, positions))
        self.assertFalse(NaverAutomation._verify_article_document(state["data"], paragraphs,
                         [ids[1], ids[0], *ids[2:]], positions))
        self.assertEqual(positions[:3], [0, 0, 0])

    def test_upload_sends_one_path_per_input_even_when_input_claims_multiple(self):
        paths = [item["path"] for item in self.article["images"][:3]]
        uploads = [MagicMock() for _path in paths]
        state = {"count": 0}
        for upload in uploads:
            upload.get_attribute.return_value = "multiple"
            upload.send_keys.side_effect = lambda _path: state.update(count=state["count"] + 1)
        self.app._image_component_count = MagicMock(side_effect=lambda _driver: state["count"])
        self.app._find_image_inputs = MagicMock(side_effect=[[upload] for upload in uploads])

        NaverAutomation._upload_blog_images(self.app, self.driver, paths)

        self.assertEqual(state["count"], 3)
        for upload, path in zip(uploads, paths):
            upload.send_keys.assert_called_once_with(str(Path(path).resolve()))
            upload.get_attribute.assert_not_called()

    def test_seventeenth_image_and_eleventh_google_reference_are_rejected(self):
        self._append_google_images(11)
        with self.assertRaisesRegex(ValueError, "6~16"):
            self.app._validate_publish_article(self.article)
        self.article["images"].pop(5)
        with self.assertRaisesRegex(ValueError, "최대 10장"):
            self.app._validate_publish_article(self.article)

    def test_reference_caption_requires_original_text_free_and_exact_final_vision_review(self):
        self._append_google_images(1, captions=True)
        self.assertEqual(len(self.app._validate_publish_article(self.article)[2]), 7)
        for changes in ({"original_text_free": False}, {"caption_applied": False}, {"caption_layout": "over_image"},
                        {"caption_text": "열한글자이상의아주긴설명입니다"}, {"caption_text": "영문\n표시"},
                        {"caption_text": "ABC"}):
            invalid = copy.deepcopy(self.article)
            invalid["images"][-1].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "가운데 한글"):
                self.app._validate_publish_article(invalid)
        for changes in ({"caption_exact": False}, {"caption_legible": False}, {"no_other_text": False},
                        {"detected_text": "다른 문구"}, {"text_free": True}):
            invalid = copy.deepcopy(self.article)
            invalid["images"][-1]["reviews"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "캡션의 정확성"):
                self.app._validate_publish_article(invalid)

    def test_generated_cover_policy_allows_only_separately_verified_reference_captions(self):
        self._append_google_images(4, captions=True)
        headline = "방문 전 확인"
        self.article.update(image_policy=IMAGE_POLICY, cover_headline=headline)
        for image in self.article["images"][:6]:
            image["image_policy"] = IMAGE_POLICY
            image["reviews"][0]["text_free"] = True
        cover = self.article["images"][0]
        cover.update(cover_headline=headline, cover_text_applied=True, cover_aspect_ratio="1:1",
                     cover_text_color="#8CE88C", width=20, height=20)
        cover["reviews"][0].update(text_free=False, detected_text=headline,
                                   **{flag: True for flag in ("cover_text_exact", "cover_text_legible", "no_other_text",
                                       "square_1_to_1", "no_human_face", "bold_gothic", "text_shadow_visible", "approved_text_color")})
        self.assertEqual(len(self.app._validate_publish_article(self.article)[2]), 10)

    def test_center_question_caption_requires_new_colors_and_actual_text_review(self):
        from image_delivery import CAPTION_RENDER_VERSION
        self._append_google_images(1, captions=True)
        item = self.article["images"][-1]
        item.update(caption_text="재고는 언제 확인할까?", caption_placement="center",
                    caption_layout="center_overlay", caption_band_height=0,
                    caption_render_version=CAPTION_RENDER_VERSION,
                    caption_text_colors=["#FFFFFF", "#8CE88C"], caption_panel_color="#000000")
        item["reviews"][0]["detected_text"] = item["caption_text"]
        self.assertEqual(len(self.app._validate_publish_article(self.article)[2]), 7)
        for changes in ({"caption_text_colors": ["#EF3340", "#8CE88C"]},
                        {"caption_placement": "top"}, {"caption_text": "재고 확인 방법"},
                        {"caption_render_version": "unknown"}, {"original_text_free": False}):
            invalid = copy.deepcopy(self.article)
            invalid["images"][-1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.app._validate_publish_article(invalid)

    def test_new_cover_palette_is_white_green_while_old_approval_stays_readable(self):
        from image_delivery import COVER_RENDER_VERSION
        self.test_generated_cover_policy_allows_only_separately_verified_reference_captions()
        cover = self.article["images"][0]
        cover["cover_text_color"] = "#EF3340"
        self.app._validate_publish_article(self.article)
        cover.update(cover_render_version=COVER_RENDER_VERSION,
                     cover_text_color="#8CE88C", cover_text_colors=["#FFFFFF", "#8CE88C"])
        self.app._validate_publish_article(self.article)
        cover["cover_text_colors"] = ["#EF3340", "#8CE88C"]
        with self.assertRaisesRegex(ValueError, "흰색과 형광 녹색"):
            self.app._validate_publish_article(self.article)

    def test_published_page_inspects_sixteen_images_in_repeated_section_positions(self):
        self._append_google_images(10)
        self.app._article_native_bold_rendered = MagicMock(return_value=True)
        ordered = sorted(self.article["images"], key=lambda item: item["paragraph_index"])
        ids = [f"image-{index}" for index in range(16)]
        self.driver.execute_script.return_value = {"sections": list(self.article["paragraphs"]),
            "images": [{"id": image_id, "position": image["paragraph_index"]} for image_id, image in zip(ids, ordered)]}
        result = NaverAutomation.inspect_published_naver_article(self.app, "testblog", self.article, expected_image_ids=ids)
        self.assertTrue(result["verified"])
        self.assertEqual(result["image_count"], 16)
        self.driver.get.assert_not_called()

    def test_success_clicks_final_once_and_checks_post_url_title(self):
        receipt = self.app.publish_naver_article("testblog", self.article)
        self.assertTrue(receipt["published"])
        self.assertTrue(receipt["content_verified"])
        self.assertEqual(receipt["content_verification_issues"], [])
        self.app.inspect_published_naver_article.assert_called_once()
        self.assertEqual(self.app.inspect_published_naver_article.call_args.kwargs["expected_image_ids"],
                         [f"id-{i}" for i in range(6)])
        self.final.click.assert_called_once()
        self.opener.click.assert_called_once()
        self.app._published_article_url.assert_called_once_with(self.driver, "testblog", self.article["title"])
        repeated = self.app.publish_naver_article("testblog", self.article)
        self.assertTrue(repeated["published"])
        self.assertTrue(repeated["reused_receipt"])
        self.final.click.assert_called_once()
        self.app._prepare_article_in_writer.assert_called_once()

    def test_count_or_order_failure_never_opens_publish_panel(self):
        self.app._article_ready_to_publish.return_value = False
        with self.assertRaisesRegex(RuntimeError, "순서 검증"):
            self.app.publish_naver_article("testblog", self.article)
        self.opener.click.assert_not_called()
        self.final.click.assert_not_called()
        self.assertFalse((self.root / "publication_receipts").exists())

    def test_receipt_lookup_without_receipt_is_read_only(self):
        self.assertIsNone(self.app.publication_receipt_for("testblog", self.article))
        self.assertFalse((self.root / "publication_receipts").exists())
        self.app._driver.assert_not_called()

    def test_receipt_reuse_after_image_cleanup_never_requires_original_files(self):
        receipt = self.app.publish_naver_article("testblog", self.article)
        for item in self.article["images"]:
            Path(item["path"]).unlink()
        self.app._driver.reset_mock()
        self.assertEqual(self.app.publication_receipt_for("testblog", self.article), receipt)
        repeated = self.app.publish_naver_article("testblog", self.article)
        self.assertTrue(repeated["published"])
        self.assertTrue(repeated["reused_receipt"])
        self.app._driver.assert_not_called()
        self.final.click.assert_called_once()

    def test_receipt_identity_normalizes_title_sections_and_image_order(self):
        self.app.publish_naver_article("testblog", self.article)
        same = copy.deepcopy(self.article)
        same["title"] = " " + same["title"] + " "
        same["paragraphs"] = [" " + section + " " for section in same["paragraphs"]]
        same["images"].reverse()
        self.assertTrue(self.app.publication_receipt_for("TESTBLOG", same)["published"])
        same["paragraphs"][0] += " 본문 수정"
        self.assertIsNone(self.app.publication_receipt_for("testblog", same))

    def test_receipt_lookup_rejects_corrupt_record_without_browser_or_rewrite(self):
        receipt = self.app.publish_naver_article("testblog", self.article)
        path = self.root / "publication_receipts" / f"{receipt['article_key']}.json"
        for raw in ("{broken", "[]", json.dumps({**receipt, "article_key": "wrong"}),
                    json.dumps({**receipt, "url": "https://blog.naver.com/otherblog/12345"})):
            path.write_text(raw, encoding="utf-8")
            self.app._driver.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "기존 발행 기록"):
                self.app.publication_receipt_for("testblog", self.article)
            self.assertEqual(path.read_text(encoding="utf-8"), raw)
            self.app._driver.assert_not_called()

    def test_receipt_lookup_validates_metadata_without_opening_files(self):
        for images in (None, [{"sha256": "invalid", "paragraph_index": 0}] * 6,
                       [{**item, "paragraph_index": True} for item in self.article["images"]]):
            with self.subTest(images=images), self.assertRaises(ValueError):
                self.app.publication_receipt_for("testblog", {**self.article, "images": images})
        self.app._driver.assert_not_called()

    def test_legacy_published_receipt_preserves_success_and_marks_content_unverified(self):
        receipt = self.app.publish_naver_article("testblog", self.article)
        legacy = {key: value for key, value in receipt.items() if not key.startswith("content_verif")}
        path = self.root / "publication_receipts" / f"{receipt['article_key']}.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = self.app.publication_receipt_for("testblog", self.article)
        self.assertTrue(loaded["published"])
        self.assertFalse(loaded["content_verified"])
        self.assertTrue(loaded["content_verification_issues"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), legacy)

    def test_published_content_mismatch_preserves_submission_and_never_reclicks(self):
        self.app.inspect_published_naver_article.return_value = {
            "verified": False, "sections_match": False, "image_positions_match": False,
        }
        result = self.app.publish_naver_article("testblog", self.article)
        self.assertTrue(result["published"])
        self.assertEqual(result["status"], "published")
        self.assertFalse(result["content_verified"])
        self.assertEqual(len(result["content_verification_issues"]), 2)
        self.assertTrue(self.app.publish_naver_article("testblog", self.article)["published"])
        self.final.click.assert_called_once()

    def test_inspection_error_keeps_durable_confirmed_publication(self):
        def failed_inspection(*args, **kwargs):
            stored = json.loads(next((self.root / "publication_receipts").glob("*.json")).read_text(encoding="utf-8"))
            self.assertTrue(stored["published"], "Submission must be durable before inspection")
            raise WebDriverException("read-only page snapshot unavailable")
        self.app.inspect_published_naver_article.side_effect = failed_inspection
        result = self.app.publish_naver_article("testblog", self.article)
        self.assertTrue(result["published"])
        self.assertFalse(result["content_verified"])
        self.assertIn("snapshot unavailable", result["content_verification_issues"][0])
        self.assertEqual(self.app.publication_receipt_for("testblog", self.article), result)
        self.final.click.assert_called_once()

    def test_content_changed_after_panel_open_never_submits(self):
        self.app._article_ready_to_publish.side_effect = [True, False]
        with self.assertRaisesRegex(RuntimeError, "발행 직전"):
            self.app.publish_naver_article("testblog", self.article)
        self.opener.click.assert_called_once()
        self.final.click.assert_not_called()

    def test_missing_final_button_reports_specific_error_without_receipt_or_final_click(self):
        self.app._find_publish_control.side_effect = lambda _driver, final=False: None if final else self.opener
        with self.assertRaisesRegex(RuntimeError, "최종 버튼은 누르지 않았으며"):
            self.app.publish_naver_article("testblog", self.article)
        self.opener.click.assert_called_once()
        self.final.click.assert_not_called()
        self.assertFalse((self.root / "publication_receipts").exists())

    def test_uncertain_timeout_is_durable_and_never_reclicked(self):
        self.app._published_article_url.return_value = ""
        receipt = self.app.publish_naver_article("testblog", self.article)
        self.assertFalse(receipt["published"])
        self.assertEqual(receipt["status"], "uncertain")
        self.final.click.assert_called_once()
        new_app = NaverAutomation(self.root, lambda message: None)
        new_app._driver = MagicMock(side_effect=AssertionError("Must not reopen writer after uncertainty"))
        repeated = new_app.publish_naver_article("testblog", self.article)
        self.assertEqual(repeated["status"], "uncertain")
        new_app._driver.assert_not_called()

    def test_click_error_is_uncertain_and_receipt_precedes_click(self):
        def failed_click():
            receipts = list((self.root / "publication_receipts").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text(encoding="utf-8"))["status"], "uncertain")
            raise WebDriverException("connection lost while submitting")
        self.final.click.side_effect = failed_click
        result = self.app.publish_naver_article("testblog", self.article)
        self.assertEqual(result["status"], "uncertain")
        self.final.click.assert_called_once()

    def test_parallel_claim_prevents_second_submission(self):
        self.app._claim_publication_receipt = MagicMock(return_value=False)
        result = self.app.publish_naver_article("testblog", self.article)
        self.assertTrue(result["reused_receipt"])
        self.final.click.assert_not_called()

    def test_prepare_only_never_clicks_publish(self):
        result = self.app.publish_naver_article("testblog", self.article, publish=False)
        self.assertEqual(result["status"], "prepared")
        self.opener.click.assert_not_called()
        self.final.click.assert_not_called()

    def _mock_draft_button(self, confirmed=True):
        button = MagicMock()
        button.text = "저장"
        self.app._find_draft_buttons = MagicMock(return_value=[button])
        self.app._arm_article_draft_confirmation = MagicMock()
        self.app._fresh_article_draft_confirmed = MagicMock(return_value=confirmed)
        return button

    def test_draft_save_clicks_once_and_requires_fresh_confirmation(self):
        button = self._mock_draft_button()
        result = self.app.publish_naver_article("testblog", self.article, publish=False, save_draft=True)
        self.assertEqual(result["status"], "draft_saved")
        self.assertTrue(result["saved"])
        self.assertFalse(result["published"])
        self.assertEqual(result["url"], self.driver.current_url)
        button.click.assert_called_once()
        self.opener.click.assert_not_called()
        self.final.click.assert_not_called()
        self.app._find_publish_control.assert_not_called()
        token = self.app._arm_article_draft_confirmation.call_args.args[1]
        self.assertTrue(token)
        self.app._fresh_article_draft_confirmed.assert_called_once_with(self.driver, token)
        self.assertFalse((self.root / "publication_receipts").exists())

    def test_draft_confirmation_timeout_is_failure_and_never_reclicked(self):
        button = self._mock_draft_button(confirmed=False)
        with self.assertRaisesRegex(RuntimeError, "저장 성공으로 처리하지"):
            self.app.publish_naver_article("testblog", self.article, publish=False, save_draft=True)
        button.click.assert_called_once()
        self.app._find_publish_control.assert_not_called()

    def test_missing_or_ambiguous_draft_button_never_clicks(self):
        button = self._mock_draft_button()
        for candidates in [[], [button, button]]:
            self.app._find_draft_buttons.return_value = candidates
            with self.subTest(count=len(candidates)), self.assertRaisesRegex(RuntimeError, "고유하게"):
                self.app.publish_naver_article("testblog", self.article, publish=False, save_draft=True)
        button.click.assert_not_called()
        self.app._find_publish_control.assert_not_called()

    def test_draft_list_button_with_count_is_not_current_save(self):
        button = self._mock_draft_button()
        button.text = "임시저장 3"
        with self.assertRaisesRegex(RuntimeError, "고유하게"):
            self.app.publish_naver_article("testblog", self.article, publish=False, save_draft=True)
        button.click.assert_not_called()

    def test_draft_with_invalid_content_does_not_click_save(self):
        button = self._mock_draft_button()
        self.app._article_ready_to_publish.return_value = False
        with self.assertRaisesRegex(RuntimeError, "순서 검증"):
            self.app.publish_naver_article("testblog", self.article, publish=False, save_draft=True)
        button.click.assert_not_called()
        self.app._arm_article_draft_confirmation.assert_not_called()

    def test_draft_with_publish_flag_is_rejected_before_browser(self):
        with self.assertRaisesRegex(ValueError, "동시에 선택"):
            self.app.publish_naver_article("testblog", self.article, publish=True, save_draft=True)
        self.app._driver.assert_not_called()

    def test_existing_published_receipt_cannot_claim_requested_draft_was_saved(self):
        self.assertTrue(self.app.publish_naver_article("testblog", self.article)["published"])
        self.opener.click.reset_mock()
        self.final.click.reset_mock()
        self.app._find_publish_control.reset_mock()
        button = self._mock_draft_button()
        result = self.app.publish_naver_article("testblog", self.article, publish=False, save_draft=True)
        self.assertEqual(result["status"], "draft_saved")
        self.assertFalse(result["published"])
        self.assertNotIn("reused_receipt", result)
        button.click.assert_called_once()
        self.opener.click.assert_not_called()
        self.final.click.assert_not_called()

    def test_draft_does_not_report_success_on_unexpected_post_navigation(self):
        button = self._mock_draft_button()
        self.driver.current_url = "https://blog.naver.com/testblog/123456789012"
        with self.assertRaisesRegex(RuntimeError, "게시글 화면 이동"):
            self.app.publish_naver_article("testblog", self.article, publish=False, save_draft=True)
        button.click.assert_called_once()
        self.app._find_publish_control.assert_not_called()

    def test_wrong_paragraph_count_missing_file_or_rejected_review_stops_before_browser(self):
        bad_variants = []
        wrong_count = copy.deepcopy(self.article)
        wrong_count["paragraphs"].pop()
        bad_variants.append(wrong_count)
        missing = copy.deepcopy(self.article)
        missing["images"][0]["path"] = str(self.root / "missing.png")
        bad_variants.append(missing)
        rejected = copy.deepcopy(self.article)
        rejected["images"][0]["reviews"][0]["approved"] = False
        bad_variants.append(rejected)
        for value in bad_variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.app.publish_naver_article("testblog", value)
        self.app._driver.assert_not_called()

    def test_google_requires_license_rights_review_and_no_public_attribution(self):
        ref = {"path": str(self.root / "image-6.png"), "paragraph_index": 2, "provider": "google",
               "sha256": hashlib.sha256((self.root / "image-6.png").read_bytes()).hexdigest(),
               "approved": True, "reviews": [{"provider": "claude", "approved": True}]}
        self.article["images"].append(ref)
        with self.assertRaisesRegex(ValueError, "라이선스"):
            self.app.publish_naver_article("testblog", self.article)
        ref.update({"license_verified": True, "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "commercial_use_allowed": True, "modification_allowed": True,
                    "attribution_required": True, "attribution": "출처: 예시 작가"})
        with self.assertRaisesRegex(ValueError, "출처 표시"):
            self.app.publish_naver_article("testblog", self.article)
        self.article["paragraphs"][2] += " 출처: 예시 작가"
        self._set_reviewed_content_hash()
        with self.assertRaisesRegex(ValueError, "공개 출처 표시"):
            self.app._validate_publish_article(self.article)
        ref.update({"attribution_required": False, "license_url": "https://creativecommons.org/publicdomain/zero/1.0/"})
        title, paragraphs, images = self.app._validate_publish_article(self.article)
        self.assertEqual(len(paragraphs), 8)
        self.assertEqual(len(images), 7)

    def test_multiline_article_validation_accepts_sentence_spacing(self):
        self.article["paragraphs"] = [f"소제목 {index}\n\n첫 문장.\n\n두 번째 문장." for index in range(8)]
        self._set_reviewed_content_hash()
        _title, sections, _images = self.app._validate_publish_article(self.article)
        self.assertEqual(sections, self.article["paragraphs"])

    def test_verified_reference_can_replace_generated_body_image_but_never_cover(self):
        ref = self.article["images"][3]
        ref.update(provider="google", license_verified=True,
                   license_url="https://creativecommons.org/publicdomain/zero/1.0/",
                   commercial_use_allowed=True, modification_allowed=True, attribution_required=False)
        self.assertEqual(len(self.app._validate_publish_article(self.article)[2]), 6)
        ref["reviews"][0]["approved"] = False
        with self.assertRaisesRegex(ValueError, "시각 검수"):
            self.app._validate_publish_article(self.article)
        ref["reviews"][0]["approved"] = True
        ref["paragraph_index"] = 0
        self.article["images"][0]["paragraph_index"] = 1
        with self.assertRaisesRegex(ValueError, "생성 표지"):
            self.app._validate_publish_article(self.article)

    def test_reference_cannot_claim_attribution_free_with_by_license(self):
        self.article["images"][3].update(provider="google", license_verified=True,
                   license_url="https://creativecommons.org/licenses/by/4.0/",
                   commercial_use_allowed=True, modification_allowed=True, attribution_required=False)
        with self.assertRaisesRegex(ValueError, "CC0"):
            self.app._validate_publish_article(self.article)

    def test_public_domain_file_template_is_validated_again_before_publication(self):
        self._append_google_images(1)
        item = self.article["images"][-1]
        source = ReferenceLicenseTests.source
        image = ReferenceLicenseTests.image
        item.update(NaverAutomation._commons_license_evidence(source, image, {
            "original_file_present": True, "license_links": [], "public_domain_templates": [{
                "name": "Public domain", "link_required": "false", "attribution_required": "false"}]}))
        item.update(source_url=source, image_url=image)
        self.assertEqual(len(self.app._validate_publish_article(self.article)[2]), 7)
        for changes in ({"license_evidence_type": "search_result"}, {"public_domain_template": {}},
                        {"source_url": "https://example.com/File:Example.jpg"},
                        {"image_url": image.replace("Example", "Unrelated")},
                        {"license_evidence_url": source + "?other"},
                        {"license_url": source + "#Other"}):
            bad = copy.deepcopy(self.article)
            bad["images"][-1].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "CC0"):
                self.app._validate_publish_article(bad)

    def test_public_markdown_and_source_urls_are_rejected(self):
        for text in ["**강조** 본문", "# 소제목", "출처 https://example.com/source"]:
            self.article["paragraphs"][0] = text
            self._set_reviewed_content_hash()
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.app.publish_naver_article("testblog", self.article)
        self.app._driver.assert_not_called()

    def test_same_image_bytes_cannot_count_as_six_unique_pictures(self):
        self.article["images"][1]["path"] = self.article["images"][0]["path"]
        self.article["images"][1]["sha256"] = self.article["images"][0]["sha256"]
        with self.assertRaisesRegex(ValueError, "중복"):
            self.app.publish_naver_article("testblog", self.article)
        self.final.click.assert_not_called()

    def test_changed_image_after_review_is_rejected(self):
        path = Path(self.article["images"][0]["path"])
        Image.new("RGB", (20, 20), "yellow").save(path)
        with self.assertRaisesRegex(ValueError, "이미지 파일이 변경"):
            self.app.publish_naver_article("testblog", self.article)
        self.app._driver.assert_not_called()

    def test_provisional_image_requires_final_context_review_even_if_initial_review_passed(self):
        self.article["images"][0]["requires_final_semantic_review"] = True
        with self.assertRaisesRegex(ValueError, "최종 본문 문맥"):
            self.app.publish_naver_article("testblog", self.article)
        self.app._driver.assert_not_called()

    def test_image_moved_to_different_section_after_visual_review_is_rejected(self):
        image = self.article["images"][0]
        image["reviewed_paragraph_sha256"] = hashlib.sha256(
            self.article["paragraphs"][image["paragraph_index"]].encode("utf-8")
        ).hexdigest()
        image["paragraph_index"] = 2
        with self.assertRaisesRegex(ValueError, "배치 구역"):
            self.app.publish_naver_article("testblog", self.article)
        self.app._driver.assert_not_called()

    def test_final_article_review_does_not_revalidate_image_for_changed_section(self):
        image = self.article["images"][0]
        image["reviewed_paragraph_sha256"] = hashlib.sha256(
            self.article["paragraphs"][0].encode("utf-8")
        ).hexdigest()
        self.article["paragraphs"][0] = "최종 글 검수에서 주제를 바꾼 문단입니다.\n\n기존 사진 문맥과 다릅니다."
        self._set_reviewed_content_hash()
        with self.assertRaisesRegex(ValueError, "본문 내용이 변경"):
            self.app.publish_naver_article("testblog", self.article)
        self.app._driver.assert_not_called()

    def test_matching_final_context_hash_preserves_internal_sentence_spacing(self):
        self.article["paragraphs"][0] = "첫 문장입니다.\n\n둘째 문장입니다."
        self._set_reviewed_content_hash()
        image = self.article["images"][0]
        image.update({"requires_final_semantic_review": False,
                      "reviewed_paragraph_sha256": hashlib.sha256(self.article["paragraphs"][0].encode("utf-8")).hexdigest()})
        _title, sections, checked_images = self.app._validate_publish_article(self.article)
        self.assertEqual(sections[0], self.article["paragraphs"][0])
        self.assertEqual(checked_images[0]["reviewed_paragraph_sha256"], image["reviewed_paragraph_sha256"])
        self.article["paragraphs"][0] = self.article["paragraphs"][0].replace("\n\n", "\n")
        self._set_reviewed_content_hash()
        with self.assertRaisesRegex(ValueError, "본문 내용이 변경"):
            self.app._validate_publish_article(self.article)

    def test_changed_title_after_review_is_rejected(self):
        self.article["title"] += " (수정)"
        with self.assertRaisesRegex(ValueError, "제목 또는 본문이 변경"):
            self.app.publish_naver_article("testblog", self.article)
        self.app._driver.assert_not_called()

    def test_missing_fact_approval_or_ready_flag_is_rejected(self):
        self.article["ready_to_publish"] = False
        with self.assertRaisesRegex(ValueError, "발행 준비"):
            self.app.publish_naver_article("testblog", self.article)
        self.article["ready_to_publish"] = True
        self.article["reviews"][0]["review"]["facts_verified"] = False
        with self.assertRaisesRegex(ValueError, "본문 사실"):
            self.app.publish_naver_article("testblog", self.article)
        self.app._driver.assert_not_called()

    def test_published_page_inspection_reads_eight_sections_and_image_ids_without_writes(self):
        self.app._article_native_bold_rendered = MagicMock(return_value=True)
        ids = [f"image-{i}" for i in range(6)]
        self.driver.execute_script.return_value = {
            "sections": list(self.article["paragraphs"]),
            "images": [{"id": value, "position": image["paragraph_index"]}
                       for value, image in zip(ids, self.article["images"])],
        }
        result = NaverAutomation.inspect_published_naver_article(self.app, "testblog", self.article, expected_image_ids=ids)
        self.assertTrue(result["verified"])
        self.assertEqual(result["section_count"], 8)
        self.assertEqual(result["image_count"], 6)
        self.assertTrue(result["image_identity_matches"])
        self.driver.get.assert_not_called()
        self.app._prepare_article_in_writer.assert_not_called()
        self.app._find_publish_control.assert_not_called()
        self.final.click.assert_not_called()

    def test_published_page_inspection_rejects_missing_text_or_wrong_image_position(self):
        self.app._article_native_bold_rendered = MagicMock(return_value=True)
        self.driver.execute_script.return_value = {
            "sections": list(self.article["paragraphs"]),
            "images": [{"id": f"image-{i}", "position": image["paragraph_index"]}
                       for i, image in enumerate(self.article["images"])],
        }
        self.driver.execute_script.return_value["images"][0]["position"] = 7
        self.assertFalse(NaverAutomation.inspect_published_naver_article(self.app, "testblog", self.article)["verified"])
        self.driver.execute_script.return_value["sections"].pop()
        self.assertFalse(NaverAutomation.inspect_published_naver_article(self.app, "testblog", self.article)["verified"])


class CommentSubmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = NaverAutomation(self.root, lambda _: None)
        self.driver = MagicMock(current_url="https://blog.naver.com/neighbor/123456789012")
        self.phrase = "정성스러운 글 잘 읽었습니다."
        self.record = {"id": "200", "author": "https://blog.naver.com/owner", "text": self.phrase}
        self.mocks = {}
        values = {"_fill_comment_editor": MagicMock(), "_comment_records": [{"id": "100"}],
                  "_visible_comment_upload": MagicMock(), "_visible_comment_editor": MagicMock(),
                  "_comment_editor_value": self.phrase, "_new_comment_record": self.record}
        for name, value in values.items():
            handle = patch.object(NaverAutomation, name, return_value=value)
            self.mocks[name] = handle.start()
            self.addCleanup(handle.stop)
        handle = patch("naver_automation.WebDriverWait", ImmediateWait)
        handle.start()
        self.addCleanup(handle.stop)

    def test_uncertain_comment_survives_restart_and_never_submits_new_random_phrase(self):
        self.mocks["_new_comment_record"].return_value = None
        with self.assertRaisesRegex(RuntimeError, "등록을 시도"):
            self.app._submit_comment_once(self.driver, self.phrase, "owner")
        receipt_path = next((self.root / "comment_receipts").glob("*.json"))
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["status"], "uncertain")
        restarted = NaverAutomation(self.root, lambda _: None)
        self.driver.current_url = "https://m.blog.naver.com/PostView.naver?logNo=123456789012&blogId=neighbor"
        with self.assertRaisesRegex(RuntimeError, "이전 댓글 등록 결과"):
            restarted._submit_comment_once(self.driver, "새로운 랜덤 문구", "owner")
        self.driver.execute_script.assert_called_once()
        self.mocks["_fill_comment_editor"].assert_called_once()
        self.mocks["_new_comment_record"].assert_called_with(self.driver, {"100"}, self.phrase, "owner", "")
        self.mocks["_new_comment_record"].return_value = self.record
        result = restarted._submit_comment_once(self.driver, "새로운 랜덤 문구", "owner")
        self.assertTrue(result["reused_receipt"])
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["status"], "confirmed")
        self.driver.execute_script.assert_called_once()

    def test_comment_receipt_is_durable_before_click_and_success_is_reusable(self):
        def click(*_):
            receipt = json.loads(next((self.root / "comment_receipts").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "uncertain")
            self.assertEqual(receipt["before_ids"], ["100"])
        self.driver.execute_script.side_effect = click
        self.app._submit_comment_once(self.driver, self.phrase, "owner", "parent123")
        result = self.app._submit_comment_once(self.driver, "다음 문구", "owner", "parent123")
        self.assertTrue(result["reused_receipt"])
        self.driver.execute_script.assert_called_once()

    def test_stop_during_comment_entry_or_upload_wait_prevents_submission(self):
        for target in ("_fill_comment_editor", "_visible_comment_upload"):
            with self.subTest(target=target):
                self.app.reset_stop()
                self.mocks[target].side_effect = lambda *_: (self.app.stop_event.set() or MagicMock())
                with self.assertRaisesRegex(RuntimeError, "중지"):
                    self.app._submit_comment_once(self.driver, self.phrase, "owner")
                self.mocks[target].side_effect = None
                self.driver.execute_script.assert_not_called()
                self.assertFalse((self.root / "comment_receipts").exists())

    def test_early_comment_failure_remains_retryable_and_corrupt_receipt_does_not_click(self):
        self.mocks["_fill_comment_editor"].side_effect = RuntimeError("editor missing")
        with self.assertRaises(RuntimeError):
            self.app._submit_comment_once(self.driver, self.phrase, "owner")
        self.assertFalse((self.root / "comment_receipts").exists())
        self.mocks["_fill_comment_editor"].side_effect = None
        self.app._submit_comment_once(self.driver, self.phrase, "owner")
        path = next((self.root / "comment_receipts").glob("*.json"))
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "이전 댓글 등록 기록"):
            self.app._submit_comment_once(self.driver, self.phrase, "owner")
        self.driver.execute_script.assert_called_once()

    def test_stopped_comment_workers_do_not_clear_stop_or_open_browser(self):
        self.app._driver = MagicMock()
        self.app.stop_event.set()
        self.app.run_own_posts("owner", 3, 1)
        self.app.run_neighbor_posts("owner", 1, 2)
        self.assertTrue(self.app.stop_event.is_set())
        self.app._driver.assert_not_called()

    def test_existing_reply_uses_exact_author_identity(self):
        comment, reply, profile = MagicMock(), MagicMock(), MagicMock()
        comment.find_elements.return_value = [reply]
        reply.find_elements.return_value = [profile]
        for href, expected in (("https://blog.naver.com/owner_other", False),
                               ("https://example.com/owner", False),
                               ("https://blog.naver.com/OWNER", True),
                               ("https://m.blog.naver.com/PostList.naver?blogId=owner", True)):
            with self.subTest(href=href):
                profile.get_attribute.return_value = href
                self.assertIs(NaverAutomation._own_reply_exists(comment, "owner"), expected)


class WhaleOwnershipTests(unittest.TestCase):
    def test_command_profile_must_be_exact_even_with_quoted_spaces(self):
        with tempfile.TemporaryDirectory(prefix="blog owner ") as folder:
            app = NaverAutomation(Path(folder), lambda _: None)
            profile = (Path(folder) / "naver-whale-profile").resolve()
            base = [r"C:\Program Files\Naver\whale.exe", f"--user-data-dir={profile}", "--remote-debugging-port=9222"]
            self.assertEqual(app._owned_whale_command_port(subprocess.list2cmdline(base)), 9222)
            cases = [base[:1] + [f"--user-data-dir={profile}-other", base[2]],
                     base + [f"--user-data-dir={profile}-other"],
                     base + ["--type=renderer"],
                     [base[0], f"--user-agent=fake --user-data-dir={profile}", base[2]],
                     [base[0], base[1], "--remote-debugging-port=65536"]]
            for command in cases:
                with self.subTest(command=command):
                    self.assertIsNone(app._owned_whale_command_port(subprocess.list2cmdline(command)))
            separated = [base[0], "--user-data-dir", str(profile), "--remote-debugging-port", "9222"]
            self.assertEqual(app._owned_whale_command_port(subprocess.list2cmdline(separated)), 9222)

    def test_other_profiles_ports_and_processes_are_never_probed(self):
        with tempfile.TemporaryDirectory() as folder:
            app = NaverAutomation(Path(folder), lambda _: None)
            profile = Path(folder) / "naver-whale-profile"
            processes = [{"ProcessId": pid, "CommandLine": subprocess.list2cmdline([
                r"C:\Whale\whale.exe", f"--user-data-dir={location}", f"--remote-debugging-port={port}"])}
                for pid, location, port in [(11, profile.with_name("other-task"), 9221), (22, profile, 9222)]]
            ports = "\n".join(f"TCP 127.0.0.1:{port} 0.0.0.0:0 LISTENING {pid}" for pid, port in [(11, 9221), (22, 9333), (22, 9222)])
            response = MagicMock()
            response.json.return_value = {"Browser": "Chrome/150.0.1.2", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/id"}
            with patch("naver_automation.subprocess.run", side_effect=[MagicMock(stdout=ports), MagicMock(stdout=json.dumps(processes))]), \
                 patch("naver_automation.requests.get", return_value=response) as request:
                self.assertEqual(app._find_running_automation_whale(), (9222, "150.0.1.2"))
            request.assert_called_once_with("http://127.0.0.1:9222/json/version", timeout=1)

    def test_missing_process_command_line_never_attaches(self):
        with tempfile.TemporaryDirectory() as folder:
            app = NaverAutomation(Path(folder), lambda _: None)
            ports = "TCP 127.0.0.1:9222 0.0.0.0:0 LISTENING 22"
            with patch("naver_automation.subprocess.run", side_effect=[MagicMock(stdout=ports), MagicMock(stdout='[{"ProcessId":22,"CommandLine":null}]')]), \
                 patch("naver_automation.requests.get") as request:
                self.assertIsNone(app._find_running_automation_whale())
            request.assert_not_called()


class ReferenceLicenseTests(unittest.TestCase):
    source = "https://commons.wikimedia.org/wiki/File:Example.jpg"
    image = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Example.jpg/800px-Example.jpg"

    def evidence(self, **changes):
        page = {"original_file_present": True, "author": "Author", "license_links": ["https://creativecommons.org/publicdomain/zero/1.0/"]}
        page.update(changes)
        return NaverAutomation._commons_license_evidence(self.source, self.image, page)

    def test_english_source_requires_observed_page_language_or_visible_english_description(self):
        for page, expected in [({"document_language": "en-US"}, True),
                               ({"document_language": "ko"}, False),
                               ({"document_language": "ko", "english_description_visible": True,
                                 "english_description": "An empty modern kitchen interior"}, True),
                               ({"document_language": "ko", "english_description_visible": False,
                                 "english_description": "An empty modern kitchen interior"}, False),
                               ({"document_language": "ko", "english_description_visible": True,
                                 "english_description": "한국어 설명"}, False)]:
            with self.subTest(page=page):
                result = NaverAutomation._reference_language_evidence(page, self.source + "?uselang=en")
                self.assertIs(result["english_source_verified"], expected)
                if expected:
                    self.assertEqual(result["source_language"], "en")
                    self.assertTrue(result["language_evidence"])

    def test_english_inspection_visits_english_commons_and_preserves_license_source_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            app = NaverAutomation(Path(folder), lambda _: None)
            driver = MagicMock()
            driver.current_window_handle = "search"
            driver.find_elements.return_value = [MagicMock()]
            driver.get.side_effect = lambda url: setattr(driver, "current_url", url)
            driver.execute_script.return_value = {"original_file_present": True, "author": "Example photographer",
                "license_links": ["https://creativecommons.org/publicdomain/zero/1.0/"], "document_language": "en"}
            source = self.source + "?oldid=123&uselang=ko"
            with patch("naver_automation.WebDriverWait", ImmediateWait):
                evidence = app._inspect_reference_license(driver, source, self.image, english_only=True)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(driver.get.call_args.args[0]).query)
            self.assertEqual(query, {"oldid": ["123"], "uselang": ["en"]})
            self.assertTrue(evidence["english_source_verified"])
            self.assertTrue(evidence["license_verified"])
            self.assertEqual(evidence["license_evidence_url"], source)
            self.assertEqual(evidence["language_evidence_url"], driver.get.call_args.args[0])
            driver.close.assert_called_once()
            driver.switch_to.window.assert_called_once_with("search")

    def test_english_query_validation_and_filters_do_not_assume_english_source(self):
        with tempfile.TemporaryDirectory() as folder:
            app = NaverAutomation(Path(folder), lambda _: None)
            app._driver = MagicMock()
            for query in ("한국 경제", "CPI 한국", "1234", ""):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    app.capture_google_reference_candidates(query, Path(folder), english_only=True)
            app._driver.assert_not_called()
            driver = app._driver.return_value
            driver.find_elements.return_value = []
            with patch("naver_automation.WebDriverWait", ImmediateWait):
                result = app.capture_google_reference_candidates("consumer price index", Path(folder), english_only=True)
            self.assertEqual(result, [])
            query = urllib.parse.parse_qs(urllib.parse.urlparse(driver.get.call_args.args[0]).query)
            self.assertEqual(query["hl"], ["en"])
            self.assertEqual(query["lr"], ["lang_en"])
            self.assertEqual(query["q"], ["consumer price index"])

    def test_file_specific_cc0_proof_sets_rights_and_no_required_attribution(self):
        result = self.evidence()
        self.assertTrue(result["license_verified"])
        self.assertTrue(result["commercial_use_allowed"])
        self.assertTrue(result["modification_allowed"])
        self.assertFalse(result["attribution_required"])

    def test_search_filter_or_unrelated_creative_commons_link_is_not_proof(self):
        page = {"license_filter": "Creative Commons", "license_links": ["https://creativecommons.org/publicdomain/zero/1.0/"]}
        self.assertFalse(NaverAutomation._commons_license_evidence("https://example.com/picture", self.image, page)["license_verified"])
        self.assertFalse(self.evidence(original_file_present=False)["license_verified"])
        self.assertFalse(self.evidence(license_links=[])["license_verified"])

    def test_mismatched_source_image_or_noncommercial_license_rejected(self):
        page = {"original_file_present": True, "license_links": ["https://creativecommons.org/publicdomain/zero/1.0/"]}
        self.assertFalse(NaverAutomation._commons_license_evidence(self.source, self.image.replace("Example", "Other"), page)["license_verified"])
        self.assertFalse(self.evidence(license_links=["https://creativecommons.org/licenses/by-nc/4.0/"])["license_verified"])

    def test_attribution_license_requires_known_author_and_records_requirements(self):
        license_url = "https://creativecommons.org/licenses/by/4.0/"
        self.assertFalse(self.evidence(author="", license_links=[license_url])["license_verified"])
        result = self.evidence(license_links=[license_url])
        self.assertTrue(result["attribution_required"])
        self.assertFalse(result["share_alike"])
        self.assertIn(self.source, result["attribution"])

    def test_share_alike_needs_separate_compliance_and_is_not_eligible(self):
        self.assertFalse(self.evidence(license_links=["https://creativecommons.org/licenses/by-sa/4.0/"])["license_verified"])

    def test_google_result_source_unwrap_and_wikimedia_file_suffix(self):
        wrapped = "https://www.google.com/imgres?imgrefurl=https%3A%2F%2Fcommons.wikimedia.org%2Fwiki%2FFile%3AExample.jpg"
        self.assertEqual(NaverAutomation._reference_source_url([wrapped]), self.source)
        self.assertEqual(NaverAutomation._reference_source_url([self.source]), self.source)
        self.assertEqual(NaverAutomation._reference_source_url(["https://example.com/image.jpg"]), "")

    def test_localized_creative_commons_deeds_preserve_license_conditions(self):
        for slug, required in (("publicdomain/zero/1.0", False),
                               ("publicdomain/mark/1.0", False), ("licenses/by/4.0", True)):
            with self.subTest(slug=slug):
                result = self.evidence(license_links=[f"https://creativecommons.org/{slug}/deed.en"])
                self.assertTrue(result["license_verified"])
                self.assertEqual(result["license_url"], f"https://creativecommons.org/{slug}/")
                self.assertIs(result["attribution_required"], required)
        for value in ("https://creativecommons.org/licenses/by-sa/4.0/deed.en",
                      "https://creativecommons.org/publicdomain/zero/1.0/unrelated",
                      "https://creativecommons.org.example.com/publicdomain/zero/1.0/deed.en"):
            self.assertFalse(self.evidence(license_links=[value])["license_verified"])

    def test_public_domain_template_requires_explicit_file_terms_and_matching_original(self):
        template = {"name": "Public domain", "link_required": "false", "attribution_required": "false"}
        page = {"original_file_present": True, "license_links": [], "public_domain_templates": [template]}
        result = NaverAutomation._commons_license_evidence(self.source, self.image, page)
        self.assertTrue(result["license_verified"])
        self.assertEqual(result["license_url"], self.source + "#Licensing")
        self.assertEqual(result["public_domain_template"], template)
        for field, value in (("name", "Free use"), ("name", "CC BY 4.0"),
                             ("link_required", "true"), ("attribution_required", "true"),
                             ("attribution_required", "")):
            with self.subTest(field=field, value=value):
                invalid = {**page, "public_domain_templates": [{**template, field: value}]}
                self.assertFalse(NaverAutomation._commons_license_evidence(self.source, self.image, invalid)["license_verified"])
        self.assertFalse(NaverAutomation._commons_license_evidence(self.source, self.image.replace("Example", "Other"), page)["license_verified"])
        self.assertFalse(NaverAutomation._commons_license_evidence(self.source, self.image, {**page, "original_file_present": False})["license_verified"])

    def test_preview_resize_preserves_entire_aspect_ratio(self):
        source = Image.new("RGB", (400, 600), "white")
        enlarged = NaverAutomation._gently_enhance_google_image(source)
        self.assertAlmostEqual(enlarged.width / enlarged.height, 2 / 3, places=3)
        self.assertGreater(enlarged.height, source.height)

    def test_captures_first_two_image_elements_without_download_or_rights_assumption(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = NaverAutomation(Path(temporary), lambda message: None)
            driver = MagicMock()
            app._driver = MagicMock(return_value=driver)
            app._inspect_reference_license = MagicMock(return_value={"license_verified": False, "license_url": "", "attribution": ""})
            thumbnails = [MagicMock() for _ in range(3)]
            previews = [MagicMock() for _ in range(3)]
            loading_placeholder = MagicMock()
            loading_placeholder.is_displayed.side_effect = StaleElementReferenceException("Google replaced placeholder")
            selection = {"index": 0}
            for index, (thumb, preview) in enumerate(zip(thumbnails, previews)):
                thumb.is_displayed.return_value = True
                thumb.rect = {"width": 200, "height": 150}
                preview.is_displayed.return_value = True
                preview.get_attribute.return_value = "original-css"
                buffer = io.BytesIO()
                Image.new("RGB", (400, 240), (index * 30, 50, 70)).save(buffer, "PNG")
                preview.screenshot_as_png = buffer.getvalue()
            def find_elements(_by, selector):
                return thumbnails if "div[data-img-wrapper]" in selector else [loading_placeholder, previews[selection["index"]]]
            def script(source, *arguments):
                if "scrollIntoView" in source:
                    selection["index"] = thumbnails.index(arguments[0])
                elif "complete:e.complete" in source:
                    return {"width": 800, "height": 480, "display_width": 400, "display_height": 240,
                            "url": f"https://example.com/image-{selection['index']}.jpg", "complete": True}
                elif "const links=[]" in source:
                    return [f"https://example.com/photo-{selection['index']}.html"]
                return None
            driver.find_elements.side_effect = find_elements
            driver.execute_script.side_effect = script
            with patch("naver_automation.WebDriverWait", ImmediateWait), patch.object(app, "_download_google_preview") as download:
                candidates = app.capture_google_reference_candidates("예시", Path(temporary), 2)
            self.assertEqual([item["search_rank"] for item in candidates], [1, 2])
            self.assertTrue(all(item["capture_method"] == "image_element_screenshot" for item in candidates))
            self.assertTrue(all(item["license_verified"] is False and item["vision_reviewed"] is False for item in candidates))
            self.assertTrue(all(item["capture_width"] == 400 for item in candidates))
            self.assertTrue(all(Path(item["path"]).is_file() for item in candidates))
            self.assertTrue(all(item["capture_sha256"] == hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
                                for item in candidates))
            download.assert_not_called()

    def test_reuse_only_skips_ineligible_results_and_stops_after_sixty_candidates(self):
        reusable = {"license_verified": True, "commercial_use_allowed": True, "modification_allowed": True,
                    "attribution_required": False, "share_alike": False,
                    "license_url": "https://creativecommons.org/publicdomain/zero/1.0/"}
        scenarios = [([{}, {**reusable, "attribution_required": True}, reusable, reusable], [3, 4], 4, 2, False, 0),
                     ([{}] * 61, [], 60, 4, False, 0),
                     ([reusable] * 12, list(range(1, 11)), 10, 20, False, 0),
                     ([reusable], [1], 1, 1, False, 61),
                     ([{**reusable, "english_source_verified": False},
                       {**reusable, "english_source_verified": True, "source_language": "en"}], [2], 2, 1, True, 0)]
        for rights, expected_ranks, expected_checks, requested, english, small_count in scenarios:
            with self.subTest(expected_ranks=expected_ranks), tempfile.TemporaryDirectory() as folder:
                app = NaverAutomation(Path(folder), lambda _: None)
                driver = MagicMock()
                app._driver = MagicMock(return_value=driver)
                thumbnails = [MagicMock() for _ in rights]
                preview = MagicMock()
                preview.is_displayed.return_value = True
                preview.get_attribute.return_value = "original-css"
                buffer = io.BytesIO()
                Image.new("RGB", (400, 240), "green").save(buffer, "PNG")
                preview.screenshot_as_png = buffer.getvalue()
                selection = {"index": 0}
                for thumb in thumbnails:
                    thumb.is_displayed.return_value = True
                    thumb.rect = {"width": 200, "height": 150}
                small_icons = [MagicMock() for _ in range(small_count)]
                for icon in small_icons:
                    icon.is_displayed.return_value = True
                    icon.rect = {"width": 45, "height": 45}
                driver.find_elements.side_effect = lambda _by, selector: small_icons + thumbnails if "div[data-img-wrapper]" in selector else [preview]
                def script(source, *arguments):
                    if "scrollIntoView" in source:
                        selection["index"] = thumbnails.index(arguments[0])
                    elif "complete:e.complete" in source:
                        return {"width": 800, "height": 480, "display_width": 400, "display_height": 240,
                                "url": f"https://upload.wikimedia.org/image-{selection['index']}.jpg", "complete": True}
                    elif "const links=[]" in source:
                        return [f"https://commons.wikimedia.org/wiki/File:Example{selection['index']}.jpg"]
                driver.execute_script.side_effect = script
                app._inspect_reference_license = MagicMock(side_effect=lambda *_, **kwargs: rights[selection["index"]])
                with patch("naver_automation.WebDriverWait", ImmediateWait), patch.object(app, "_download_google_preview") as download:
                    keyword = "sample photos" if english else "예시 사진"
                    captured = app.capture_google_reference_candidates(keyword, Path(folder), requested,
                                                                       reuse_only=True, english_only=english)
                self.assertEqual([item["search_rank"] for item in captured], expected_ranks)
                self.assertEqual(app._inspect_reference_license.call_count, expected_checks)
                diagnostics = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
                self.assertEqual(diagnostics["scanned_count"], expected_checks)
                self.assertEqual(diagnostics["rejection_counts"].get("thumbnail_too_small", 0), small_count)
                self.assertEqual(diagnostics["photo_candidates"][0]["rank"], 1)
                self.assertEqual(diagnostics["photo_candidates"][0]["dom_index"], small_count + 1)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(driver.get.call_args.args[0]).query)
                self.assertEqual(query["q"], [keyword + " site:commons.wikimedia.org"])
                if english:
                    self.assertEqual(query["hl"], ["en"])
                    self.assertEqual(query["lr"], ["lang_en"])
                    self.assertTrue(all(item["english_source_verified"] and item["source_language"] == "en" for item in captured))
                    self.assertTrue(all(call.kwargs == {"english_only": True} for call in app._inspect_reference_license.call_args_list))
                self.assertTrue(all(item["reuse_only"] and item["vision_reviewed"] is False for item in captured))
                self.assertEqual(len(list(Path(folder).glob("google_reference_*.jpg"))), len(expected_ranks))
                download.assert_not_called()


class ReferenceCaptureLoadingTests(unittest.TestCase):
    class PollingWait:
        def __init__(self, driver, *_args, **_kwargs):
            self.driver = driver

        def until(self, callback):
            for _ in range(8):
                result = callback(self.driver)
                if result:
                    return result
            raise TimeoutException("Offline bounded polling ended")

    def setup_capture(self, folder, *, loading=True, icons_only=False, preview_error="", cancel="", photo_count=2, blocked_previews=()):
        app = NaverAutomation(Path(folder), lambda _: None)
        driver = MagicMock()
        driver.current_url = "https://www.google.com/search?q=wallet&token=SECRET#session"
        driver.title = "Google photo results"
        app._driver = MagicMock(return_value=driver)
        app._inspect_reference_license = MagicMock(return_value={"license_verified": False})
        icon = MagicMock()
        icon.is_displayed.return_value = True
        icon.rect = {"width": 16, "height": 16}
        photos, previews = [], []
        for index in range(photo_count):
            photo = MagicMock()
            photo.is_displayed.return_value = True
            photo.rect = {"width": 220, "height": 150}
            attrs = {"id": f"dimg_{index}", "alt": f"Wallet photo {index}",
                     "src": f"https://user:password@example.com/photo{index}.jpg?access_token=SECRET#private"}
            photo.get_attribute.side_effect = lambda name, values=attrs: values.get(name, "")
            photos.append(photo)
            preview = MagicMock()
            preview.is_displayed.return_value = True
            preview.get_attribute.return_value = "original-css"
            buffer = io.BytesIO()
            Image.new("RGB", (400, 240), ((index * 40) % 256, 60, 80)).save(buffer, "PNG")
            preview.screenshot_as_png = buffer.getvalue()
            previews.append(preview)
        state = {"photo_polls": 0, "clicks": 0, "selected": 0}
        def find_elements(by, selector):
            if by == "id":
                return [photos[int(selector.removeprefix("dimg_"))]]
            if "div[data-img-wrapper]" in selector:
                state["photo_polls"] += 1
                if cancel == "loading":
                    app.stop_event.set()
                if icons_only or (loading and state["photo_polls"] == 1):
                    return [icon]
                if loading and state["photo_polls"] == 2:
                    return [icon, photos[0]]
                return [icon, *photos]
            if cancel == "preview":
                app.stop_event.set()
            if (preview_error == "always" or state["selected"] in blocked_previews
                    or (preview_error == "timeout" and state["clicks"] == 1)):
                return []
            return [previews[state["selected"]]]
        def execute(source, *arguments):
            if "scrollIntoView" in source:
                state["clicks"] += 1
                state["selected"] = photos.index(arguments[0])
                if preview_error == "stale" and state["clicks"] == 1:
                    raise StaleElementReferenceException("Photo node replaced")
            elif "complete:e.complete" in source:
                return {"width": 800, "height": 480, "display_width": 400, "display_height": 240,
                        "url": f"https://example.com/photo{state['selected']}.jpg?token=SECRET", "complete": True}
            elif "const links=[]" in source:
                return [f"https://example.com/photo{state['selected']}.html?token=SECRET"]
            elif "Object.fromEntries" in source:
                return {"img.sFlh5c": 0, "img.iPVvYb": 1, "img.n3VNCb": 0, "div[role='dialog'] img": 0}
        driver.find_elements.side_effect = find_elements
        driver.execute_script.side_effect = execute
        return app, driver, state

    def test_waits_for_late_photos_and_stable_list_instead_of_icon_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            app, driver, state = self.setup_capture(folder)
            with patch("naver_automation.WebDriverWait", self.PollingWait):
                images = app.capture_google_reference_candidates("wallet coins photograph", Path(folder), count=2)
            self.assertEqual(len(images), 2)
            self.assertGreaterEqual(state["photo_polls"], 5)
            record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual([sample["photos"] for sample in record["result_load_samples"][:3]], [0, 1, 2])
            self.assertTrue(record["results_stabilized"])
            self.assertEqual(record["final_url"], "https://www.google.com/search")
            self.assertEqual(record["final_title"], "Google photo results")
            self.assertEqual(record["photo_candidates"][0]["src"], "https://example.com/photo0.jpg")
            self.assertEqual(record["photo_candidates"][0]["id"], "dimg_0")
            self.assertEqual(record["preview_attempts"][0]["selector_counts"]["img.iPVvYb"], 1)
            serialized = json.dumps(record)
            for private in ("SECRET", "password", "access_token", "#session"):
                self.assertNotIn(private, serialized)

    def test_icon_only_results_never_open_a_preview_and_wait_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            app, _, state = self.setup_capture(folder, icons_only=True)
            with patch("naver_automation.WebDriverWait", self.PollingWait):
                self.assertEqual(app.capture_google_reference_candidates("wallet coins photograph", Path(folder)), [])
            self.assertEqual(state["clicks"], 0)
            self.assertEqual(state["photo_polls"], 8)
            app._inspect_reference_license.assert_not_called()
            record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "no_results")
            self.assertFalse(record["results_stabilized"])
            self.assertEqual(record["eligible_thumbnails"], 0)

    def test_preview_timeout_or_stale_click_retries_the_same_photo_once(self):
        for error in ("timeout", "stale", "always"):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                app, _, state = self.setup_capture(folder, loading=False, preview_error=error)
                with patch("naver_automation.WebDriverWait", self.PollingWait):
                    images = app.capture_google_reference_candidates("wallet coins photograph", Path(folder), count=1)
                record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
                self.assertEqual(len(images), 0 if error == "always" else 1)
                self.assertEqual(state["clicks"], 4 if error == "always" else 2)
                attempts = [attempt for attempt in record["preview_attempts"] if attempt["rank"] == 1]
                self.assertEqual([attempt["attempt"] for attempt in attempts], [1, 2])
                self.assertTrue(all(attempt['dom_index'] == 2 for attempt in attempts))
                self.assertEqual([attempt["status"] for attempt in attempts], ["retry", "failed" if error == "always" else "ready"])

    def test_stop_during_loading_or_preview_never_retries_or_checks_license(self):
        for phase in ("loading", "preview"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                app, _, state = self.setup_capture(folder, cancel=phase)
                with patch("naver_automation.WebDriverWait", self.PollingWait), self.assertRaisesRegex(RuntimeError, "중지"):
                    app.capture_google_reference_candidates("wallet coins photograph", Path(folder), count=1)
                self.assertLessEqual(state["clicks"], 1)
                app._inspect_reference_license.assert_not_called()
                record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "cancelled")

    def test_navigation_error_is_recorded_without_credentials_or_query_values(self):
        with tempfile.TemporaryDirectory() as folder:
            app, driver, _ = self.setup_capture(folder)
            driver.get.side_effect = WebDriverException("Blocked https://user:password@example.com/search?token=SECRET")
            with self.assertRaises(WebDriverException):
                app.capture_google_reference_candidates("wallet coins photograph", Path(folder))
            record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "search_error")
            self.assertEqual(record["rejection_counts"], {"search_navigation_error": 1})
            self.assertNotIn("SECRET", json.dumps(record))
            self.assertNotIn("password", json.dumps(record))
            self.assertEqual(NaverAutomation._reference_diagnostic_url("data:image/png;base64,private"), "data:(omitted)")
            self.assertEqual(NaverAutomation._reference_diagnostic_url("file:///C:/Users/private/photo.jpg"), "file:(omitted)")

    def test_three_failed_previews_end_query_before_remaining_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            app, _, state = self.setup_capture(folder, loading=False, photo_count=8, preview_error="always")
            with patch("naver_automation.WebDriverWait", self.PollingWait):
                images = app.capture_google_reference_candidates("wallet coins photograph", Path(folder), count=4)
            record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(images, [])
            self.assertEqual(record["status"], "preview_unavailable")
            self.assertEqual(record["consecutive_preview_failures"], 3)
            self.assertEqual(record["scanned_count"], 3)
            self.assertEqual(state["clicks"], 6)
            app._inspect_reference_license.assert_not_called()
            self.assertFalse(app.stop_event.is_set())

    def test_loaded_preview_resets_failures_even_when_license_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            app, _, state = self.setup_capture(folder, loading=False, photo_count=8, blocked_previews={0, 1, 3, 4, 5})
            with patch("naver_automation.WebDriverWait", self.PollingWait):
                images = app.capture_google_reference_candidates("wallet coins photograph", Path(folder), count=4, reuse_only=True)
            record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(images, [])
            self.assertEqual(record["status"], "preview_unavailable")
            self.assertEqual(record["scanned_count"], 6)
            self.assertEqual(record["rejection_counts"]["reuse_rights_unverified"], 1)
            self.assertEqual(record["consecutive_preview_failures"], 3)
            self.assertEqual(app._inspect_reference_license.call_count, 1)

    def test_license_rejections_do_not_trigger_preview_failure_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            app, _, state = self.setup_capture(folder, loading=False, photo_count=7)
            with patch("naver_automation.WebDriverWait", self.PollingWait):
                self.assertEqual(app.capture_google_reference_candidates("wallet coins photograph", Path(folder), count=4, reuse_only=True), [])
            record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "no_eligible_candidates")
            self.assertEqual(record["scanned_count"], 7)
            self.assertEqual(record["consecutive_preview_failures"], 0)
            self.assertEqual(app._inspect_reference_license.call_count, 7)

    def test_query_time_budget_preserves_captured_files_and_returns_partial_without_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            app, _, state = self.setup_capture(folder, loading=False, photo_count=4)
            with patch("naver_automation.WebDriverWait", self.PollingWait), \
                 patch("naver_automation.time.monotonic", side_effect=[0, 0, 91, 91]):
                images = app.capture_google_reference_candidates("wallet coins photograph", Path(folder), count=4)
            record = json.loads((Path(folder) / "google_reference_diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(len(images), 1)
            self.assertTrue(Path(images[0]["path"]).is_file())
            self.assertEqual(record["status"], "time_budget_exhausted")
            self.assertEqual(record["candidate_elapsed_seconds"], 91)
            self.assertEqual(record["scanned_count"], 1)
            self.assertEqual(state["clicks"], 1)
            self.assertFalse(app.stop_event.is_set())
            self.assertEqual(json.loads((Path(folder) / "google_reference_manifest.json").read_text(encoding="utf-8")), images)


class PublishControlTests(unittest.TestCase):
    def test_observed_css_module_publish_panel_without_dialog_role_finds_final_button(self):
        # Minimal actual 2026-09-12 Naver DOM: CSS-module suffixes, no role=dialog.
        html = '''<div><button class="publish_btn__v_kS9"><span>발행</span></button>
          <div class="layer_popup__VVWW8 is_show__LbCeK"><div class="layer_publish__Jv1Pu">
            <div class="layer_content_set_publish__hKFQ5"><p>카테고리 공개 설정 발행 설정</p>
              <div class="btn_area__koq5u"><button type="button" class="confirm_btn__byZZW"
                data-testid="seOnePublishBtn" data-click-area="tpb*i.publish"><span>발행</span></button></div>
            </div></div></div></div>'''
        tree = ET.fromstring(html)
        parents = {child: parent for parent in tree.iter() for child in parent}
        buttons = []
        for node in tree.iter("button"):
            button = MagicMock()
            button.node = node
            button.text = "".join(node.itertext())
            button.is_displayed.return_value = True
            button.is_enabled.return_value = True
            buttons.append(button)
        driver = MagicMock()
        driver.find_elements.return_value = buttons
        def matches(node, selector):
            selector = selector.strip()
            if selector.startswith("."):
                return selector[1:] in node.get("class", "").split()
            attr = re.fullmatch(r'\[([\w-]+)(\*?=)"([^"]+)"\]', selector)
            if not attr:
                return False
            key, operator, wanted = attr.groups()
            actual = node.get(key, "")
            return wanted in actual if operator == "*=" else actual == wanted
        def evaluate_scope(script, button):
            # Apply the production closest() selector to the parsed HTML ancestry.
            selectors = re.search(r"e\.closest\('([^']+)'\)", script).group(1).split(",")
            node = button.node
            while node is not None:
                if any(matches(node, selector) for selector in selectors):
                    return {"inside": True, "settings": bool(re.search("공개|카테고리|발행 설정|주제", "".join(node.itertext())))}
                node = parents.get(node)
            return {"inside": False, "settings": False}
        driver.execute_script.side_effect = evaluate_scope
        with patch.object(NaverAutomation, "_find_across_frames", side_effect=lambda _d, finder: finder()):
            self.assertIs(NaverAutomation._find_publish_control(driver, final=False), buttons[0])
            self.assertIs(NaverAutomation._find_publish_control(driver, final=True), buttons[1])
            tree.find(".//p").text = "알 수 없는 안내"
            self.assertIsNone(NaverAutomation._find_publish_control(driver, final=True))

    def test_recovery_prompt_declines_only_known_restore_action(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = NaverAutomation(Path(temporary), lambda message: None)
            driver, dialog, cancel = MagicMock(), MagicMock(), MagicMock()
            dialog.text = "작성 중인 글이 있습니다. 이전 내용을 이어서 작성하시겠습니까?"
            cancel.text = "취소"
            shown = {"value": True}
            dialog.is_displayed.side_effect = lambda: shown["value"]
            cancel.is_displayed.return_value = True
            cancel.is_enabled.return_value = True
            cancel.click.side_effect = lambda: shown.update(value=False)
            dialog.find_elements.return_value = [cancel]
            driver.find_elements.return_value = [dialog]
            with patch("naver_automation.WebDriverWait", ImmediateWait):
                app._handle_writer_recovery_prompt(driver)
            cancel.click.assert_called_once()
            self.assertFalse(shown["value"])

    def test_unknown_writer_modal_is_never_dismissed(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = NaverAutomation(Path(temporary), lambda message: None)
            driver, dialog = MagicMock(), MagicMock()
            dialog.text = "다른 안내: 기존 글을 삭제하시겠습니까?"
            dialog.is_displayed.return_value = True
            driver.find_elements.return_value = [dialog]
            with self.assertRaisesRegex(RuntimeError, "알 수 없는"):
                app._handle_writer_recovery_prompt(driver)
            dialog.find_elements.assert_not_called()

    def test_rendered_bold_requires_matching_visible_runs_not_only_model_flags(self):
        sections = ["❝소제목❞\n\n본문 강조입니다."] * 8
        driver = MagicMock()
        driver.execute_script.return_value = [[
            [{"value": "❝소제목❞", "bold": True}],
            [{"value": "\u200b", "bold": False}],
            [{"value": "본문 ", "bold": False}, {"value": "강조", "bold": True}, {"value": "입니다.", "bold": False}],
        ] for _ in range(8)]
        self.assertTrue(NaverAutomation._article_native_bold_rendered(driver, sections, ["강조"]))
        driver.execute_script.return_value[0][0][0]["bold"] = False
        self.assertFalse(NaverAutomation._article_native_bold_rendered(driver, sections, ["강조"]))

    def test_final_control_must_be_in_publication_settings_and_unique(self):
        driver = MagicMock()
        opener, final, unrelated = MagicMock(), MagicMock(), MagicMock()
        for button in (opener, final, unrelated):
            button.is_displayed.return_value = True
            button.is_enabled.return_value = True
        driver.find_elements.return_value = [opener, final, unrelated]
        def scope(_script, button):
            return {"inside": button is not opener, "settings": button is final}
        driver.execute_script.side_effect = scope
        with patch.object(NaverAutomation, "_find_across_frames", side_effect=lambda _d, finder: finder()):
            self.assertIs(NaverAutomation._find_publish_control(driver), opener)
            self.assertIs(NaverAutomation._find_publish_control(driver, final=True), final)
            driver.find_elements.return_value = [final, final]
            self.assertIsNone(NaverAutomation._find_publish_control(driver, final=True))

    def test_post_confirmation_requires_own_post_url_and_visible_exact_title(self):
        driver = MagicMock()
        heading = MagicMock()
        heading.is_displayed.return_value = True
        heading.text = "테스트 제목"
        driver.find_elements.return_value = [heading]
        with patch.object(NaverAutomation, "_find_across_frames", side_effect=lambda _d, finder: finder()):
            for url in ["https://blog.naver.com/testblog/postwrite", "https://blog.naver.com/otherblog/123456789012", "https://not-naver.example/testblog/123456789012"]:
                driver.current_url = url
                self.assertEqual(NaverAutomation._published_article_url(driver, "testblog", "테스트 제목"), "")
            driver.current_url = "https://blog.naver.com/testblog/123456789012"
            self.assertEqual(NaverAutomation._published_article_url(driver, "testblog", "다른 제목"), "")
            self.assertEqual(NaverAutomation._published_article_url(driver, "testblog", "테스트 제목"), driver.current_url)
            heading.is_displayed.return_value = False
            self.assertEqual(NaverAutomation._published_article_url(driver, "testblog", "테스트 제목"), "")


if __name__ == "__main__":
    unittest.main()
