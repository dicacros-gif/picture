"""Persistent, validated CLI workflow choices; contains no credentials."""
from __future__ import annotations

import copy
import re
import unicodedata
import uuid

PROVIDER_LABELS = {
    "chatgpt": "ChatGPT CLI (Codex)",
    "claude": "Claude CLI",
    "antigravity": "Antigravity CLI",
}
DEFAULT_ORDER = ["chatgpt", "claude", "antigravity", "chatgpt"]
STAGE_ROLES = ["작성", "교차 검수", "팩트·최신 정보 보강", "문체 다듬기"]
SPORTS_BLOCKED_TERMS = [
    "스포츠", "축구", "야구", "농구", "배구", "골프", "선수", "구단", "리그", "경기",
    "올림픽", "월드컵", "KBO", "프리미어리그", "감독 경질", "테니스", "배드민턴",
    "메달", "챔피언스", "플레이오프", "결승", "준결승", "프로야구", "MLB", "NBA", "FIFA",
]
DEATH_BLOCKED_TERMS = [
    "사망", "죽음", "별세", "부고", "숨져", "숨진", "자살", "극단적 선택", "참사",
    "사고사", "시신", "장례", "추모", "사망자", "유족", "운명하다", "타계",
]
DEFAULT_BLOCKED_TERMS = SPORTS_BLOCKED_TERMS + DEATH_BLOCKED_TERMS


def normalize_blocked_terms(value=None) -> list[str]:
    if value is None or not isinstance(value, (str, list, tuple)):
        value = DEFAULT_BLOCKED_TERMS
    if isinstance(value, str):
        value = re.split(r"[,;\n\r]+", value)
    terms, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            continue
        item = unicodedata.normalize("NFKC", item).strip()
        key = re.sub(r"\s+", "", item).casefold()
        if key and key not in seen:
            terms.append(item)
            seen.add(key)
    return terms


def blocked_term_hits(value, blocked_terms=None) -> list[str]:
    """Case/spacing-insensitive substring blocking, including every related result."""
    def strings(item):
        if isinstance(item, str):
            return [item]
        if isinstance(item, dict):
            return [text for part in item.values() for text in strings(part)]
        if isinstance(item, (list, tuple)):
            return [text for part in item for text in strings(part)]
        return []
    texts = [re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold() for text in strings(value)]
    return [term for term in normalize_blocked_terms(blocked_terms)
            if any(re.sub(r"\s+", "", term).casefold() in text for text in texts)]


def normalize_preferences(value: dict | None, default_prompt: str, legacy_prompt: str = "") -> dict:
    value = value if isinstance(value, dict) else {}
    presets, ids, names = [], set(), set()
    for preset in value.get("prompts", []):
        if not isinstance(preset, dict):
            continue
        identifier = str(preset.get("id", "")).strip()
        name = str(preset.get("name", "")).strip()
        text = str(preset.get("text", "")).strip()
        if identifier and name and text and identifier not in ids and name not in names:
            presets.append({"id": identifier, "name": name, "text": text})
            ids.add(identifier)
            names.add(name)
    if not presets:
        presets = [{"id": "default", "name": "기본 글쓰기", "text": default_prompt}]
        if legacy_prompt and legacy_prompt.strip() != default_prompt.strip():
            presets.append({"id": "legacy", "name": "이전 글쓰기 프롬프트", "text": legacy_prompt})
    order = value.get("order", DEFAULT_ORDER)
    object_stages = order if isinstance(order, list) and all(isinstance(p, dict) for p in order) else None
    if object_stages is not None:
        order = [p.get("provider") for p in object_stages]
    if not isinstance(order, list) or len(order) != 4 or any(p not in PROVIDER_LABELS for p in order):
        order = DEFAULT_ORDER.copy()
    try:
        count = max(1, min(4, int(value.get("step_count", 4))))
    except (TypeError, ValueError):
        count = 4
    selected = value.get("selected_prompt_id")
    selected = selected if any(p["id"] == selected for p in presets) else presets[0]["id"]
    models = value.get("models", {})
    if not isinstance(models, dict):
        models = {}
    stages = value.get("stages", object_stages or [])
    stages = stages if isinstance(stages, list) else []
    normalized_stages = []
    for index, provider in enumerate(order):
        stage = stages[index] if index < len(stages) and isinstance(stages[index], dict) else {}
        role = stage.get("role", "팩트·최신 정보 보강" if provider == "antigravity" else STAGE_ROLES[index])
        normalized_stages.append({"provider": provider, "role": role if role in STAGE_ROLES else STAGE_ROLES[index],
                                  "model": str(stage.get("model", "")).strip()})
    def threshold(name, default):
        try:
            return max(.1, min(1.0, float(value.get(name, default))))
        except (TypeError, ValueError):
            return default
    try:
        image_retry_limit = int(value.get("image_retry_limit", 2))
        if isinstance(value.get("image_retry_limit"), bool) or str(value.get("image_retry_limit", 2)).strip() != str(image_retry_limit):
            image_retry_limit = 2
        if image_retry_limit not in range(1, 4):
            image_retry_limit = 2
    except (TypeError, ValueError, OverflowError):
        image_retry_limit = 2
    try:
        google_reference_count = int(value.get("google_reference_count", 4))
        if isinstance(value.get("google_reference_count"), bool) or not 1 <= google_reference_count <= 10:
            google_reference_count = 4
    except (TypeError, ValueError, OverflowError):
        google_reference_count = 4
    return {
        "prompts": presets, "selected_prompt_id": selected,
        "default_revision": str(value.get("default_revision", "")),
        "order": list(order), "step_count": count,
        "stages": normalized_stages,
        "review_mode": str(value.get("review_mode", "단계별 교차 검수")),
        "models": {key: str(models.get(key, "")).strip() for key in PROVIDER_LABELS},
        "include_google": value.get("include_google", True) is True,
        "image_retry_limit": image_retry_limit,
        "google_reference_count": google_reference_count,
        "editorial_mode": value.get("editorial_mode") if value.get("editorial_mode") in ("natural", "strict") else "natural",
        "auto_start_on_launch": value.get("auto_start_on_launch", True) is True,
        "blocked_terms": normalize_blocked_terms(value.get("blocked_terms")),
        "duplicate_keyword_threshold": threshold("duplicate_keyword_threshold", .4),
        "duplicate_title_threshold": threshold("duplicate_title_threshold", .5),
        "publication_mode": {"발행": "자동 발행", "자동 발행": "자동 발행", "임시저장": "임시저장까지만",
                             "임시저장까지만": "임시저장까지만", "입력만": "편집기에 입력만",
                             "편집기에 입력만": "편집기에 입력만"}.get(value.get("publication_mode"), "자동 발행"),
    }


