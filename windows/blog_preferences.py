"""Persistent, validated CLI workflow choices; contains no credentials."""
from __future__ import annotations

import copy
import uuid

PROVIDER_LABELS = {
    "chatgpt": "ChatGPT CLI (Codex)",
    "claude": "Claude CLI",
    "antigravity": "Antigravity CLI",
}
DEFAULT_ORDER = ["chatgpt", "claude", "antigravity", "chatgpt"]


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
