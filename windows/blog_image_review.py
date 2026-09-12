"""Decide visual-review eligibility without rewriting the CLI's verdict.

This is not a file, copyright-license, or publication validator. Callers must
attach the actual file to the reviewing CLI and retain their existing file
hash, license, image-count, and final paragraph-context checks.
"""
from __future__ import annotations

import copy
import re
from typing import Any


IMAGE_REVIEW_PROTOCOL = "image-quality-v1"
LEGACY_QUALITY_MINIMUM = 75
CLASSIFIED_QUALITY_MINIMUM = 65
QUALITY_NOTE_CODES = frozenset({"composition", "fine_grain", "minor_quality"})
BASE_HARD_FLAGS = ("watermark_free", "logo_free", "anatomy_ok", "relevant",
                   "original_subject", "photorealistic")
COVER_HARD_FLAGS = ("cover_text_exact", "cover_text_legible", "no_other_text", "square_1_to_1",
                    "no_human_face", "bold_gothic", "text_shadow_visible", "approved_text_color")
CAPTION_HARD_FLAGS = ("caption_exact", "caption_legible", "no_other_text")


def image_review_classification_schema() -> dict:
    """Extra fields for a new actual-file review; never add them to old replies."""
    return {"review_protocol": IMAGE_REVIEW_PROTOCOL, "image_observed": True,
            "blocking_issues": [], "quality_notes": []}