def automation_config_snapshot(settings: dict | None, fallback: dict | None = None) -> dict:
    """Resolve the next cycle from saved plain data, without reading Tk variables."""
    result = copy.deepcopy(fallback) if isinstance(fallback, dict) else {}
    saved = copy.deepcopy(settings) if isinstance(settings, dict) else {}
    preferences = saved.get("cli_workflow")
    if isinstance(preferences, dict):
        pref = normalize_preferences(preferences, str(result.get("base_prompt", "")),
                                     str(saved.get("blog_prompt", "")))
        prompt = next(p for p in pref["prompts"] if p["id"] == pref["selected_prompt_id"])
        result.update(
            steps=pref["order"][:pref["step_count"]],
            stage_configs=pref["stages"][:pref["step_count"]],
            models=pref["models"], base_prompt=prompt["text"], prompt_id=prompt["id"],
            review_mode=pref["review_mode"], include_google=pref["include_google"],
            image_retry_limit=pref["image_retry_limit"],
            google_reference_count=pref["google_reference_count"],
            editorial_mode=pref["editorial_mode"],
            publish=pref["publication_mode"] == "자동 발행",
            save_draft=pref["publication_mode"] == "임시저장까지만",
            completion_label=pref["publication_mode"], blocked_terms=pref["blocked_terms"],
            duplicate_keyword_threshold=pref["duplicate_keyword_threshold"],
            duplicate_title_threshold=pref["duplicate_title_threshold"],
        )
        result.setdefault("quality_checks", True)
    if "blog_id" in saved:
        result["blog_id"] = str(saved["blog_id"]).strip()
    try:
        hours = int(saved.get("auto_interval_hours", result.get("interval_hours", 1)))
        if hours not in range(1, 7):
            hours = 1
    except (TypeError, ValueError, OverflowError):
        hours = 1
    result.update(interval_hours=hours, interval_seconds=hours * 3600)
    result.setdefault("image_retry_limit", 2)
    result.setdefault("google_reference_count", 4)
    result.setdefault("editorial_mode", "natural")
    return result


def store_prompt(preferences: dict, identifier: str, name: str, text: str, *, create=False) -> dict:
    name, text = name.strip(), text.strip()
    if not name or not text:
        raise ValueError("프롬프트 이름과 내용을 입력하세요.")
    result = copy.deepcopy(preferences)
    if any(p["name"] == name and (create or p["id"] != identifier) for p in result["prompts"]):
        raise ValueError("같은 이름의 프롬프트가 있습니다. 다른 이름을 입력하세요.")
    if create:
        identifier = uuid.uuid4().hex
        result["prompts"].append({"id": identifier, "name": name, "text": text})
    else:
        target = next((p for p in result["prompts"] if p["id"] == identifier), None)
        if target is None:
            raise ValueError("저장할 프롬프트를 선택하세요.")
        target.update(name=name, text=text)
    result["selected_prompt_id"] = identifier
    return result
