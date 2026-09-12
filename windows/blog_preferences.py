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
    return {
        "prompts": presets, "selected_prompt_id": selected,
        "default_revision": str(value.get("default_revision", "")),
        "order": list(order), "step_count": count,
        "review_mode": str(value.get("review_mode", "단계별 교차 검수")),
        "models": {key: str(models.get(key, "")).strip() for key in PROVIDER_LABELS},
        "include_google": value.get("include_google", True) is True,
        "auto_start_on_launch": value.get("auto_start_on_launch", True) is True,
        "blocked_terms": normalize_blocked_terms(value.get("blocked_terms")),
        "publication_mode": {"발행": "자동 발행", "자동 발행": "자동 발행", "임시저장": "임시저장까지만",
                             "임시저장까지만": "임시저장까지만", "입력만": "편집기에 입력만",
                             "편집기에 입력만": "편집기에 입력만"}.get(value.get("publication_mode"), "자동 발행"),
    }


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