def image_review_classification_prompt() -> str:
    """Explain the explicit classification without changing the hard constraints."""
    return (
        "review_protocol은 image-quality-v1이다. 실제 첨부 파일을 직접 본 경우에만 image_observed=true로 기록한다. "
        "파일을 볼 수 없거나 저작권 우려·워터마크·로고·왜곡·본문 무관·비실사·글자 오류나 가독성 실패·"
        "표지의 사람 얼굴 등 필수 조건이 실패하면 approved=false와 blocking_issues에 구체적인 사유를 적는다. "
        "blocking_issues는 문자열 배열이다. 경미한 구도 취향, 자연스러운 고운 필름 그레인, 약간의 품질 차이는 "
        "quality_notes에 {code: composition 또는 fine_grain 또는 minor_quality, severity: minor, detail: 실제 관찰 설명} "
        "객체로 분리한다. 심한 흐림·본문 의미를 알아볼 수 없는 핵심 대상 가림·글자 판독 실패를 경미한 품질로 분류하지 않는다. "
        "issues에 남기는 사유는 blocking_issues 또는 quality_notes.detail에도 빠짐없이 같은 문자열로 분류한다. "
        "모든 필수 조건을 만족하고 blocking_issues가 없으며 65점 이상이면 경미한 품질 의견만으로 거절하지 않는다. "
        "65~74점은 그 이유를 quality_notes에 반드시 설명한다. 승인할 수 없는 상태의 approved=false를 숨기지 않는다. "
        "original_subject는 시각적 기존 캐릭터·브랜드 재현 여부이고 법적 사용권 보증은 아니다. "
    )


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def evaluate_image_review(raw_review: Any, *, expected_headline: str = "", expected_caption: str = "",
                          actual_image_attached: bool = False, actual_image_size=None) -> dict:
    """Evaluate a raw CLI result, preserving it verbatim in ``raw_review``.

    ``actual_image_attached=True`` means this invocation attached the actual
    image bytes, not merely a prompt or filename. ``actual_image_size``, when
    supplied, must come from decoding those same bytes; it is authoritative
    only for square geometry. No model rejection is converted to approval.
    Legacy replies retain the 75-point and empty-issues requirements. A new
    protocol with complete, explicitly minor classifications may use 65 points.
    """
    decision = {"approved": False, "policy": "legacy-strict", "reasons": [],
                "quality_notes": [], "relaxed_quality": False, "diagnostics": [],
                "raw_approved": raw_review.get("approved") if isinstance(raw_review, dict) else None,
                "raw_review": copy.deepcopy(raw_review)}
    reasons = decision["reasons"]
    if actual_image_attached is not True:
        reasons.append("검수 요청에 실제 이미지 파일이 첨부되었다는 확인이 없습니다.")
    if not isinstance(raw_review, dict):
        reasons.append("CLI 이미지 검수 응답은 JSON 객체여야 합니다.")
        return decision
    if (not isinstance(expected_headline, str) or not isinstance(expected_caption, str)
            or (expected_headline and expected_caption)):
        reasons.append("표지 문구와 참고 이미지 설명의 검수 문맥이 올바르지 않습니다.")
        return decision
    size = None
    if actual_image_size is not None:
        if (not isinstance(actual_image_size, (tuple, list)) or len(actual_image_size) != 2
                or any(type(value) is not int or value <= 0 for value in actual_image_size)):
            reasons.append("첨부 이미지에서 직접 읽은 가로·세로 치수가 올바르지 않습니다.")
        else:
            size = tuple(actual_image_size)
    if raw_review.get("approved") is not True:
        reasons.append("CLI가 이 이미지의 사용을 승인하지 않았습니다.")
    flags = BASE_HARD_FLAGS + (COVER_HARD_FLAGS if expected_headline else
                               CAPTION_HARD_FLAGS if expected_caption else ("text_free",))
    for flag in flags:
        effective = raw_review.get(flag) is True
        if flag == "square_1_to_1" and size is not None:
            effective = size[0] == size[1]
            decision["diagnostics"].append({"code": "decoded_geometry", "width": size[0], "height": size[1],
                "raw_square_1_to_1": raw_review.get(flag), "square_1_to_1": effective})
        if not effective:
            reasons.append("필수 이미지 조건 미충족: " + flag)
    # If a newer caller asks for further hard checks, an explicit failure is
    # never reclassified as a harmless composition note.
    for flag in ("people_at_distance", "usable_clarity", "cover_centered_text", "cover_translucent_black_panel"):
        if flag in raw_review and raw_review[flag] is not True:
            reasons.append("필수 이미지 조건 미충족: " + flag)
    expected = expected_headline or expected_caption
    if expected and (raw_review.get("text_free") is not False
            or not isinstance(raw_review.get("detected_text"), str)
            or re.sub(r"\s+", "", raw_review["detected_text"]) != re.sub(r"\s+", "", expected)):
        reasons.append("실제로 읽은 이미지 문구가 허용한 한글 문구와 일치하지 않습니다.")
    issues = raw_review.get("issues")
    if not _string_list(issues):
        reasons.append("이미지 검수 issues는 비어 있거나 유효한 사유가 담긴 문자열 배열이어야 합니다.")
    score = raw_review.get("quality_score")
    score_valid = type(score) in (int, float) and 0 <= score <= 100
    if not score_valid:
        reasons.append("이미지 품질 점수는 0~100 사이의 유한한 숫자여야 합니다.")
    protocol = raw_review.get("review_protocol")
    if protocol is None:
        if score_valid and score < LEGACY_QUALITY_MINIMUM:
            reasons.append("기존 검수 형식은 품질 점수 75 이상이어야 합니다.")
        if isinstance(issues, list) and issues:
            reasons.append("기존 검수 형식에 해결되지 않은 사유가 있습니다.")
    elif protocol != IMAGE_REVIEW_PROTOCOL:
        reasons.append("지원하지 않는 이미지 검수 분류 프로토콜입니다.")
    else:
        decision["policy"] = IMAGE_REVIEW_PROTOCOL
        if raw_review.get("image_observed") is not True:
            reasons.append("CLI가 실제 이미지 파일을 직접 보았다고 확인하지 않았습니다.")
        blocking = raw_review.get("blocking_issues")
        notes = raw_review.get("quality_notes")
        if not _string_list(blocking):
            reasons.append("blocking_issues는 비어 있거나 차단 사유가 담긴 문자열 배열이어야 합니다.")
        elif blocking:
            reasons.extend("차단 사유: " + reason for reason in blocking)
        valid_notes = isinstance(notes, list) and all(isinstance(note, dict)
            and isinstance(note.get("code"), str) and note["code"] in QUALITY_NOTE_CODES
            and note.get("severity") == "minor" and isinstance(note.get("detail"), str)
            and bool(note["detail"].strip()) for note in notes)
        if not valid_notes:
            reasons.append("quality_notes에는 허용된 종류의 경미한 품질 의견만 명시할 수 있습니다.")
        else:
            decision["quality_notes"] = copy.deepcopy(notes)
            classified = {note["detail"] for note in notes}
            if _string_list(blocking):
                classified.update(blocking)
            if _string_list(issues) and any(issue not in classified for issue in issues):
                reasons.append("경미한 품질 또는 차단 사유로 분류되지 않은 검수 지적이 있습니다.")
            if score_valid and score < LEGACY_QUALITY_MINIMUM and not notes:
                reasons.append("75점 미만의 품질 점수에는 경미한 품질 사유가 명시되어야 합니다.")
        if score_valid and score < CLASSIFIED_QUALITY_MINIMUM:
            reasons.append("경미한 품질 완화 기준인 65점에 미달합니다.")
        decision["relaxed_quality"] = not reasons and (score < LEGACY_QUALITY_MINIMUM or bool(issues))
    decision["approved"] = not reasons
    return decision
