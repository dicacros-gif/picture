"""Offline decision tests; never inspect, generate, or publish live images."""
import copy
import unittest

from blog_image_review import (BASE_HARD_FLAGS, COVER_HARD_FLAGS, IMAGE_REVIEW_PROTOCOL,
                               evaluate_image_review, image_review_classification_prompt,
                               image_review_classification_schema)


def valid_review(*, classified=False, headline="", caption=""):
    review = {"approved": True, "quality_score": 90, "issues": [], "text_free": True,
              **{flag: True for flag in BASE_HARD_FLAGS}}
    if headline:
        review.update({flag: True for flag in COVER_HARD_FLAGS})
        review.update(text_free=False, detected_text=headline)
    elif caption:
        review.update(text_free=False, caption_exact=True, caption_legible=True,
                      no_other_text=True, detected_text=caption)
    if classified:
        review.update(image_review_classification_schema())
    return review


def add_minor_note(review, *, code="composition", detail="구도가 다소 평범합니다."):
    review["quality_notes"] = [{"code": code, "severity": "minor", "detail": detail}]
    review["issues"] = [detail]
    return review


class ImageReviewDecisionTests(unittest.TestCase):
    def check(self, review, **kwargs):
        return evaluate_image_review(review, actual_image_attached=True, **kwargs)

    def test_legacy_valid_review_keeps_strict_behavior(self):
        review = valid_review()
        decision = self.check(review)
        self.assertTrue(decision["approved"])
        self.assertEqual(decision["policy"], "legacy-strict")
        self.assertFalse(decision["relaxed_quality"])
        for update in ({"quality_score": 74}, {"issues": ["구도가 평범합니다."]}, {"approved": False}):
            with self.subTest(update=update):
                self.assertFalse(self.check({**review, **update})["approved"])

    def test_classified_minor_notes_allow_65_to_74_without_rewriting_cli_approval(self):
        for score in (65, 70.5, 74, 75, 100):
            for code in ("composition", "fine_grain", "minor_quality"):
                with self.subTest(score=score, code=code):
                    review = add_minor_note(valid_review(classified=True), code=code)
                    review["quality_score"] = score
                    decision = self.check(review)
                    self.assertTrue(decision["approved"], decision["reasons"])
                    self.assertTrue(decision["relaxed_quality"])
                    self.assertIs(decision["raw_approved"], True)
                    self.assertEqual(decision["raw_review"], review)

    def test_new_protocol_does_not_promote_a_raw_cli_rejection(self):
        review = add_minor_note(valid_review(classified=True))
        review.update(approved=False, quality_score=80)
        decision = self.check(review)
        self.assertFalse(decision["approved"])
        self.assertIs(decision["raw_approved"], False)
        self.assertIs(decision["raw_review"]["approved"], False)

    def test_copyright_or_other_blocking_findings_cannot_be_softened(self):
        for concern in ("원본 사용권이 불명확합니다.", "워터마크가 보입니다.", "본문과 무관합니다."):
            with self.subTest(concern=concern):
                review = add_minor_note(valid_review(classified=True))
                review["blocking_issues"] = [concern]
                decision = self.check(review)
                self.assertFalse(decision["approved"])
                self.assertTrue(any(concern in reason for reason in decision["reasons"]))
        for code in ("copyright", "logo", "watermark", "text_accuracy", "photorealism"):
            with self.subTest(code=code):
                review = add_minor_note(valid_review(classified=True), code=code)
                self.assertFalse(self.check(review)["approved"])

    def test_every_existing_hard_condition_still_requires_literal_true(self):
        base = add_minor_note(valid_review(classified=True))
        for flag in (*BASE_HARD_FLAGS, "text_free"):
            for value in (False, None, 1, "true"):
                with self.subTest(flag=flag, value=value):
                    decision = self.check({**base, flag: value})
                    self.assertFalse(decision["approved"])
                    self.assertTrue(any(flag in reason for reason in decision["reasons"]))

    def test_cover_text_faces_and_typography_are_not_minor_composition_issues(self):
        headline = "왜 다를까?"
        base = add_minor_note(valid_review(classified=True, headline=headline))
        for flag in COVER_HARD_FLAGS:
            with self.subTest(flag=flag):
                self.assertFalse(self.check({**base, flag: False}, expected_headline=headline)["approved"])
        for detected in ("왜 다를까", "왜 다를가?", headline + " 추가 글자"):
            with self.subTest(detected=detected):
                self.assertFalse(self.check({**base, "detected_text": detected}, expected_headline=headline)["approved"])
        self.assertTrue(self.check({**base, "detected_text": "왜\n다를까?"}, expected_headline=headline)["approved"])

    def test_captioned_reference_keeps_ocr_rules_without_cover_requirements(self):
        caption = "신청 기준"
        review = add_minor_note(valid_review(classified=True, caption=caption))
        self.assertTrue(self.check(review, expected_caption=caption, actual_image_size=(1600, 900))["approved"])
        for flag in ("caption_exact", "caption_legible", "no_other_text"):
            with self.subTest(flag=flag):
                self.assertFalse(self.check({**review, flag: False}, expected_caption=caption)["approved"])
        self.assertFalse(self.check({**review, "detected_text": "신청 기준 2026"}, expected_caption=caption)["approved"])
        self.assertFalse(self.check({**review, "text_free": True}, expected_caption=caption)["approved"])

    def test_actual_geometry_overrules_only_the_model_square_flag_with_diagnostic(self):
        headline = "핵심 기준"
        review = valid_review(classified=True, headline=headline)
        review["square_1_to_1"] = False
        decision = self.check(review, expected_headline=headline, actual_image_size=(2048, 2048))
        self.assertTrue(decision["approved"], decision["reasons"])
        self.assertIs(review["square_1_to_1"], False)
        self.assertIs(decision["raw_review"]["square_1_to_1"], False)
        self.assertEqual(decision["diagnostics"], [{"code": "decoded_geometry", "width": 2048, "height": 2048,
                         "raw_square_1_to_1": False, "square_1_to_1": True}])
        self.assertFalse(self.check(review, expected_headline=headline)["approved"])
        self.assertFalse(self.check({**review, "square_1_to_1": True}, expected_headline=headline,
                                    actual_image_size=(2048, 1536))["approved"])
        self.assertFalse(self.check({**review, "approved": False}, expected_headline=headline,
                                    actual_image_size=(2048, 2048))["approved"])
        self.assertFalse(self.check({**review, "no_human_face": False}, expected_headline=headline,
                                    actual_image_size=(2048, 2048))["approved"])

    def test_malformed_decoded_dimensions_are_never_treated_as_square_proof(self):
        for size in ((True, True), (0, 0), (-1, -1), (2048.0, 2048), (2048,), "2048x2048", {"width": 2048}):
            with self.subTest(size=size):
                self.assertFalse(self.check(valid_review(), actual_image_size=size)["approved"])

    def test_actual_file_attachment_and_new_protocol_observation_are_required(self):
        review = valid_review(classified=True)
        self.assertFalse(evaluate_image_review(review)["approved"])
        self.assertFalse(evaluate_image_review(review, actual_image_attached=1)["approved"])
        for value in (False, None, 1):
            with self.subTest(observed=value):
                self.assertFalse(self.check({**review, "image_observed": value})["approved"])

    def test_missing_classification_does_not_opt_legacy_reply_into_lenient_policy(self):
        review = add_minor_note(valid_review(classified=True))
        review["quality_score"] = 70
        review.pop("review_protocol")
        self.assertFalse(self.check(review)["approved"])
        for missing in ("blocking_issues", "quality_notes", "image_observed"):
            with self.subTest(missing=missing):
                malformed = add_minor_note(valid_review(classified=True))
                malformed.pop(missing)
                self.assertFalse(self.check(malformed)["approved"])
        self.assertFalse(self.check({**valid_review(), "review_protocol": "other-policy"})["approved"])

    def test_every_freeform_issue_must_be_explicitly_classified(self):
        review = add_minor_note(valid_review(classified=True))
        review["issues"].append("설명되지 않은 다른 거절 사유")
        self.assertFalse(self.check(review)["approved"])
        for field, value in (("issues", None), ("blocking_issues", None), ("quality_notes", None),
                             ("issues", [None]), ("blocking_issues", [""]),
                             ("quality_notes", [{"code": [], "severity": "minor", "detail": "구도"}]),
                             ("quality_notes", [{"code": "composition", "severity": "major", "detail": "대상 안 보임"}])):
            with self.subTest(field=field, value=value):
                self.assertFalse(self.check({**valid_review(classified=True), field: value})["approved"])

    def test_score_floor_and_invalid_scores_cannot_be_overridden_by_minor_notes(self):
        base = add_minor_note(valid_review(classified=True))
        for score in (64.99, -1, 101, True, "90", None, float('nan'), float('inf'), 10**500):
            with self.subTest(score=str(score)[:20]):
                self.assertFalse(self.check({**base, "quality_score": score})["approved"])
        self.assertFalse(self.check({**valid_review(classified=True), "quality_score": 70})["approved"])

    def test_explicit_extra_hard_failures_are_not_overridden_by_composition_notes(self):
        for flag in ("people_at_distance", "usable_clarity", "cover_centered_text", "cover_translucent_black_panel"):
            with self.subTest(flag=flag):
                self.assertFalse(self.check({**add_minor_note(valid_review(classified=True)), flag: False})["approved"])

    def test_decision_preserves_raw_input_and_independent_diagnostic_copy(self):
        review = add_minor_note(valid_review(classified=True))
        original = copy.deepcopy(review)
        decision = self.check(review)
        self.assertEqual(review, original)
        decision["raw_review"]["quality_notes"][0]["detail"] = "별도 복사본 변경"
        decision["quality_notes"][0]["detail"] = "별도 요약 변경"
        self.assertEqual(review, original)

    def test_malformed_response_and_conflicting_text_context_are_rejected(self):
        for value in (None, [], True, "approved"):
            with self.subTest(value=value):
                self.assertFalse(self.check(value)["approved"])
        self.assertFalse(self.check(valid_review(), expected_headline="표지", expected_caption="설명")["approved"])

    def test_schema_and_prompt_explain_classification_without_fake_approval(self):
        schema = image_review_classification_schema()
        self.assertEqual(schema["review_protocol"], IMAGE_REVIEW_PROTOCOL)
        schema["quality_notes"].append("mutated")
        self.assertEqual(image_review_classification_schema()["quality_notes"], [])
        prompt = image_review_classification_prompt()
        self.assertIn("실제 첨부 파일을 직접 본 경우", prompt)
        self.assertIn("approved=false", prompt)
        self.assertIn("65~74점", prompt)
        self.assertIn("법적 사용권 보증은 아니다", prompt)


if __name__ == "__main__":
    unittest.main()
