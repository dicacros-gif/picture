"""Persistent, validated CLI workflow choices; contains no credentials."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
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
DEFAULT_CANNED_PHRASES = [
    "앞에서 본 핵심은", "그래서", "앞 구역의 답은 명확했어요", "그 다음에",
    "이 흐름이 가능했던 배경은", "많은 분들이", "를 확인했다면 이제",
    "를 명확히 구분했다면", "여기서", "차근차근 짚어보면", "이제 살펴볼",
    "알아볼 필요가 있습니다", "짚어볼 필요가 있어요", "권해 드립니다",
]

_TITLE_POLICY_BLOCK_V2 = """[제목 통합 작성 규칙 · 2026-09-13 v2]
첫 제목은 첫줄 후킹과 마지막 SEO 제목의 핵심 정보를 실제로 한 제목에 합쳐 작성합니다. 두 제목 중 하나만 고르거나 짧은 검색어 꼬리만 붙이지 않습니다. 독자가 궁금해하는 점과 본문에서 답하는 서로 다른 정보 두 가지 이상을 담고 공백 포함 45~68자로 쓰되 70자를 넘지 않습니다. 입력된 실제 연관 검색어 중 중요한 2개를 자연스럽게 포함합니다.
같은 연도·주제어·설명은 한 번만 남깁니다. 겹치는 주변 표현은 실제 연관어 안의 자연스러운 유사 표현으로 연결하되 의미가 달라지는 억지 동의어와 본문에 없는 사실은 만들지 않습니다. 물음표는 유지하고 쉼표·콜론·세미콜론은 쓰지 않습니다. 마지막 SEO 제목은 같은 검색 의도를 다른 정리·판단 관점으로 표현하고 반드시 뜻과 의미로 끝냅니다."""

_OPENING_POLICY_BLOCK = """[도입 후킹 규칙 · 2026-09-13]
첫 구역은 검색이나 블로그를 찾아온 과정을 설명하지 않고, 끝까지 읽어야 놓치기 쉬운 차이와 판단 기준을 알 수 있는 이유를 구체적인 상황이나 의문문으로 먼저 보여줍니다.
'검색한 분들이', '제일 먼저 답하면', '검색창을 옮겨 다니다 보면', '블로그마다', '이 글에서는 알아보겠습니다' 같은 상투 문장은 출력하지 않습니다. 정답을 첫 문장에 모두 소진하지 않고 가까운 문장에서 이유와 답을 자연스럽게 풉니다."""

_CANNED_TRANSITION_POLICY_BLOCK = """[상투적 연결 표현 금지 · 2026-09-13]
'앞에서 본 핵심은', '앞 구역의 답은 명확했어요', '그 다음에', '이 흐름이 가능했던 배경은', '많은 분들이', '~를 확인했다면 이제', '~를 명확히 구분했다면', '차근차근 짚어보면', '이제 살펴볼', '알아볼 필요가 있습니다', '짚어볼 필요가 있어요', '권해 드립니다'가 들어간 문장은 공개 원고에서 삭제합니다. '그래서'와 '여기서'가 문장 첫머리에 나오면 그 앞부분만 제거합니다. 앞 구역을 기계적으로 요약하지 말고 현재 구역의 구체적인 사실·상황·이유로 바로 이어갑니다."""

_EDITORIAL_FLOW_POLICY_BLOCK = """[도입부·소제목·문장 리듬 규칙 · 2026-09-13]
첫 구역 첫 문장은 의외의 사실, 둘째 문장은 글을 끝까지 읽고 얻을 구체적인 결과 약속이며 합계 90자 이내입니다. 제목 연관어를 이 두 문장에 자연스럽게 포함하고 검색어만 따로 한 줄로 쓰지 않습니다. 도입부 후보 3개를 완성 본문에 근거해 검토하고 금지어·길이·근거 구역 검사를 통과한 후보만 사용합니다.
8개 소제목은 질문형·단정형·반전형·장면형을 각각 두 번씩, 같은 유형이 연속하지 않게 씁니다. 2~8구역은 앞 구역 요약 없이 내용으로 바로 시작합니다. 1~7구역 마지막에는 다음 소제목의 구체적인 궁금증을 남기고 8구역은 실행 가능한 정리로 닫습니다.
처음 나오는 어려운 용어는 생활 비유를 먼저 쓰고 정의합니다. 문장은 보통 45자 안팎, 최대 60자로 쓰며 일반 문장은 2~3문장씩 묶고 굵은 문장·형광 문장·구역 끝 궁금증만 따로 띄웁니다. 수치는 같은 문장이나 바로 다음 문장에 비교 대상을 밝힙니다."""

_IMAGE_REALISM_POLICY_BLOCK = """[실사 이미지 질감 규칙 · 2026-09-13 v2]
생성 이미지는 실제 카메라 사진처럼 눈에 보이는 중간 강도의 고운 35mm 필름 그레인, 자연광, 자연스러운 렌즈 보케와 아웃포커싱, 아주 약한 광학 왜곡·비네팅·색수차를 사용합니다. 디지털 노이즈나 과한 빈티지 손상은 피하고 피부·옷감·사물의 미세한 실제 질감을 유지합니다.
인물은 가상의 한국인 성인만 사용하며 몇 미터 떨어진 중거리·원경에 작게 배치합니다. 정면 응시·정면 포즈·셀피·얼굴 클로즈업은 금지하고 측면·비스듬한 각도·뒷모습이나 자연스러운 활동 장면으로 표현합니다. 첫 이미지는 사람 얼굴을 전혀 넣지 않습니다. AI 생성 이미지는 앱의 해상도·파일·중복·첫 썸네일 문구 검사만 하고 CLI 시각 검수는 생략하며, Google 캡처만 글자·로고·워터마크·본문 관련성을 CLI로 검수합니다."""

_IMAGE_TEXT_POLICY_BLOCK_V2 = """[이미지 문구 최신 규칙 · 2026-09-13 v2]
첫 생성 이미지는 얼굴 없는 1:1 실사 사진으로 만들고, 핵심 사물은 선명하게 알아볼 수 있게 두며 주변 배경에는 자연스러운 아웃포커싱과 렌즈 보케를 사용합니다. 본문이 답하는 궁금증을 자연스러운 한글 질문으로 압축해 공백 포함 최대 28자로 쓰고 반드시 ?로 끝냅니다. 띄어쓰기와 문법을 지키고 단어만 붙인 조어·긴 설명·해시태그는 쓰지 않습니다.
앱이 한글 질문을 가독성 높은 굵은 고딕체로 두세 줄 이내에 가운데 정렬합니다. 일반 문구는 흰색, 서로 다른 핵심 단어는 형광 녹색 #8CE88C~#95F095와 선명한 형광 빨간색으로 나누어 강조합니다. 글자 아래에는 짙은 그림자를 넣고, 글자 뒤에는 원사진이 보이는 반투명 검정 배경을 둡니다.
Google 이미지는 핵심 키워드를 영어로 검색해 이미지 탭의 앞쪽 후보부터 확인합니다. 원본에 글자·로고·워터마크가 없고 사용 조건을 확인한 캡처만 여백을 보수적으로 잘라 원래 비율로 사용합니다. 같은 굵은 고딕체·가운데 정렬·반투명 검정 배경·짙은 그림자와 흰색·형광 녹색·형광 빨간색 조합으로 짧은 한글 질문을 추가합니다."""


def _migrate_title_policy_prompt(text: str) -> str:
    """Upgrade only the app-inserted, explicitly marked title policy block."""
    if not isinstance(text, str) or "[제목 통합 작성 규칙 · 2026-09-13]" not in text:
        return text
    return re.sub(
        r"\[제목 통합 작성 규칙 · 2026-09-13\]\s*.*?(?=\n+\[이미지 문구 최신 규칙 · 2026-09-13\])",
        _TITLE_POLICY_BLOCK_V2 + "\n",
        text,
        count=1,
        flags=re.S,
    )


def _migrate_opening_policy_prompt(text: str) -> str:
    """Add the requested opening rule only to prompts carrying the app title marker."""
    if (not isinstance(text, str) or "[도입 후킹 규칙 · 2026-09-13]" in text
            or "[제목 통합 작성 규칙 · 2026-09-13 v2]" not in text):
        return text
    marker = "[이미지 문구 최신 규칙 · 2026-09-13]"
    if marker in text:
        return text.replace(marker, _OPENING_POLICY_BLOCK + "\n\n" + marker, 1)
    return text.rstrip() + "\n\n" + _OPENING_POLICY_BLOCK


def _migrate_image_text_policy_prompt(text: str) -> str:
    """Replace the obsolete app-owned no-red block without touching user prose."""
    if not isinstance(text, str) or "[이미지 문구 최신 규칙 · 2026-09-13 v2]" in text:
        return text
    old_marker = "[이미지 문구 최신 규칙 · 2026-09-13]"
    if old_marker in text:
        return re.sub(
            r"\[이미지 문구 최신 규칙 · 2026-09-13\]\s*.*?(?=\n+\[[^\]\n]+\]|\Z)",
            _IMAGE_TEXT_POLICY_BLOCK_V2 + "\n",
            text,
            count=1,
            flags=re.S,
        )
    if "[제목 통합 작성 규칙 · 2026-09-13 v2]" in text:
        return text.rstrip() + "\n\n" + _IMAGE_TEXT_POLICY_BLOCK_V2
    return text


def _append_marked_policy(text: str, marker: str, block: str) -> str:
    """Append a new app policy once without changing unrelated user presets."""
    if not isinstance(text, str) or marker in text or "[제목 통합 작성 규칙 · 2026-09-13 v2]" not in text:
        return text
    return text.rstrip() + "\n\n" + block


def atomic_json_write(path: str | Path, value) -> None:
    """Commit a complete JSON file; concurrent writers never share a temp name."""
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.05 * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _settings_backup_path(path: Path) -> Path:
    return path.with_name(path.stem + ".last-good" + path.suffix)


def _read_settings_file(path: Path) -> dict:
    def invalid_constant(value):
        raise ValueError(f"잘못된 숫자 값: {value}")
    value = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=invalid_constant)
    if (not isinstance(value, dict)
            or ("cli_workflow" in value and not isinstance(value["cli_workflow"], dict))
            or ("blog_id" in value and not isinstance(value["blog_id"], str))):
        raise ValueError("설정 파일 형식이 올바르지 않습니다.")
    value.pop("_settings_recovery", None)
    return value


def _preserve_damaged_settings(path: Path) -> Path:
    destination = path.with_name(f"{path.stem}.corrupt-{uuid.uuid4().hex[:12]}{path.suffix}")
    shutil.copy2(path, destination)
    return destination


def save_settings_json(path: str | Path, settings: dict) -> None:
    """Keep a recoverable settings copy without discarding an unreadable original."""
    path = Path(path)
    value = copy.deepcopy(settings)
    if (not isinstance(value, dict)
            or ("cli_workflow" in value and not isinstance(value["cli_workflow"], dict))
            or ("blog_id" in value and not isinstance(value["blog_id"], str))):
        raise ValueError("설정 파일 형식이 올바르지 않습니다.")
    value.pop("_settings_recovery", None)
    # Validate serializability before creating either settings file.
    json.dumps(value, ensure_ascii=False, allow_nan=False)
    if path.exists():
        try:
            _read_settings_file(path)
        except (ValueError, UnicodeError):
            _preserve_damaged_settings(path)
    atomic_json_write(_settings_backup_path(path), value)
    atomic_json_write(path, value)


def load_settings_json(path: str | Path, default=None) -> dict:
    path = Path(path)
    backup = _settings_backup_path(path)
    if not path.exists() and not backup.exists():
        return copy.deepcopy(default) if isinstance(default, dict) else {}
    damage, preservation_error = None, ""
    try:
        loaded = _read_settings_file(path)
    except (OSError, ValueError, UnicodeError):
        if path.exists():
            try:
                damage = _preserve_damaged_settings(path)
            except OSError as exc:
                preservation_error = str(exc)
        try:
            loaded = _read_settings_file(backup)
        except (OSError, ValueError, UnicodeError):
            loaded = copy.deepcopy(default) if isinstance(default, dict) else {}
            preferences = loaded.get("cli_workflow")
            preferences = copy.deepcopy(preferences) if isinstance(preferences, dict) else {}
            preferences.update(auto_start_on_launch=False, publication_mode="편집기에 입력만")
            loaded.update(blog_id="", cli_workflow=preferences)
            status = "safe_defaults"
            message = "설정 파일과 정상 백업을 읽지 못했습니다. 계정과 자동 실행을 비워 두었습니다. 설정을 확인하고 저장하세요."
        else:
            status = "restored"
            message = "설정 파일 손상을 확인하여 최근 정상 백업의 설정과 프롬프트를 복원했습니다."
            if not preservation_error:
                try:
                    atomic_json_write(path, loaded)
                except OSError as exc:
                    message += f" 복원 파일 저장 확인이 필요합니다: {exc}"
        if preservation_error:
            loaded["blog_id"] = ""
            preferences = loaded.setdefault("cli_workflow", {})
            preferences["auto_start_on_launch"] = False
            preferences["publication_mode"] = "편집기에 입력만"
            message += f" 손상 원본 보존이 완료되지 않아 자동 실행을 중단했습니다: {preservation_error}"
        loaded["_settings_recovery"] = {"status": status, "message": message,
                                         "damaged_path": str(damage or "")}
        return loaded
    else:
        try:
            atomic_json_write(backup, loaded)
        except OSError as exc:
            loaded["_settings_recovery"] = {"status": "backup_unavailable",
                "message": f"현재 설정을 읽었지만 정상 백업 저장을 완료하지 못했습니다: {exc}"}
        return loaded


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


def normalize_canned_phrases(value=None) -> list[str]:
    if value is None or not isinstance(value, (str, list, tuple)):
        value = DEFAULT_CANNED_PHRASES
    if isinstance(value, str):
        value = re.split(r"[,;\n\r]+", value)
    phrases = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item)).strip()
        if 1 <= len(text) <= 80 and text not in phrases:
            phrases.append(text)
    return phrases[:80]


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
    supplied = value.get("prompts", [])
    for preset in supplied if isinstance(supplied, list) else []:
        if not isinstance(preset, dict):
            continue
        identifier = str(preset.get("id", "")).strip()
        name = str(preset.get("name", "")).strip()
        text = _migrate_image_text_policy_prompt(_migrate_opening_policy_prompt(
            _migrate_title_policy_prompt(str(preset.get("text", "")).strip())))
        text = _append_marked_policy(text, "[상투적 연결 표현 금지 · 2026-09-13]",
                                     _CANNED_TRANSITION_POLICY_BLOCK)
        text = _append_marked_policy(text, "[실사 이미지 질감 규칙 · 2026-09-13 v2]",
                                     _IMAGE_REALISM_POLICY_BLOCK)
        text = _append_marked_policy(text, "[도입부·소제목·문장 리듬 규칙 · 2026-09-13]",
                                     _EDITORIAL_FLOW_POLICY_BLOCK)
        typography_marker = "[썸네일 타이포그래피 · 2026-09-13]"
        if typography_marker in default_prompt:
            text = _append_marked_policy(text, typography_marker,
                typography_marker + default_prompt.split(typography_marker, 1)[1].rstrip())
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
    if not isinstance(order, list) or len(order) != 4 or any(not isinstance(p, str) or p not in PROVIDER_LABELS for p in order):
        order = DEFAULT_ORDER.copy()
    try:
        count = max(1, min(4, int(value.get("step_count", 4))))
    except (TypeError, ValueError, OverflowError):
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
            number = float(value.get(name, default))
            return max(.1, min(1.0, number)) if math.isfinite(number) else default
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
        "review_mode": value.get("review_mode") if value.get("review_mode") in ("단계별 교차 검수", "마지막 CLI 집중 검수", "선택 CLI 모두 검수") else "단계별 교차 검수",
        "models": {key: str(models.get(key, "")).strip() for key in PROVIDER_LABELS},
        "include_google": value.get("include_google", True) is True,
        "image_retry_limit": image_retry_limit,
        "google_reference_count": google_reference_count,
        "editorial_mode": value.get("editorial_mode") if value.get("editorial_mode") in ("natural", "strict") else "natural",
        "auto_start_on_launch": value.get("auto_start_on_launch", True) is True,
        "blocked_terms": normalize_blocked_terms(value.get("blocked_terms")),
        "canned_phrases": normalize_canned_phrases(value.get("canned_phrases")),
        "duplicate_keyword_threshold": threshold("duplicate_keyword_threshold", .4),
        "duplicate_title_threshold": threshold("duplicate_title_threshold", .5),
        "publication_mode": {"발행": "자동 발행", "자동 발행": "자동 발행", "임시저장": "임시저장까지만",
                             "임시저장까지만": "임시저장까지만", "입력만": "편집기에 입력만",
                             "편집기에 입력만": "편집기에 입력만"}.get(str(value.get("publication_mode", "")), "자동 발행"),
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
            canned_phrases=pref["canned_phrases"],
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
