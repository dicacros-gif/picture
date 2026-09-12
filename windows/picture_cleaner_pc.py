from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, Canvas, StringVar, Tk, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageTk
from send2trash import send2trash
from chatgpt_classic_automation import ChatGPTClassicAutomation
from blog_controls import BlogWorkflowControls, next_cycle_tick
from blog_workflow import BlogWorkflow
from blog_preferences import (atomic_json_write, automation_config_snapshot, blocked_term_hits,
                              load_settings_json, save_settings_json)
from blog_runtime import ApplicationAlreadyRunning, application_instance_lock, access_error_from_exception, wait_for_restart_parent
from blog_diagnostics import BlogDiagnostics, capture_thread_exceptions, redact_diagnostic
from naver_automation import NaverAutomation


APP_NAME = "Blog"
APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "PictureCleanerPC"
CONFIG_FILE = APP_DIR / "settings.json"
DB_FILE = APP_DIR / "keywords.json"
AUTO_HISTORY_FILE = APP_DIR / "automation-history.json"
SUPPORTED = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
CLAUDE_CLI_MODELS = ("sonnet", "opus")
ANTIGRAVITY_CLI_MODELS = (
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.5-flash-high",
    "gemini-3.5-flash-medium",
    "gemini-3.5-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)

ANTIGRAVITY_MODEL_MIGRATION = {
    "Gemini 3.6 Flash": "gemini-3.6-flash-medium",
    "Gemini 3.5 Flash": "gemini-3.5-flash-medium",
    "Gemini 3.1 Pro": "gemini-3.1-pro-high",
    "Claude Sonnet 4.6": "claude-sonnet-4-6",
    "Claude Opus 4.6": "claude-opus-4-6-thinking",
    "GPT-OSS-120b": "gpt-oss-120b-medium",
}


def normalize_antigravity_model(value: str) -> str:
    model = (value or "").strip()
    model = ANTIGRAVITY_MODEL_MIGRATION.get(model, model)
    return model if model in ANTIGRAVITY_CLI_MODELS else "gemini-3.6-flash-medium"

DEFAULT_BLOG_PROMPT = (
    Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    / "assets" / "default_blog_prompt.txt"
).read_text(encoding="utf-8")


def load_json(path: Path, default):
    if Path(path).name.casefold() == "settings.json":
        return load_settings_json(path, default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, value) -> None:
    if Path(path).name.casefold() == "settings.json":
        save_settings_json(path, value)
    else:
        atomic_json_write(path, value)


def default_screenshot_folder() -> Path:
    candidates = [
        Path.home() / "Pictures" / "Screenshots",
        Path.home() / "OneDrive" / "Pictures" / "Screenshots",
        Path.home() / "Pictures",
    ]
    return next((p for p in candidates if p.exists()), candidates[-1])


def image_candidates(folder: Path, today_only: bool) -> list[Path]:
    if not folder.exists():
        return []
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    result = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        if today_only and path.stat().st_mtime < start:
            continue
        result.append(path)
    return sorted(result, key=lambda p: p.stat().st_mtime)


def detect_content_bounds(image: Image.Image) -> tuple[int, int, int, int]:
    rgb = image.convert("RGB")
    w, h = rgb.size
    scale = min(1.0, 900.0 / max(w, h))
    aw, ah = max(1, round(w * scale)), max(1, round(h * scale))
    small = rgb.resize((aw, ah), Image.Resampling.BILINEAR) if scale < 1 else rgb
    pixels = small.load()
    active = [[False] * aw for _ in range(ah)]

    def lum(px):
        return (px[0] * 299 + px[1] * 587 + px[2] * 114) // 1000

    for y in range(ah):
        for x in range(aw):
            r, g, b = pixels[x, y]
            brightness = lum((r, g, b))
            edge = 0
            if x + 1 < aw:
                edge += abs(brightness - lum(pixels[x + 1, y]))
            if y + 1 < ah:
                edge += abs(brightness - lum(pixels[x, y + 1]))
            saturation = max(r, g, b) - min(r, g, b)
            flat = (brightness <= 30 or brightness >= 248) and saturation < 14 and edge < 16
            active[y][x] = not flat and (saturation > 18 or edge > 22)

    def longest_run(flags: list[bool]) -> tuple[int, int]:
        best_start = best_len = 0
        start = -1
        for i in range(len(flags) + 1):
            on = i < len(flags) and flags[i]
            if on and start < 0:
                start = i
            elif not on and start >= 0:
                if i - start > best_len:
                    best_start, best_len = start, i - start
                start = -1
        return (best_start, best_start + best_len) if best_len else (0, len(flags))

    def smoothed(scores: list[int], base_threshold: int) -> list[bool]:
        threshold = max(base_threshold, max(scores, default=1) // 6)
        raw = [score >= threshold for score in scores]
        return [
            any(raw[j] for j in range(max(0, i - 1), min(len(raw), i + 2)))
            for i in range(len(raw))
        ]

    rows = [sum(row) for row in active]
    top, bottom = longest_run(smoothed(rows, max(1, aw // 12)))
    if bottom - top < ah // 8:
        return 0, 0, w, h
    columns = [sum(active[y][x] for y in range(top, bottom)) for x in range(aw)]
    left, right = longest_run(smoothed(columns, max(1, (bottom - top) // 12)))
    if right - left < aw // 8:
        left, right = 0, aw
    inv = 1.0 / scale
    pad = max(2, min(w, h) // 300)
    return (
        max(0, round(left * inv) - pad),
        max(0, round(top * inv) - pad),
        min(w, round(right * inv) + pad),
        min(h, round(bottom * inv) + pad),
    )


def conservative_content_bounds(
    image: Image.Image,
    *,
    max_side_trim: float = 0.10,
    min_retained_area: float = 0.82,
) -> tuple[int, int, int, int]:
    """Return a padded crop only when it cannot remove meaningful content."""
    w, h = image.size
    left, top, right, bottom = detect_content_bounds(image)
    pad_x = max(4, round(w * 0.045))
    pad_y = max(4, round(h * 0.045))
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(w, right + pad_x)
    bottom = min(h, bottom + pad_y)
    retained = ((right - left) * (bottom - top)) / max(1, w * h)
    trims = (
        left / max(1, w),
        top / max(1, h),
        (w - right) / max(1, w),
        (h - bottom) / max(1, h),
    )
    if retained < min_retained_area or any(value > max_side_trim for value in trims):
        return 0, 0, w, h
    return left, top, right, bottom


def enhance_image_resolution(image: Image.Image, target_long_side: int = 2048) -> Image.Image:
    """Upscale gently while preserving the source aspect ratio and natural detail."""
    image = image.convert("RGB")
    long_side = max(image.size)
    if long_side < target_long_side:
        final_ratio = min(3.0, target_long_side / max(1, long_side))
        final_size = (
            max(1, round(image.width * final_ratio)),
            max(1, round(image.height * final_ratio)),
        )
        # Two smaller Lanczos passes create fewer halos than one very large resize.
        if final_ratio > 1.65:
            middle_ratio = final_ratio**0.5
            image = image.resize(
                (
                    max(1, round(image.width * middle_ratio)),
                    max(1, round(image.height * middle_ratio)),
                ),
                Image.Resampling.LANCZOS,
            )
        image = image.resize(final_size, Image.Resampling.LANCZOS)
    image = ImageOps.autocontrast(image, cutoff=0.35, preserve_tone=True)
    image = ImageEnhance.Contrast(image).enhance(1.015)
    image = ImageEnhance.Color(image).enhance(1.01)
    return image.filter(ImageFilter.UnsharpMask(radius=0.9, percent=55, threshold=4))


def process_image(source: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        # Google capture files already contain only the selected preview image.
        # Cropping them again used to cut off faces, text and portrait edges.
        if not source.stem.lower().startswith("google_cc_"):
            image = image.crop(conservative_content_bounds(image))
        image = enhance_image_resolution(image)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        output = output_dir / f"cleaned_{stamp}_{source.stem[:30]}.jpg"
        image.save(output, "JPEG", quality=96, optimize=True, exif=b"")
    return output


def normalize_keyword(value: str) -> str:
    # 일부 실시간 검색어 API는 한글을 분해된 유니코드 자모(NFD)로 반환한다.
    # NFC로 합쳐 Windows/Tk 글꼴에서 정상적인 완성형 한글로 표시한다.
    value = unicodedata.normalize("NFC", str(value))
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f]+", " ", value)).strip()


def keyword_comparison_key(value: str) -> str:
    """띄어쓰기·기호 차이를 무시해 같은 검색어인지 비교하는 키를 만든다."""
    return re.sub(r"[\W_]+", "", normalize_keyword(value).casefold())


def fetch_autocomplete(seed: str) -> dict[str, list[str]]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    endpoints = {
        "네이버": (
            "https://ac.search.naver.com/nx/ac",
            {
                "q": seed,
                "con": 0,
                "frm": "nx",
                "ans": 2,
                "r_format": "json",
                "r_enc": "UTF-8",
                "r_unicode": 0,
                "t_koreng": 1,
                "run": 2,
                "rev": 4,
                "q_enc": "UTF-8",
                "st": 100,
            },
        ),
        "다음": (
            "https://suggest.search.daum.net/sushi/opensearch/pc",
            {"q": seed, "DA": "JU2"},
        ),
        "구글": (
            "https://suggestqueries.google.com/complete/search",
            {"client": "firefox", "hl": "ko", "q": seed},
        ),
    }
    output: dict[str, list[str]] = {}
    for name, (url, params) in endpoints.items():
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=12
            )
            response.raise_for_status()
            data = response.json()
            values: list[str] = []
            if name == "네이버":
                for group in data.get("items", []):
                    for item in group:
                        if isinstance(item, list) and item:
                            values.append(str(item[0]))
            elif name == "다음":
                values = (
                    list(data[1])
                    if isinstance(data, list)
                    and len(data) > 1
                    and isinstance(data[1], list)
                    else []
                )
            else:
                values = list(data[1]) if isinstance(data, list) and len(data) > 1 else []
            normalized = [normalize_keyword(value) for value in values]
            output[name] = list(
                dict.fromkeys(value for value in normalized if value)
            )[:15]
        except Exception:
            output[name] = []
    return output


def translate_korean_to_english(text: str) -> str:
    source = normalize_keyword(text)
    if not source:
        raise ValueError("번역할 검색어가 없습니다.")
    response = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={
            "client": "gtx",
            "sl": "ko",
            "tl": "en",
            "dt": "t",
            "q": source,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    translated = "".join(
        str(part[0])
        for part in (data[0] if data and isinstance(data[0], list) else [])
        if isinstance(part, list) and part and part[0]
    ).strip()
    if not translated:
        raise RuntimeError("Google 번역 결과가 비어 있습니다.")
    return normalize_keyword(translated)


def merge_related_keywords(
    seed: str, result: dict[str, list[str]]
) -> list[str]:
    seed_key = keyword_comparison_key(seed)
    merged = []
    seen = set()
    for source in ("네이버", "다음", "구글"):
        for word in result.get(source, []):
            normalized = normalize_keyword(word)
            key = keyword_comparison_key(normalized)
            if key and key != seed_key and key not in seen:
                seen.add(key)
                merged.append(normalized)
    return merged


def split_related_keywords(
    seed: str,
    full_result: dict[str, list[str]],
    prefix_result: dict[str, list[str]],
) -> tuple[str, list[str], list[str]]:
    """전체 문구 결과와 첫 단어 추가 결과를 중복 없이 분리한다."""
    normalized_seed = normalize_keyword(seed)
    parts = normalized_seed.split()
    prefix = parts[0] if len(parts) > 1 else ""
    full_keywords = merge_related_keywords(normalized_seed, full_result)
    if not prefix:
        return "", full_keywords, []

    seen = {
        keyword_comparison_key(normalized_seed),
        keyword_comparison_key(prefix),
    }
    seen.update(
        keyword_comparison_key(keyword)
        for keyword in full_keywords
        if keyword_comparison_key(keyword)
    )
    prefix_keywords = []
    for keyword in merge_related_keywords(prefix, prefix_result):
        normalized = normalize_keyword(keyword)
        key = keyword_comparison_key(normalized)
        if key and key not in seen:
            seen.add(key)
            prefix_keywords.append(normalized)
    return prefix, full_keywords, prefix_keywords


EPHEMERAL_PATTERNS = [
    r"\b\d+\s*[:대-]\s*\d+\b", r"\bvs\b", r"경기\s*(결과|중계|스코어)",
    r"(축구|야구|농구|배구|골프).*(결과|스코어|중계|라인업)",
    r"(결과|스코어|중계|라인업).*(축구|야구|농구|배구|골프)",
    r"선발\s*라인업", r"실시간\s*스코어", r"(로또|복권).*(당첨|번호|추첨)",
    r"(당첨|추첨).*(결과|번호)", r"로또\s*\d*회", r"오늘의\s*경기",
    r"몇\s*대\s*몇", r"득점\s*결과",
    r"(속보|긴급|현재|실시간).*(사고|상황|현황)",
    r"(경기|매치).*(오늘|내일|중계|시간)",
    r"^[0-9A-Za-z가-힣]+\s+대\s+[0-9A-Za-z가-힣]+$",
    r"\b(KIA|KT|롯데|키움|삼성|LG|한화|두산|SSG|NC|전북|서울|울산|포항|수원|KBO|K리그|프리미어리그|MLB|NBA)\b",
]

LONGTAIL_INTENT_WORDS = {
    "뜻",
    "의미",
    "이유",
    "원인",
    "방법",
    "사용법",
    "후기",
    "추천",
    "비교",
    "가격",
    "효과",
    "부작용",
    "정리",
    "정보",
    "인물관계도",
    "등장인물",
    "줄거리",
    "결말",
    "재방송",
    "시즌",
}


def is_ephemeral_keyword(keyword: str) -> bool:
    value = normalize_keyword(keyword).lower()
    return any(re.search(pattern, value, re.I) for pattern in EPHEMERAL_PATTERNS)


def longtail_candidate_score(
    keyword: str,
    related_keywords: list[str],
    source_count: int = 1,
) -> int:
    """연관어가 풍부하고 정보 탐색 의도가 오래가는 검색어를 우선한다."""
    normalized = normalize_keyword(keyword)
    if not normalized or is_ephemeral_keyword(normalized):
        return -10_000
    related = {
        keyword_comparison_key(value)
        for value in related_keywords
        if keyword_comparison_key(value)
    }
    intent_hits = sum(
        1
        for word in LONGTAIL_INTENT_WORDS
        if word.casefold() in normalized.casefold()
        or any(word.casefold() in value.casefold() for value in related_keywords)
    )
    word_bonus = min(4, len(normalized.split())) * 3
    return len(related) * 12 + min(4, source_count) * 5 + intent_hits * 7 + word_bonus


def fetch_realtime_groups() -> dict[str, list[str]]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    groups: dict[str, list[str]] = {
        "애드센스팜": [],
        "다음": [],
        "구글": [],
        "크리에이터 어드바이저": [],
        "네이버 시그널": [],
    }
    # 중계 사이트의 522(원본 서버 시간 초과)가 앱 전체를 멈추지 않도록
    # Google이 직접 제공하는 공식 Trending Searches RSS를 사용한다.
    try:
        response = requests.get(
            "https://trends.google.com/trending/rss?geo=KR",
            headers=headers,
            timeout=12,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        values = [
            normalize_keyword(item.text or "")
            for item in root.findall(".//item/title")
        ]
        groups["구글"] = list(dict.fromkeys(filter(None, values)))[:10]
    except Exception:
        groups["구글"] = []
    try:
        values = requests.get(
            "https://api.signal.bz/news/realtime", headers=headers, timeout=15
        ).json().get("top10", [])
        groups["네이버 시그널"] = [
            normalize_keyword(str(item.get("keyword", "")))
            for item in values
            if normalize_keyword(str(item.get("keyword", "")))
        ][:10]
    except Exception:
        groups["네이버 시그널"] = []
    try:
        groups["다음"] = fetch_daum_realtime_direct(10)
    except Exception:
        groups["다음"] = []
    return groups


def extract_daum_embedded_trends(
    payload: object,
    limit: int = 10,
) -> list[str]:
    """Extract Daum's ranked realtime keywords from its embedded page data."""
    requested = max(1, min(int(limit), 10))
    candidates: list[list[tuple[int, str]]] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            ui_type = str(value.get("uiType", "")).upper()
            contents = value.get("contents")
            if "REALTIME_TREND" in ui_type and isinstance(contents, dict):
                data = contents.get("data")
                keywords = data.get("keywords") if isinstance(data, dict) else None
                if isinstance(keywords, list):
                    ranked: list[tuple[int, str]] = []
                    for index, item in enumerate(keywords, 1):
                        if not isinstance(item, dict):
                            continue
                        keyword = normalize_keyword(str(item.get("keyword", "")))
                        try:
                            rank = int(item.get("displayRank", index))
                        except (TypeError, ValueError):
                            rank = index
                        if keyword:
                            ranked.append((rank, keyword))
                    if ranked:
                        candidates.append(ranked)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    if not candidates:
        return []
    best = max(candidates, key=len)
    output: list[str] = []
    for _, keyword in sorted(best, key=lambda item: item[0]):
        if keyword not in output:
            output.append(keyword)
        if len(output) >= requested:
            break
    return output


def fetch_daum_realtime_direct(limit: int = 10) -> list[str]:
    """Fetch Daum trends directly, without depending on AdsenseFarm or WebDriver."""
    response = requests.get(
        "https://www.daum.net/",
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9",
        },
        timeout=15,
    )
    response.raise_for_status()
    html = response.content.decode("utf-8", errors="replace")
    marker = re.search(r"window\.tillerInitData\s*=\s*", html)
    if not marker:
        raise RuntimeError("다음 실시간 트렌드 데이터가 페이지에 없습니다.")
    payload, _ = json.JSONDecoder().raw_decode(html[marker.end():])
    keywords = extract_daum_embedded_trends(payload, limit)
    if not keywords:
        raise RuntimeError("다음 실시간 트렌드 10개를 해석하지 못했습니다.")
    return keywords


def build_prompt(topic: str, keywords: list[str], base: str, image_slots: bool) -> str:
    instructions = (base or "").strip() or DEFAULT_BLOG_PROMPT
    keyword_block = "\n".join(
        f"- {keyword}" for keyword in keywords if (keyword or "").strip()
    )
    slot = (
        "\n사진을 넣기 좋은 문단 다음에는 [사진 삽입 위치]를 한 줄로 표시하세요."
        if image_slots
        else "\n사진 삽입 위치 문구는 출력하지 마세요."
    )
    return f"""{instructions}

──────────────
이번 글 작성 입력

주제
{topic}

연관 검색어 전체
{keyword_block or "- 없음"}
{slot}

위 입력의 검색 의도를 하나의 자연스러운 글로 통합하세요.
연관 검색어 목록 자체와 소스 및 출처는 결과에 출력하지 마세요.
완성된 블로그 글만 출력하세요."""


def compose_related_topic(seed: str, keywords: list[str]) -> str:
    """대표 검색어와 연관 검색어 전체를 중복 없이 하나의 주제로 만든다."""
    values: list[str] = []
    seen: set[str] = set()
    for value in [seed, *keywords]:
        keyword = normalize_keyword(value)
        comparison = keyword_comparison_key(keyword)
        if comparison and comparison not in seen:
            seen.add(comparison)
            values.append(keyword)
    return ", ".join(values)



class PictureCleanerApp(BlogWorkflowControls):
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_NAME)
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "blog.ico"
        if icon.is_file():
            self.root.iconbitmap(default=str(icon))
        self.root.geometry("1180x780")
        self.root.minsize(940, 650)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.diagnostics = BlogDiagnostics(APP_DIR / "logs" / "blog.log")
        self.root.bind("<Destroy>", lambda event: self._close_diagnostics() if event.widget is self.root else None, add="+")
        loaded_settings = load_json(CONFIG_FILE, {})
        self.settings = loaded_settings if isinstance(loaded_settings, dict) else {}
        self.dark_mode = BooleanVar(value=self.settings.get("dark_mode", False))
        from keyword_database import load_database, words
        self.keyword_db_records = load_database(DB_FILE)
        self.keyword_db = words(self.keyword_db_records)
        self.last_outputs: list[Path] = []
        self.preview_ref = None
        self.status = StringVar(value="준비됨")
        self.folder = StringVar(value=self.settings.get("folder", str(default_screenshot_folder())))
        self.today_only = BooleanVar(value=self.settings.get("today_only", True))
        self.recycle = BooleanVar(value=self.settings.get("recycle", False))
        self.seed = StringVar()
        self.selected_realtime = StringVar()
        self.realtime_by_source: dict[str, list[str]] = {}
        self.selected_keyword_label = StringVar(value="검색 키워드: -")
        self.prefix_keyword_label = StringVar(value="앞 단어 추가 검색: -")
        self.google_image_keyword = StringVar(value="선택한 검색어: -")
        self.google_image_translation = StringVar(value="영문 번역: -")
        self.last_google_image_url = ""
        self.last_google_capture_dir: Path | None = None
        self.related_request_id = 0
        self.keyword_checks = {}
        self.realtime_groups = {}
        self.naver_task_active = False
        self.realtime_task_active = False
        self.full_auto_active = False
        self.full_auto_stop = threading.Event()
        self.auto_use_claude = BooleanVar(
            value=self.settings.get("auto_use_claude", True)
        )
        self.auto_use_antigravity = BooleanVar(
            value=self.settings.get(
                "auto_use_antigravity",
                self.settings.get("auto_use_gemini", False),
            )
        )
        self.auto_interval_hours = StringVar(
            value=str(self.settings.get("auto_interval_hours", "1"))
        )
        self.auto_image_count = StringVar(
            value=str(self.settings.get("auto_image_count", "10"))
        )
        loaded_history = load_json(AUTO_HISTORY_FILE, [])
        self.auto_history = loaded_history if isinstance(loaded_history, list) else []
        self.topic = StringVar()
        self.image_slots = BooleanVar(value=True)
        self.phone_auto_images = BooleanVar(value=True)
        self.claude_cli_model = StringVar(
            value=self.settings.get("claude_cli_model", "sonnet")
        )
        self.antigravity_cli_model = StringVar(
            value=normalize_antigravity_model(
                self.settings.get(
                    "antigravity_cli_model",
                    "gemini-3.6-flash-medium",
                )
            )
        )
        self.blog_auto_images = BooleanVar(
            value=self.settings.get("blog_auto_images", True)
        )
        self.blog_id = StringVar(value=self.settings.get("blog_id", "macdcross"))
        self.comment_days = StringVar(value=self.settings.get("comment_days", "10"))
        self.comment_interval = StringVar(value=self.settings.get("comment_interval", "5"))
        self.neighbor_interval = StringVar(value=self.settings.get("neighbor_interval", "60"))
        self.neighbor_max = StringVar(value=self.settings.get("neighbor_max", "5"))
        self.naver_bot = NaverAutomation(APP_DIR, self._naver_log)
        self.chatgpt_classic = ChatGPTClassicAutomation(
            self._naver_log, self.naver_bot.stop_event
        )
        self._init_cli_controls(CONFIG_FILE, APP_DIR, DEFAULT_BLOG_PROMPT)
        self._style()
        self._layout()
        self._install_general_settings_autosave()
        recovery = self.settings.get("_settings_recovery", {})
        if isinstance(recovery, dict) and recovery.get("message"):
            self.status.set(recovery["message"])
            self._naver_log(recovery["message"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = lambda kind, value, trace: self._report_uncaught_exception(
            "화면 콜백 예외", kind, value, trace)
        self._naver_log("프로그램 시작 · Blog")
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.after_idle(self._maximize_window)
        # 일부 Windows 환경은 최초 매핑 직후 창 상태를 다시 복원하므로 한 번 더 적용한다.
        self.root.after(500, self._maximize_window)
        self.root.after(100, self._poll)
        self.root.after(300, lambda: self.run_realtime(startup=True))

    def _general_settings_snapshot(self):
        names = ("folder", "today_only", "recycle", "claude_cli_model", "antigravity_cli_model",
                 "blog_id", "comment_days", "comment_interval", "neighbor_interval", "neighbor_max",
                 "dark_mode", "blog_auto_images", "auto_use_claude", "auto_use_antigravity",
                 "auto_interval_hours", "auto_image_count")
        settings = {name: getattr(self, name).get() for name in names if hasattr(self, name)}
        limits = {"comment_days": (1, None, "10"), "comment_interval": (0, None, "5"),
                  "neighbor_interval": (10, None, "60"), "neighbor_max": (1, 200, "5"),
                  "auto_interval_hours": (1, 6, "1"), "auto_image_count": (1, None, "10")}
        for name, (minimum, maximum, default) in limits.items():
            if name not in settings:
                continue
            try:
                number = int(settings[name])
                if number < minimum or (maximum is not None and number > maximum):
                    raise ValueError
                settings[name] = str(number)
            except (TypeError, ValueError, OverflowError):
                settings[name] = self.settings.get(name, default)
        return settings

    def _install_general_settings_autosave(self):
        self._general_settings_job = None
        for name in self._general_settings_snapshot():
            getattr(self, name).trace_add("write", lambda *_: self._schedule_general_settings_save())

    def _schedule_general_settings_save(self):
        if getattr(self, "_closing", False):
            return
        pending = getattr(self, "_general_settings_job", None)
        if pending:
            self.root.after_cancel(pending)
        self._general_settings_job = self.root.after(700, self._autosave_general_settings)

    def _autosave_general_settings(self):
        self._general_settings_job = None
        if getattr(self, "_closing", False):
            return
        try:
            self.settings.update(self._general_settings_snapshot())
            self._persist_cli_preferences()
        except (OSError, ValueError) as exc:
            self.status.set(f"설정 저장 실패: {exc}")
            self._naver_log(f"설정 저장 실패: {exc}")

    def _update_keyword_queue(self, *, observed=(), consumed=()):
        from keyword_database import reconcile, update_database, words
        seed = reconcile(getattr(self, "keyword_db_records", {}), getattr(self, "keyword_db", []))
        self.keyword_db_records = update_database(DB_FILE, observed=observed, consumed=consumed,
            seed_records=seed, keyword_filter=self.topic_history.filter_keywords)
        self.keyword_db = words(self.keyword_db_records)

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        self.root.configure(background="#eef3f8")
        style.configure("TFrame", background="#eef3f8")
        style.configure("TLabel", background="#eef3f8", foreground="#1f2937")
        style.configure(
            "TNotebook", background="#eef3f8", borderwidth=0, tabmargins=(0, 0, 0, 0)
        )
        style.configure(
            "TNotebook.Tab",
            padding=(20, 11),
            font=("맑은 고딕", 10, "bold"),
            background="#dce6f1",
            foreground="#425466",
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#ffffff"), ("active", "#e8f1fb")],
            foreground=[("selected", "#0b5cab"), ("active", "#0b5cab")],
        )
        style.configure(
            "TButton", font=("맑은 고딕", 10), padding=(12, 8)
        )
        style.configure(
            "Accent.TButton",
            font=("맑은 고딕", 10, "bold"),
            padding=(16, 9),
            background="#1769aa",
            foreground="#ffffff",
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#0f568e"), ("pressed", "#0b4778")],
        )
        style.configure(
            "Copy.TButton",
            font=("맑은 고딕", 10, "bold"),
            padding=(16, 9),
            background="#16835d",
            foreground="#ffffff",
        )
        style.map(
            "Copy.TButton",
            background=[("active", "#106c4c"), ("pressed", "#0c573e")],
        )
        style.configure("Title.TLabel", font=("맑은 고딕", 19, "bold"), foreground="#15324b")
        style.configure("Sub.TLabel", font=("맑은 고딕", 10), foreground="#52606d")
        style.configure(
            "Panel.TLabelframe",
            background="#ffffff",
            bordercolor="#b8c8d8",
            relief="solid",
        )
        style.configure(
            "Panel.TLabelframe.Label",
            background="#eef3f8",
            foreground="#174b73",
            font=("맑은 고딕", 11, "bold"),
        )
        style.configure(
            "Source.TLabelframe",
            background="#f8fbfe",
            bordercolor="#c9d8e7",
        )
        style.configure(
            "Source.TLabelframe.Label",
            background="#f8fbfe",
            foreground="#0b5cab",
            font=("맑은 고딕", 10, "bold"),
        )
        style.configure(
            "Keyword.TCheckbutton",
            background="#f8fbfe",
            foreground="#172b3a",
            font=("맑은 고딕", 10),
            padding=(3, 4),
        )
        style.map(
            "Keyword.TCheckbutton",
            background=[("active", "#e8f3ff"), ("selected", "#dceeff")],
            foreground=[("selected", "#064f8c")],
        )
        style.configure(
            "Status.TLabel",
            background="#dceaf6",
            foreground="#174b73",
            font=("맑은 고딕", 10, "bold"),
            padding=(10, 7),
        )
        self.style = style
        self._apply_theme()

    def _apply_theme(self):
        dark = bool(self.dark_mode.get())
        colors = {
            "bg": "#111827" if dark else "#eef3f8",
            "panel": "#1f2937" if dark else "#ffffff",
            "panel_alt": "#253244" if dark else "#f8fbfe",
            "text": "#f3f4f6" if dark else "#1f2937",
            "muted": "#b7c2ce" if dark else "#52606d",
            "border": "#465568" if dark else "#b8c8d8",
            "tab": "#273548" if dark else "#dce6f1",
            "tab_selected": "#1f2937" if dark else "#ffffff",
            "entry": "#182231" if dark else "#ffffff",
            "primary": "#2388d1" if dark else "#1769aa",
            "green": "#20a875" if dark else "#16835d",
            "danger": "#d9534f" if dark else "#c43d3d",
            "status": "#203b55" if dark else "#dceaf6",
            "selection": "#315f86" if dark else "#b9dcf7",
        }
        self.palette = colors
        style = self.style
        self.root.configure(background=colors["bg"])
        style.configure("TFrame", background=colors["bg"])
        style.configure(
            "TLabel",
            background=colors["bg"],
            foreground=colors["text"],
            font=("맑은 고딕", 10, "bold"),
        )
        style.configure(
            "TCheckbutton",
            background=colors["bg"],
            foreground=colors["text"],
            font=("맑은 고딕", 10, "bold"),
        )
        style.map(
            "TCheckbutton",
            background=[("active", colors["panel_alt"])],
            foreground=[("active", colors["text"])],
        )
        style.configure(
            "TLabelframe",
            background=colors["bg"],
            bordercolor=colors["border"],
        )
        style.configure(
            "TLabelframe.Label",
            background=colors["bg"],
            foreground=colors["text"],
            font=("맑은 고딕", 11, "bold"),
        )
        style.configure("TNotebook", background=colors["bg"])
        style.configure(
            "TNotebook.Tab",
            background=colors["tab"],
            foreground=colors["muted"],
            font=("맑은 고딕", 10, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", colors["tab_selected"]),
                ("active", colors["panel_alt"]),
            ],
            foreground=[("selected", "#49a9ed" if dark else "#0b5cab")],
        )
        style.configure(
            "TButton",
            background=colors["tab"],
            foreground=colors["text"],
            font=("맑은 고딕", 10, "bold"),
        )
        style.map(
            "TButton",
            background=[("active", colors["panel_alt"]), ("pressed", colors["border"])],
        )
        style.configure(
            "Accent.TButton", background=colors["primary"], foreground="#ffffff"
        )
        style.configure(
            "Copy.TButton", background=colors["green"], foreground="#ffffff"
        )
        style.configure(
            "Danger.TButton", background=colors["danger"], foreground="#ffffff",
            font=("맑은 고딕", 10, "bold"), padding=(16, 9)
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#b83232"), ("pressed", "#922626")],
        )
        style.configure(
            "Secondary.TButton", background="#596b7e", foreground="#ffffff",
            font=("맑은 고딕", 10, "bold"), padding=(16, 9)
        )
        style.configure("Sub.TLabel", background=colors["bg"], foreground=colors["muted"])
        style.configure(
            "Panel.TLabelframe",
            background=colors["panel"],
            bordercolor=colors["border"],
        )
        style.configure(
            "Panel.TLabelframe.Label",
            background=colors["bg"],
            foreground="#58b6f5" if dark else "#174b73",
        )
        style.configure(
            "Source.TLabelframe",
            background=colors["panel_alt"],
            bordercolor=colors["border"],
        )
        style.configure(
            "Source.TLabelframe.Label",
            background=colors["panel_alt"],
            foreground="#58b6f5" if dark else "#0b5cab",
        )
        style.configure(
            "Keyword.TCheckbutton",
            background=colors["panel_alt"],
            foreground=colors["text"],
            font=("맑은 고딕", 10, "bold"),
        )
        style.map(
            "Keyword.TCheckbutton",
            background=[
                ("active", colors["selection"]),
                ("selected", colors["selection"]),
            ],
            foreground=[("selected", colors["text"])],
        )
        style.configure(
            "Status.TLabel",
            background=colors["status"],
            foreground="#d9efff" if dark else "#174b73",
        )
        style.configure(
            "TEntry",
            fieldbackground=colors["entry"],
            foreground=colors["text"],
            insertcolor=colors["text"],
            font=("맑은 고딕", 10, "bold"),
        )
        style.configure(
            "TCombobox",
            fieldbackground=colors["entry"],
            background=colors["entry"],
            foreground=colors["text"],
            arrowcolor=colors["text"],
            font=("맑은 고딕", 10, "bold"),
        )
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", colors["entry"]),
                ("focus", colors["entry"]),
            ],
            background=[("readonly", colors["entry"])],
            foreground=[
                ("readonly", colors["text"]),
                ("focus", colors["text"]),
            ],
            selectbackground=[("readonly", colors["selection"])],
            selectforeground=[("readonly", colors["text"])],
        )
        self.root.option_add("*TCombobox*Listbox.background", colors["entry"])
        self.root.option_add("*TCombobox*Listbox.foreground", colors["text"])
        self.root.option_add(
            "*TCombobox*Listbox.selectBackground", colors["selection"]
        )
        self.root.option_add(
            "*TCombobox*Listbox.selectForeground", colors["text"]
        )
        for name in ("keyword_canvas",):
            widget = getattr(self, name, None)
            if widget:
                widget.configure(background=colors["panel"])
        for name in (
            "keyword_text",
            "keyword_prefix_text",
            "base_text",
            "blog_result",
            "comment_log",
            "global_progress_log",
            "cli_log",
            "cli_blocked_terms",
        ):
            widget = getattr(self, name, None)
            if widget:
                widget.configure(
                    background=colors["entry"],
                    foreground=colors["text"],
                    insertbackground=colors["text"],
                    selectbackground=colors["selection"],
                    font=("맑은 고딕", 11, "bold"),
                )
        image_list = getattr(self, "image_list", None)
        if image_list:
            image_list.configure(
                background=colors["entry"],
                foreground=colors["text"],
                selectbackground=colors["selection"],
                selectforeground=colors["text"],
                font=("맑은 고딕", 10, "bold"),
            )

    def _toggle_dark_mode(self):
        self._apply_theme()
        self.status.set("다크 모드를 적용했습니다." if self.dark_mode.get() else "라이트 모드를 적용했습니다.")

    def _maximize_window(self):
        try:
            self.root.state("zoomed")
        except Exception:
            self.root.attributes("-zoomed", True)

    def _on_mousewheel(self, event):
        canvas = getattr(self, "keyword_canvas", None)
        if not canvas:
            return None
        target = self.root.winfo_containing(event.x_root, event.y_root)
        while target is not None:
            if target is canvas or target is getattr(self, "keyword_groups_frame", None):
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
            target = getattr(target, "master", None)
        return None

    def _layout(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        topbar = ttk.Frame(outer)
        topbar.pack(fill="x", pady=(0, 8))
        ttk.Button(
            topbar,
            text="반복 자동화 시작",
            style="Accent.TButton",
            command=self.start_full_automation,
        ).pack(side="left")
        ttk.Button(
            topbar,
            text="전체 자동화 중지",
            style="Danger.TButton",
            command=self.stop_full_automation,
        ).pack(side="left", padx=(7, 12))
        ttk.Checkbutton(topbar, text="시작 시 자동 실행", variable=self.auto_start_on_launch,
                        command=self._save_launch_choice).pack(side="left", padx=(0, 10))
        ttk.Label(topbar, text="반복 간격").pack(side="left")
        interval_box = ttk.Combobox(topbar, textvariable=self.auto_interval_hours,
            values=["1", "2", "3", "4", "5", "6"], width=4, state="readonly")
        interval_box.pack(side="left", padx=5)
        interval_box.bind("<<ComboboxSelected>>", self._save_cli_selection)
        ttk.Label(topbar, text="시간마다").pack(side="left")
        ttk.Label(topbar, text="완료 동작").pack(side="left", padx=(12, 4))
        completion_box = ttk.Combobox(topbar, textvariable=self.cli_publication,
            values=["자동 발행", "임시저장까지만", "편집기에 입력만"], state="readonly", width=17)
        completion_box.pack(side="left")
        self.cli_runtime_selectors.append(completion_box)
        completion_box.bind("<<ComboboxSelected>>", self._save_cli_selection)
        ttk.Label(topbar, text="8구역 · 생성 이미지 + Google 캡처", style="Sub.TLabel").pack(side="left", padx=12)
        ttk.Checkbutton(
            topbar,
            text="다크 모드",
            variable=self.dark_mode,
            command=self._toggle_dark_mode,
        ).pack(side="right")
        from progress_panel import ProgressPanel
        self.progress_panel = ProgressPanel(self, outer)
        self.tabs = self.progress_panel.notebook
        self.global_progress_log = self.progress_panel.text
        self.image_tab = ttk.Frame(self.tabs, padding=16)
        self.keyword_tab = ttk.Frame(self.tabs, padding=16)
        self.blog_tab = ttk.Frame(self.tabs, padding=16)
        self.comment_tab = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(self.keyword_tab, text="1  실시간 연관 검색어")
        self.tabs.add(self.image_tab, text="2  구글 이미지 검색")
        self.tabs.add(self.blog_tab, text="3  네이버 블로그 자동화")
        self.tabs.add(self.comment_tab, text="4  댓글·이웃 소통")
        self._keyword_ui()
        self._image_ui()
        self._blog_ui()
        self._comment_ui()
        # 프로그램 시작 화면은 실시간 연관 검색어로 고정한다.
        self.tabs.select(self.keyword_tab)
        ttk.Separator(outer).pack(fill="x", pady=(12, 7))
        ttk.Label(outer, textvariable=self.status, style="Status.TLabel").pack(
            fill="x"
        )
        self._apply_theme()

    def _image_ui(self):
        search_card = ttk.LabelFrame(
            self.image_tab,
            text="선택 검색어 영문 번역 · Google 이미지 검색",
            padding=12,
            style="Panel.TLabelframe",
        )
        search_card.pack(fill="x", pady=(0, 12))
        ttk.Label(
            search_card,
            textvariable=self.google_image_keyword,
            font=("맑은 고딕", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            search_card,
            textvariable=self.google_image_translation,
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(5, 9))
        google_actions = ttk.Frame(search_card)
        google_actions.pack(fill="x")
        ttk.Button(
            google_actions,
            text="1번에서 선택한 검색어로 Google 이미지 검색",
            style="Accent.TButton",
            command=self.start_google_image_search,
        ).pack(side="left")
        ttk.Button(
            google_actions,
            text="검색 결과 다시 열기",
            command=self.open_last_google_image_search,
        ).pack(side="left", padx=8)
        capture_actions = ttk.Frame(search_card)
        capture_actions.pack(fill="x", pady=(9, 0))
        ttk.Button(
            capture_actions,
            text="이미지 15장 크롭·화질 개선",
            style="Accent.TButton",
            command=self.capture_and_enhance_google_images_15,
        ).pack(side="left")
        ttk.Label(
            search_card,
            text=(
                "한글 검색어를 영어로 번역한 뒤 네이버 웨일에서 Google 이미지 결과를 엽니다. "
                "15장 저장과 화질 개선은 각각 또는 순서대로 실행할 수 있습니다."
            ),
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(9, 0))

        ttk.Label(
            self.image_tab,
            text="내 사진 정리 · 크롭 및 화질 개선",
            font=("맑은 고딕", 10, "bold"),
        ).pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(self.image_tab)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.folder).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="폴더 선택", command=self.choose_folder).pack(side="left", padx=(8, 0))
        options = ttk.Frame(self.image_tab)
        options.pack(fill="x", pady=10)
        ttk.Checkbutton(options, text="오늘 생성된 사진만", variable=self.today_only).pack(side="left")
        ttk.Checkbutton(
            options, text="완료 후 원본을 휴지통으로 이동", variable=self.recycle
        ).pack(side="left", padx=20)
        actions = ttk.Frame(self.image_tab)
        actions.pack(fill="x")
        ttk.Button(actions, text="사진 목록 새로고침", command=self.refresh_images).pack(side="left")
        ttk.Button(actions, text="크롭·화질 개선 시작", style="Accent.TButton", command=self.run_images).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="결과 폴더 열기", command=self.open_output).pack(side="left")
        split = ttk.Panedwindow(self.image_tab, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(12, 0))
        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(right, weight=2)
        self.image_list = __import__("tkinter").Listbox(left, exportselection=False, font=("맑은 고딕", 10))
        self.image_list.pack(fill="both", expand=True)
        self.image_list.bind("<<ListboxSelect>>", self.preview_image)
        self.preview = ttk.Label(right, anchor="center", text="사진을 선택하면 미리보기가 표시됩니다.")
        self.preview.pack(fill="both", expand=True)
        self.image_paths: list[Path] = []
        self.refresh_images()

    def _render_keyword_groups(self, groups):
        for child in self.keyword_groups_frame.winfo_children():
            child.destroy()
        self.keyword_checks = {}
        if not groups:
            ttk.Label(
                self.keyword_groups_frame,
                text="앱 시작과 동시에 5개 출처의 실시간 검색어를 수집합니다.",
                style="Sub.TLabel",
            ).pack(anchor="w", padx=6, pady=10)
            return
        for source in [
            "애드센스팜",
            "네이버 시그널",
            "다음",
            "구글",
            "크리에이터 어드바이저",
            "크리에이터 어드바이저 · 비즈니스·경제",
            "크리에이터 어드바이저 · IT·컴퓨터",
        ]:
            words = [
                word for word in groups.get(source, [])
                if not is_ephemeral_keyword(word)
            ]
            # 조회 실패 안내는 상태창에만 표시하고 빈 출처 카드는 만들지 않는다.
            if not words:
                continue
            card_title = (
                "구글 인기 검색어 · 최대 100위"
                if source == "구글"
                else f"{source} 실시간 검색어"
            )
            card = ttk.LabelFrame(
                self.keyword_groups_frame,
                text=card_title,
                padding=8,
                style="Source.TLabelframe",
            )
            card.pack(fill="x", padx=4, pady=5)
            for rank, keyword in enumerate(words, 1):
                variable = BooleanVar(value=False)
                check_key = f"{source}:{rank}:{keyword}"
                self.keyword_checks[check_key] = variable
                ttk.Checkbutton(
                    card,
                    text=f"{rank}. {keyword}",
                    variable=variable,
                    style="Keyword.TCheckbutton",
                    command=lambda key=check_key, word=keyword: (
                        self._select_realtime_keyword(key, word)
                    ),
                ).pack(anchor="w", pady=1)

    def _select_realtime_keyword(self, check_key, keyword):
        chosen = self.keyword_checks[check_key].get()
        for key, variable in self.keyword_checks.items():
            if key != check_key:
                variable.set(False)
        if not chosen:
            self.related_request_id += 1
            self.seed.set("")
            self.selected_keyword_label.set("검색 키워드: -")
            self.prefix_keyword_label.set("앞 단어 추가 검색: -")
            self.keyword_text.delete("1.0", "end")
            self.keyword_prefix_text.delete("1.0", "end")
            return
        self.seed.set(keyword)
        self.topic.set(keyword)
        self.selected_keyword_label.set(f"선택 검색어: {keyword}")
        self.run_related()

    def _keyword_ui(self):
        top = ttk.Frame(self.keyword_tab)
        top.pack(fill="x", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        ttk.Label(
            top,
            text="직접 키워드 검색",
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.keyword_search_entry = ttk.Entry(
            top,
            textvariable=self.seed,
            font=("맑은 고딕", 11, "bold"),
        )
        self.keyword_search_entry.grid(
            row=0, column=1, sticky="ew", padx=(0, 8), ipady=4
        )
        self.keyword_search_entry.bind(
            "<Return>", self.run_manual_related
        )
        ttk.Button(
            top,
            text="연관 검색어 조회",
            style="Accent.TButton",
            command=self.run_manual_related,
        ).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(
            top, text="실시간 검색어 새로고침", command=self.run_realtime
        ).grid(row=0, column=3)

        ttk.Label(
            self.keyword_tab,
            text=(
                "키워드를 입력하고 Enter를 누르거나, 왼쪽 실시간 검색어를 하나 선택하세요. "
                "여러 단어이면 전체 문구 결과는 위에, 첫 단어 추가 결과는 중복을 제거해 아래에 표시합니다."
            ),
            style="Sub.TLabel",
        ).pack(fill="x", pady=(0, 8))

        toolbar = ttk.Frame(self.keyword_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(
            toolbar, text="선택 검색어를 블로그 주제로", command=self.use_keyword
        ).pack(side="left")
        ttk.Button(
            toolbar,
            text="두 결과 모두 복사",
            style="Copy.TButton",
            command=self.copy_all_related,
        ).pack(side="right")

        split = ttk.Panedwindow(self.keyword_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.LabelFrame(
            split, text="실시간 검색어 · 하나만 선택", padding=10,
            style="Panel.TLabelframe"
        )
        right = ttk.LabelFrame(
            split,
            text="연관 검색어 · 네이버·다음·구글 통합",
            padding=10,
            style="Panel.TLabelframe",
        )
        split.add(left, weight=1)
        split.add(right, weight=1)

        self.keyword_canvas = Canvas(
            left, highlightthickness=0, background="#ffffff"
        )
        scroll = ttk.Scrollbar(
            left, orient="vertical", command=self.keyword_canvas.yview
        )
        self.keyword_groups_frame = ttk.Frame(self.keyword_canvas)
        self.keyword_groups_frame.bind(
            "<Configure>",
            lambda _event: self.keyword_canvas.configure(
                scrollregion=self.keyword_canvas.bbox("all")
            ),
        )
        groups_window = self.keyword_canvas.create_window(
            (0, 0), window=self.keyword_groups_frame, anchor="nw"
        )
        self.keyword_canvas.bind(
            "<Configure>",
            lambda event: self.keyword_canvas.itemconfigure(
                groups_window, width=event.width
            ),
        )
        self.keyword_canvas.configure(yscrollcommand=scroll.set)
        self.keyword_canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        result_split = ttk.Panedwindow(right, orient="vertical")
        result_split.pack(fill="both", expand=True)
        full_panel = ttk.Frame(result_split)
        prefix_panel = ttk.Frame(result_split)
        result_split.add(full_panel, weight=1)
        result_split.add(prefix_panel, weight=1)

        full_header = ttk.Frame(full_panel)
        full_header.pack(fill="x", pady=(0, 6))
        ttk.Label(
            full_header,
            textvariable=self.selected_keyword_label,
            font=("맑은 고딕", 10, "bold"),
        ).pack(side="left", anchor="w")
        ttk.Button(
            full_header,
            text="상단 결과 복사",
            style="Copy.TButton",
            command=lambda: self.copy_widget(
                self.keyword_text, "상단 전체 문구 결과"
            ),
        ).pack(side="right")
        self.keyword_text = ScrolledText(
            full_panel,
            wrap="word",
            font=("Malgun Gothic", 11),
            background="#fbfdff",
            foreground="#172b3a",
            insertbackground="#172b3a",
            selectbackground="#b9dcf7",
            relief="flat",
            padx=12,
            pady=10,
        )
        self.keyword_text.pack(fill="both", expand=True)

        prefix_header = ttk.Frame(prefix_panel)
        prefix_header.pack(fill="x", pady=(8, 6))
        ttk.Label(
            prefix_header,
            textvariable=self.prefix_keyword_label,
            font=("맑은 고딕", 10, "bold"),
        ).pack(side="left", anchor="w")
        ttk.Button(
            prefix_header,
            text="하단 결과 복사",
            style="Copy.TButton",
            command=lambda: self.copy_widget(
                self.keyword_prefix_text, "하단 첫 단어 추가 결과"
            ),
        ).pack(side="right")
        self.keyword_prefix_text = ScrolledText(
            prefix_panel,
            wrap="word",
            font=("Malgun Gothic", 11),
            background="#fbfdff",
            foreground="#172b3a",
            insertbackground="#172b3a",
            selectbackground="#b9dcf7",
            relief="flat",
            padx=12,
            pady=10,
        )
        self.keyword_prefix_text.pack(fill="both", expand=True)
        self._render_keyword_groups({})
        self.root.after(500, self.keyword_search_entry.focus_set)

    def _blog_ui(self):
        self._cli_blog_ui()

    def _comment_ui(self):
        form = ttk.LabelFrame(
            self.comment_tab,
            text="네이버 웨일 댓글 자동화 설정",
            padding=12,
            style="Panel.TLabelframe",
        )
        form.pack(fill="x")
        labels = [
            ("네이버 블로그 ID", self.blog_id, 18),
            ("최근 글 일수", self.comment_days, 8),
            ("답글 간격(초)", self.comment_interval, 8),
            ("이웃 댓글 간격(초)", self.neighbor_interval, 8),
            ("이웃 최대 글 수(최대 200)", self.neighbor_max, 10),
        ]
        for col, (label, variable, width) in enumerate(labels):
            block = ttk.Frame(form)
            block.grid(row=0, column=col, sticky="w", padx=(0, 14))
            ttk.Label(block, text=label).pack(anchor="w")
            ttk.Entry(block, textvariable=variable, width=width).pack(anchor="w", pady=(3, 0))
        ttk.Label(
            self.comment_tab,
            text="내 글: 기존 답글은 건너뛰고 미응답 댓글에만 감사 답글을 남기며, 꺼진 댓글 공감을 누릅니다.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(12, 3))
        ttk.Label(
            self.comment_tab,
            text="이웃 새글: 입력한 간격과 최대 개수에 따라 다양한 문구를 사용합니다. 처리 기록으로 중복 작성을 막습니다.",
            style="Sub.TLabel",
        ).pack(anchor="w")
        actions = ttk.Frame(self.comment_tab)
        actions.pack(fill="x", pady=12)
        ttk.Button(actions, text="내 글 답글·하트 시작", style="Accent.TButton", command=self.start_own_comments).pack(
            side="left", padx=8
        )
        ttk.Button(
            actions,
            text="이웃 새글 댓글 시작",
            style="Copy.TButton",
            command=self.start_neighbor_comments,
        ).pack(side="left")
        ttk.Label(self.comment_tab, text="댓글 작업 상황은 모든 탭 하단의 전체 진행 상황에서 확인합니다.").pack(anchor="w")

    def _naver_log(self, message):
        safe = redact_diagnostic(message)
        self.events.put(("naver_log", safe))
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            warning = diagnostics.write(safe)
            if warning:
                self.events.put(("naver_log", warning))

    def _report_uncaught_exception(self, label, kind, value, trace):
        self.events.put(("naver_log", redact_diagnostic(f"{label}: {kind.__name__}: {value}")))
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            warning = diagnostics.exception(label, kind, value, trace)
            if warning:
                self.events.put(("naver_log", warning))

    def _close_diagnostics(self):
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.close()

    def _start_naver_task(self, label, target, *args):
        if self._browser_task_busy():
            messagebox.showinfo(APP_NAME, "실시간 조회 또는 브라우저 자동화 작업이 실행 중입니다. 완료하거나 중지한 뒤 실행하세요.")
            return
        self.naver_task_active = True
        self.naver_bot.reset_stop()

        def work():
            try:
                if self.naver_bot.stop_event.is_set() or getattr(self, "_closing", False):
                    return
                self._naver_log(f"{label} 준비 중...")
                target(*args)
            except Exception as exc:
                self._naver_log(f"{label} 실패: {exc}")
                self.events.put(("error", f"{label} 실패\n{exc}"))
            finally:
                self.naver_task_active = False

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self.naver_task_active = False
            raise

    def open_naver_login(self):
        self._start_naver_task(
            "네이버 웨일 로그인",
            self.naver_bot.open_login,
            self.blog_id.get().strip() or "macdcross",
        )

    def open_chatgpt_login(self):
        self._start_naver_task(
            "웨일 ChatGPT 열기", self.naver_bot.open_chatgpt_login
        )

    def open_chatgpt_classic(self):
        _selected, keywords = self._selected_phone_keywords()
        if not keywords:
            self.status.set(
                "먼저 1번 실시간 연관 검색어에서 검색어를 선택하거나 연관어를 조회하세요."
            )
            self._naver_log(
                "ChatGPT Classic에 입력할 연관 검색어가 없어 실행하지 않았습니다."
            )
            return
        prompt = "\n".join(keywords)
        self.status.set(
            f"연관 검색어 {len(keywords)}개를 Phone 미래 전망 새 채팅에 입력합니다."
        )
        self._start_naver_task(
            "ChatGPT Classic 연관어 입력",
            self.chatgpt_classic.open_project_and_send,
            prompt,
        )

    def start_google_image_search(self):
        keyword = normalize_keyword(self.seed.get())
        if not keyword:
            messagebox.showinfo(
                APP_NAME,
                "먼저 1번 실시간 연관 검색어 화면에서 검색어 하나를 선택하세요.",
            )
            return
        self.tabs.select(self.image_tab)
        self.google_image_keyword.set(f"선택한 검색어: {keyword}")
        self.google_image_translation.set("영문 번역: 번역 중...")
        self.status.set(f"'{keyword}' 영문 번역 및 Google 이미지 검색 준비 중...")
        self._start_naver_task(
            "Google 이미지 검색",
            self._google_image_search,
            keyword,
        )

    def _google_image_search(self, keyword: str):
        translated = translate_korean_to_english(keyword)
        url = (
            "https://www.google.com/search?"
            + urllib.parse.urlencode(
                {
                    "tbm": "isch",
                    "hl": "en",
                    "q": translated,
                }
            )
        )
        self.last_google_image_url = url
        self.events.put(
            ("google_image_search", keyword, translated, url)
        )
        self.naver_bot.open_url(url)

    def open_last_google_image_search(self):
        if not self.last_google_image_url:
            self.start_google_image_search()
            return
        self._start_naver_task(
            "Google 이미지 검색 결과 다시 열기",
            self.naver_bot.open_url,
            self.last_google_image_url,
        )

    def _google_capture_base_folder(self) -> Path:
        selected = Path(self.folder.get())
        if selected.parent.name == "GoogleImageCapture":
            return selected.parent.parent
        if (
            selected.name == "PictureCleaner"
            and selected.parent.parent.name == "GoogleImageCapture"
        ):
            return selected.parent.parent.parent
        return selected

    def _selected_google_image_keyword(self) -> str:
        keyword = normalize_keyword(self.seed.get())
        if not keyword:
            raise ValueError(
                "먼저 1번 실시간 연관 검색어 화면에서 검색어 하나를 선택하세요."
            )
        return keyword

    def capture_google_images_15(self):
        try:
            keyword = self._selected_google_image_keyword()
        except ValueError as exc:
            messagebox.showinfo(APP_NAME, str(exc))
            return
        self.tabs.select(self.image_tab)
        self._start_naver_task(
            "Google 이미지 15장 크롭 저장",
            self._capture_google_images_worker,
            keyword,
            False,
        )

    def enhance_google_images_15(self):
        self._start_naver_task(
            "최근 Google 이미지 15장 화질·해상도 개선",
            self._enhance_google_images_worker,
            None,
        )

    def capture_and_enhance_google_images_15(self):
        try:
            keyword = self._selected_google_image_keyword()
        except ValueError as exc:
            messagebox.showinfo(APP_NAME, str(exc))
            return
        self.tabs.select(self.image_tab)
        self._start_naver_task(
            "Google 이미지 15장 크롭 → 화질·해상도 순차 작업",
            self._capture_and_enhance_google_images_worker,
            keyword,
        )

    def _capture_google_images_worker(
        self, keyword: str, continue_enhance: bool
    ) -> list[str]:
        translated = translate_korean_to_english(keyword)
        output_dir = (
            self._google_capture_base_folder()
            / "GoogleImageCapture"
            / datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        images = self.naver_bot.capture_google_images(
            translated,
            output_dir,
            count=15,
            enhance=False,
        )
        self.last_google_capture_dir = output_dir
        self.events.put(
            (
                "google_capture_done",
                keyword,
                translated,
                str(output_dir),
                len(images),
            )
        )
        if continue_enhance:
            self._enhance_google_images_worker(output_dir)
        return images

    def _latest_google_capture_dir(self) -> Path:
        if (
            self.last_google_capture_dir
            and self.last_google_capture_dir.is_dir()
        ):
            return self.last_google_capture_dir
        current = Path(self.folder.get())
        if any(current.glob("google_cc_*.jpg")):
            return current
        root = self._google_capture_base_folder() / "GoogleImageCapture"
        candidates = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ) if root.is_dir() else []
        if not candidates:
            raise RuntimeError(
                "먼저 '1. 이미지 15장 크롭 저장'을 실행하세요."
            )
        return candidates[0]

    def _enhance_google_images_worker(
        self, capture_dir: Path | None
    ) -> list[str]:
        capture_dir = Path(capture_dir) if capture_dir else self._latest_google_capture_dir()
        sources = sorted(capture_dir.glob("google_cc_*.jpg"))[:15]
        if len(sources) < 15:
            raise RuntimeError(
                f"화질 개선할 이미지가 {len(sources)}장뿐입니다. "
                "먼저 이미지 15장을 모두 저장하세요."
            )
        output_dir = capture_dir / "PictureCleaner"
        outputs = []
        for index, source in enumerate(sources, 1):
            if self.naver_bot.stop_event.is_set():
                raise RuntimeError("사용자가 작업을 중지했습니다.")
            outputs.append(str(process_image(source, output_dir)))
            self._naver_log(f"화질·해상도 개선 {index}/15 완료")
        self.events.put(
            ("google_enhance_done", str(output_dir), len(outputs))
        )
        return outputs

    def _capture_and_enhance_google_images_worker(self, keyword: str):
        self._capture_google_images_worker(keyword, True)

    def start_full_automation(self):
        self.start_cli_automation()

    def stop_full_automation(self):
        self._cancel_automatic_resume()
        self.full_auto_stop.set()
        self.naver_bot.stop()
        self.status.set("전체 자동화 중지를 요청했습니다.")

    def _full_automation_loop(self, config: dict):
        next_tick = time.monotonic()
        access_problem = None
        try:
            while not self.full_auto_stop.is_set():
                config = automation_config_snapshot(getattr(self, "settings", {}), config)
                try:
                    self._run_full_automation_cycle(config)
                except Exception as exc:
                    access_problem = access_error_from_exception(exc)
                    if access_problem:
                        self._naver_log(str(access_problem))
                        break
                    pending_path = Path(getattr(self, "cli_app_dir", APP_DIR)) / "pending-blog-topic.json"
                    prefix = "확정 주제 보완 대기" if pending_path.exists() else "주제 선정 사전 점검 대기"
                    self._naver_log(f"{prefix}: {exc}")
                    self.events.put(
                        ("auto_error", f"{prefix}: {exc}")
                    )
                if self.full_auto_stop.is_set():
                    break
                finished_at, cycle_tick = time.monotonic(), next_tick
                scheduled_hours = None
                while not self.full_auto_stop.is_set():
                    latest = automation_config_snapshot(getattr(self, "settings", {}), config)
                    now = time.monotonic()
                    if latest["interval_hours"] != scheduled_hours:
                        scheduled_hours = latest["interval_hours"]
                        next_tick = next_cycle_tick(cycle_tick, finished_at, latest["interval_seconds"])
                        expected = datetime.now() + timedelta(seconds=max(0, next_tick - now))
                        self.events.put(("status", f"다음 회차 {expected:%m/%d %H:%M} · {scheduled_hours}시간마다 · {latest.get('completion_label', '자동 발행')}"))
                    remaining = next_tick - now
                    if remaining <= 0 or self.full_auto_stop.wait(min(1.0, remaining)):
                        break
                if self.full_auto_stop.is_set():
                    break
                self.naver_bot.reset_stop()
        finally:
            self.full_auto_active = False
            self.naver_task_active = False
            self.events.put(("cli_idle",))
            self.events.put(("status", "CLI 로그인 대기 중입니다." if access_problem else "전체 자동화가 중지되었습니다."))
            if access_problem:
                self.events.put(("cli_access_required", access_problem, True))

    def _rank_longtail_topics(
        self, groups: dict[str, list[str]], config: dict | None = None
    ) -> tuple[list[dict], dict]:
        blocked_terms = (config if config is not None else self.cli_preferences).get("blocked_terms")
        groups = self.topic_history.filter_groups(groups, include_pending=True)
        history_keys = {keyword_comparison_key(topic) for topic in self.topic_history.blocked_topics()}
        source_counts: dict[str, int] = {}
        display_values: dict[str, str] = {}
        for words in groups.values():
            for word in words:
                normalized = normalize_keyword(word)
                hits = blocked_term_hits(normalized, blocked_terms)
                if hits:
                    self._naver_log(f"후보 차단 · {normalized}: {', '.join(hits)}")
                    continue
                key = keyword_comparison_key(normalized)
                if (
                    key
                    and key not in history_keys
                    and not is_ephemeral_keyword(normalized)
                ):
                    source_counts[key] = source_counts.get(key, 0) + 1
                    display_values.setdefault(key, normalized)
        candidates = list(display_values.values())
        if len(candidates) < 3:
            for stored in self.topic_history.filter_keywords(getattr(self, "keyword_db", []), include_pending=True):
                normalized = normalize_keyword(stored)
                key = keyword_comparison_key(normalized)
                if key and key not in display_values and not is_ephemeral_keyword(normalized) and not blocked_term_hits(normalized, blocked_terms):
                    display_values[key] = normalized
                    candidates.append(normalized)
                    if len(candidates) >= 3: break
        if not candidates:
            raise RuntimeError("반복되지 않은 롱테일 후보 검색어가 없습니다.")

        related_by_topic: dict[str, dict[str, list[str]]] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
            futures = {
                executor.submit(fetch_autocomplete, topic): topic
                for topic in candidates
            }
            for future in as_completed(futures):
                if self.full_auto_stop.is_set():
                    raise RuntimeError("사용자가 전체 자동화를 중지했습니다.")
                topic = futures[future]
                try:
                    related_by_topic[topic] = future.result()
                except Exception:
                    related_by_topic[topic] = {}

        ranking_groups = {source: [word for word in words if normalize_keyword(word) in candidates] for source, words in groups.items()}
        collected = {normalize_keyword(word) for words in ranking_groups.values() for word in words}
        stored_candidates = [word for word in candidates if word not in collected]
        if stored_candidates:
            ranking_groups["저장된 미사용 검색어"] = stored_candidates
        ranked = BlogWorkflow.rank_topics(ranking_groups, related_by_topic,
            exclude_topics=self.topic_history.blocked_topics(), blocked_terms=blocked_terms)
        preferences = config if config is not None else self.cli_preferences
        ranked = [candidate for candidate in ranked if self.topic_history.is_duplicate(
            candidate["topic"], candidate.get("keywords", []), candidate["topic"],
            keyword_threshold=preferences.get("duplicate_keyword_threshold", .4),
            title_threshold=preferences.get("duplicate_title_threshold", .5)) is not True]
        for candidate, related in related_by_topic.items():
            hits = blocked_term_hits(related, blocked_terms)
            if hits:
                self._naver_log(f"연관어 차단 · {candidate}: {', '.join(hits)}")
        if not ranked:
            raise RuntimeError("연관 검색어가 충분한 관심 주제를 찾지 못했습니다.")
        self.events.put(("cli_ranking", ranked[:10]))
        return ranked, related_by_topic

    def _select_longtail_topic(self, groups, config=None):
        ranked, related_by_topic = self._rank_longtail_topics(groups, config=config)
        selector = BlogWorkflow(self.cli_bridge, self.cli_app_dir / "blog-runs", self._naver_log, self.full_auto_stop)
        provider = config["steps"][0] if config is not None else self.cli_preferences["order"][0]
        models = config["models"] if config is not None else self.cli_preferences["models"]
        blocked_terms = (config if config is not None else self.cli_preferences).get("blocked_terms")
        selection_options = {"provider": provider, "model": models.get(provider, ""), "blocked_terms": blocked_terms}
        recent = self.topic_history.recent_publications(30)
        if isinstance(recent, list):
            selection_options["recent_publications"] = recent
        choice = selector.select_topic(ranked[:12], **selection_options)
        topic, related = choice["topic"], choice["keywords"]
        self._naver_log(f"선정: {topic} · 예상 관심 점수 {choice['score']} · {choice['reason']} (실제 CTR 아님)")

        return topic, related, related_by_topic.get(choice.get("source_topic", topic), {})

    def _run_full_automation_cycle(self, config: dict):
        self._cli_automation_cycle(config)

    def _selected_phone_keywords(self) -> tuple[str, list[str]]:
        selected = normalize_keyword(self.seed.get() or self.topic.get())
        values = [selected] if selected else []
        for widget in (self.keyword_text, self.keyword_prefix_text):
            values.extend(
                normalize_keyword(line)
                for line in widget.get("1.0", "end").splitlines()
                if normalize_keyword(line)
            )
        keywords = []
        seen = set()
        for value in values:
            key = keyword_comparison_key(value)
            if key and key not in seen:
                seen.add(key)
                keywords.append(value)
        return selected or (keywords[0] if keywords else ""), keywords

    def start_phone_workflow(self):
        selected, keywords = self._selected_phone_keywords()
        if not keywords:
            self.status.set(
                "먼저 2번 화면에서 검색어를 선택하거나 직접 검색해 연관어를 조회하세요."
            )
            self._naver_log(
                "선택된 연관 검색어가 없어 작업을 시작하지 않았습니다."
            )
            return
        config = {
            "topic": selected,
            "keywords": keywords,
            "base": self.base_text.get("1.0", "end").strip(),
            "folder": self.folder.get(),
            "images": self.phone_auto_images.get(),
            "blog_id": self.blog_id.get().strip() or "macdcross",
        }
        self.status.set(
            f"선택 연관어 {len(keywords)}개로 ChatGPT Classic 자동화를 바로 시작합니다."
        )
        self._start_naver_task(
            "ChatGPT Classic Phone 미래 전망 → 웨일 임시저장",
            self._phone_workflow,
            config,
        )

    def _phone_workflow(self, config):
        try:
            selected = normalize_keyword(config["topic"])
            keywords = [
                normalize_keyword(keyword)
                for keyword in config["keywords"]
                if normalize_keyword(keyword)
            ]
            if not selected or not keywords:
                raise RuntimeError(
                    "선택된 검색어 또는 연관 검색어가 없습니다. "
                    "2번 화면에서 연관어를 조회한 뒤 다시 실행해 주세요."
                )
            self.events.put(("phone_topic", selected))
            self._naver_log(
                f"선택 주제: {selected} · ChatGPT Classic에 입력할 연관어 {len(keywords)}개"
            )
            prompt_parts = ["\n".join(keywords)]
            if config["base"]:
                prompt_parts.append(config["base"])
            generated = self.chatgpt_classic.generate_phone_future(
                "\n\n".join(prompt_parts)
            )
            self.events.put(("blog", generated))
            pictures = []
            if config["images"]:
                pictures = self._today_blog_images(config["folder"])
            self.naver_bot.save_naver_draft(
                config["blog_id"], generated, pictures
            )
        except Exception as exc:
            self.events.put(("error", f"Phone 미래 전망 자동화 실패\n{exc}"))

    @staticmethod
    def _positive_int(value, label, minimum=0):
        try:
            parsed = int(value)
            if parsed < minimum:
                raise ValueError
            return parsed
        except Exception:
            raise ValueError(f"{label}은(는) {minimum} 이상의 숫자로 입력하세요.")

    def start_own_comments(self):
        try:
            days = self._positive_int(self.comment_days.get(), "최근 글 일수", 1)
            interval = self._positive_int(self.comment_interval.get(), "답글 간격", 0)
        except ValueError as exc:
            messagebox.showinfo(APP_NAME, str(exc))
            return
        self.status.set(f"최근 {days}일 미응답 댓글·하트 작업을 바로 시작합니다.")
        self._start_naver_task(
            "내 글 답글·하트",
            self.naver_bot.run_own_posts,
            self.blog_id.get().strip(),
            days,
            interval,
            True,
        )

    def start_neighbor_comments(self):
        try:
            interval = self._positive_int(self.neighbor_interval.get(), "이웃 댓글 간격", 10)
            maximum = self._positive_int(self.neighbor_max.get(), "이웃 최대 글 수", 1)
        except ValueError as exc:
            messagebox.showinfo(APP_NAME, str(exc))
            return
        if maximum > 200:
            messagebox.showinfo(APP_NAME, "한 번에 처리할 이웃 새글은 최대 200개입니다.")
            return
        self.status.set(f"이웃 새글 {maximum}개, {interval}초 간격 작업을 바로 시작합니다.")
        self._start_naver_task(
            "이웃 새글 댓글",
            self.naver_bot.run_neighbor_posts,
            self.blog_id.get().strip(),
            interval,
            maximum,
        )

    def choose_folder(self):
        selected = filedialog.askdirectory(initialdir=self.folder.get())
        if selected:
            self.folder.set(selected)
            self.refresh_images()

    def refresh_images(self):
        self.image_paths = image_candidates(Path(self.folder.get()), self.today_only.get())
        self.image_list.delete(0, "end")
        for path in self.image_paths:
            self.image_list.insert("end", path.name)
        self.status.set(f"처리 가능한 사진 {len(self.image_paths)}개")

    def preview_image(self, _event=None):
        selected = self.image_list.curselection()
        if not selected:
            return
        try:
            with Image.open(self.image_paths[selected[0]]) as image:
                view = ImageOps.exif_transpose(image).copy()
            view.thumbnail((680, 480), Image.Resampling.LANCZOS)
            self.preview_ref = ImageTk.PhotoImage(view)
            self.preview.configure(image=self.preview_ref, text="")
        except Exception as exc:
            self.preview.configure(image="", text=f"미리보기 실패: {exc}")

    def run_images(self):
        paths = list(self.image_paths)
        if not paths:
            messagebox.showinfo(APP_NAME, "처리할 사진이 없습니다.")
            return
        folder = Path(self.folder.get())
        output = folder / "PictureCleaner"
        recycle = self.recycle.get()

        def work():
            done, failed, outputs = 0, [], []
            for source in paths:
                self.events.put(("status", f"{source.name} 처리 중..."))
                try:
                    outputs.append(process_image(source, output))
                    done += 1
                    if recycle:
                        send2trash(str(source))
                except Exception as exc:
                    failed.append(f"{source.name}: {exc}")
            self.last_outputs = outputs
            self.events.put(("images_done", done, failed, output))

        threading.Thread(target=work, daemon=True).start()

    def open_output(self):
        output = Path(self.folder.get()) / "PictureCleaner"
        output.mkdir(parents=True, exist_ok=True)
        os.startfile(output)

    def run_manual_related(self, _event=None):
        if not normalize_keyword(self.seed.get()):
            messagebox.showinfo(APP_NAME, "검색할 키워드를 입력하세요.")
            self.keyword_search_entry.focus_set()
            return "break"
        self.selected_realtime.set("")
        for variable in self.keyword_checks.values():
            variable.set(False)
        self.run_related()
        return "break"

    def run_related(self):
        seed = normalize_keyword(self.seed.get())
        if not seed:
            messagebox.showinfo(APP_NAME, "검색할 키워드를 입력하세요.")
            return
        self.related_request_id += 1
        request_id = self.related_request_id
        parts = seed.split()
        prefix = parts[0] if len(parts) > 1 else ""
        self.seed.set(seed)
        self.selected_keyword_label.set(f"전체 문구: {seed}")
        self.prefix_keyword_label.set(
            f"앞 단어 추가 검색: {prefix}"
            if prefix else "앞 단어 추가 검색: 여러 단어를 입력하면 표시됩니다"
        )
        self.keyword_text.delete("1.0", "end")
        self.keyword_prefix_text.delete("1.0", "end")

        def work():
            self.events.put(("status", f"'{seed}' 연관 검색어 조회 중..."))
            if prefix:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    full_future = executor.submit(fetch_autocomplete, seed)
                    prefix_future = executor.submit(fetch_autocomplete, prefix)
                    full_result = full_future.result()
                    prefix_result = prefix_future.result()
            else:
                full_result = fetch_autocomplete(seed)
                prefix_result = {}
            self.events.put(
                (
                    "related_split",
                    request_id,
                    seed,
                    prefix,
                    full_result,
                    prefix_result,
                )
            )

        threading.Thread(target=work, daemon=True).start()

    def run_realtime(self, *, startup=False):
        # Claim the shared Whale session on the Tk thread before the worker starts.
        if self._browser_task_busy():
            if startup:
                self._launch_auto_pending = False
            self.status.set("진행 중인 브라우저 작업이 있어 실시간 조회를 시작하지 않았습니다.")
            return False
        self.realtime_task_active = True
        blog_id = self.blog_id.get().strip() or "macdcross"
        self.full_auto_stop.clear()
        self.naver_bot.reset_stop()

        def work():
            try:
                self._run_realtime_worker(blog_id)
            except Exception as exc:
                self._naver_log(f"실시간 검색어 조회 실패: {exc}")
                self.events.put(("status", f"실시간 검색어 조회 실패: {exc}"))
            finally:
                self.realtime_task_active = False
                self.events.put(("realtime_finished", startup))

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self.realtime_task_active = False
            self.events.put(("realtime_finished", startup))
            raise
        return True

    def _run_realtime_worker(self, blog_id):
        def cancelled():
            return self.full_auto_stop.is_set() or self.naver_bot.stop_event.is_set()

        if cancelled():
            return
        self.events.put(("status", "실시간 검색어와 비즈니스·경제·IT·컴퓨터 인기 주제 검색어를 불러오는 중..."))
        groups = fetch_realtime_groups()
        if cancelled():
            return
        try:
            groups["애드센스팜"] = self.naver_bot.fetch_adsensefarm_realtime(50)
        except Exception as exc:
            groups["애드센스팜"] = []
            self._naver_log(f"애드센스팜 실시간 검색어 조회 실패: {exc}")
        if cancelled():
            return
        if groups.get("다음"):
            self._naver_log(f"다음 홈페이지에서 실시간 트렌드 {len(groups['다음'])}개를 직접 가져왔습니다.")
        else:
            try:
                groups["다음"] = self.naver_bot.fetch_daum_realtime_trends(10)
                self._naver_log(f"다음 직접 요청이 비어 있어 웨일 화면에서 {len(groups['다음'])}개를 확인했습니다.")
            except Exception as exc:
                groups["다음"] = []
                self._naver_log(f"다음 실시간 트렌드 조회 실패: {exc}")
        if cancelled():
            return
        try:
            google_trends = self.naver_bot.fetch_google_trending_now(100)
            if google_trends:
                groups["구글"] = google_trends
        except Exception as exc:
            self._naver_log(f"Google 급상승 검색어 조회 실패 · 기존 RSS 결과를 사용합니다: {exc}")
        for category in ("비즈니스·경제", "IT·컴퓨터"):
            if cancelled():
                return
            source_name = f"크리에이터 어드바이저 · {category}"
            try:
                groups[source_name] = self.naver_bot.fetch_creator_advisor_trends(blog_id, category, 20)
            except Exception as exc:
                groups[source_name] = []
                self._naver_log(f"크리에이터 어드바이저 {category} 조회 실패: {exc}")
        blocked_terms = self.cli_preferences.get("blocked_terms") if hasattr(self, "cli_preferences") else None
        for source, values in list(groups.items()):
            groups[source] = [word for word in values if not is_ephemeral_keyword(word)
                              and not blocked_term_hits(word, blocked_terms)]
        if not cancelled():
            self.events.put(("realtime_groups", groups))

    def use_keyword(self):
        selected = self.seed.get().strip()
        if selected:
            self.topic.set(selected)
            self.status.set(f"블로그 주제로 설정: {selected}")

    def restore_default_blog_prompt(self):
        self.base_text.delete("1.0", "end")
        self.base_text.insert("1.0", DEFAULT_BLOG_PROMPT)
        self._schedule_prompt_save()
        self.status.set("정리된 기본 블로그 프롬프트를 복원했습니다.")

    def save_blog_prompt(self):
        return self.save_cli_prompt()

    def _all_related_keywords(self) -> list[str]:
        keywords: list[str] = []
        seen_keywords: set[str] = set()
        for widget in (self.keyword_text, self.keyword_prefix_text):
            for line in widget.get("1.0", "end").splitlines():
                keyword = normalize_keyword(line)
                key_value = keyword_comparison_key(keyword)
                if key_value and key_value not in seen_keywords:
                    seen_keywords.add(key_value)
                    keywords.append(keyword)
        return keywords

    def _blog_generation_input(self) -> tuple[str, list[str], str]:
        keywords = self._all_related_keywords()
        # The top topic field is the authoritative generation input.
        # Related keywords enrich the prompt but never replace typed text.
        topic = normalize_keyword(self.topic.get())
        if not topic:
            topic = compose_related_topic(
                normalize_keyword(self.seed.get()),
                keywords,
            )
        if not topic:
            raise ValueError("상단의 주제 입력어를 입력하세요.")
        self.topic.set(topic)
        editable_prompt = self.base_text.get("1.0", "end").strip()
        if not editable_prompt:
            raise ValueError("글 생성 프롬프트를 입력하세요.")
        return topic, keywords, build_prompt(
            topic,
            keywords,
            editable_prompt,
            self.image_slots.get(),
        )

    def open_antigravity_cli_login(self):
        self.show_cli_login_help()

    def run_cli_generate(self, provider: str):
        # Legacy shortcut now follows the configured CLI workflow.
        self.prepare_cli_article()

    def run_generate(self):
        self.prepare_cli_article()

    def copy_widget(self, widget, description="결과"):
        text = widget.get("1.0", "end").strip()
        if not text:
            self.status.set(f"복사할 {description}가 없습니다.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        line_count = len([line for line in text.splitlines() if line.strip()])
        self.status.set(f"{description}를 복사했습니다. ({line_count}개)")

    def copy_all_related(self):
        lines = []
        seen = set()
        for widget in (self.keyword_text, self.keyword_prefix_text):
            for line in widget.get("1.0", "end").splitlines():
                normalized = normalize_keyword(line)
                key = keyword_comparison_key(normalized)
                if key and key not in seen:
                    seen.add(key)
                    lines.append(normalized)
        if not lines:
            self.status.set("복사할 연관 검색어 결과가 없습니다.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.root.update()
        self.status.set(f"상단·하단 결과 {len(lines)}개를 모두 복사했습니다.")

    def copy_related_only(self):
        lines = [
            line.strip() for line in self.keyword_text.get("1.0", "end").splitlines()
            if line.strip() and not line.strip().startswith("[") and line.strip() != "조회 결과 없음"
        ]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status.set(f"제목을 제외한 연관 검색어 {len(lines)}개만 복사했습니다.")

    def select_realtime_keyword(self, keyword):
        self.selected_realtime.set(keyword)
        self.seed.set(keyword)
        self.status.set(f"'{keyword}' 연관 검색어를 조회합니다.")
        self.run_related()

    def show_realtime_sources(self, result):
        for child in self.realtime_list.winfo_children():
            child.destroy()
        self.realtime_by_source = result
        crawled_total = sum(len(values) for values in result.values())
        usable_total = 0
        for row, source in enumerate(
            (
                "애드센스팜",
                "네이버 시그널",
                "다음",
                "구글",
                "크리에이터 어드바이저",
            )
        ):
            section = ttk.LabelFrame(self.realtime_list, text=source, padding=8)
            section.grid(row=row, column=0, sticky="ew", padx=5, pady=3)
            values = [word for word in result.get(source, []) if not is_ephemeral_keyword(word)]
            usable_total += len(values)
            for rank, keyword in enumerate(values, 1):
                ttk.Radiobutton(
                    section,
                    text=f"{rank}. {keyword}",
                    value=keyword,
                    variable=self.selected_realtime,
                    command=lambda word=keyword: self.select_realtime_keyword(word),
                ).pack(anchor="w", fill="x", pady=2)
        self.realtime_list.columnconfigure(0, weight=1)
        self.status.set(
            f"실시간 검색어 {crawled_total}개 수집 · 일회성 제외 {usable_total}개 · 한 개를 선택하세요."
        )

    def open_naver(self):
        self._start_naver_task(
            "웨일 네이버 글쓰기",
            self.naver_bot.open_blog_writer,
            self.blog_id.get().strip() or "macdcross",
        )

    def _today_blog_images(self, selected_folder: str | Path | None = None) -> list[str]:
        folder = Path(selected_folder or self.folder.get())
        candidates: list[Path] = []
        cleaned = folder / "PictureCleaner"
        if cleaned.exists():
            candidates = image_candidates(cleaned, True)
        if not candidates and folder.exists():
            candidates = image_candidates(folder, True)
        # 오늘 촬영분이 많을 때는 가장 최근 10장을 시간순으로 첨부한다.
        return [str(path) for path in candidates[-10:]]

    def save_blog_draft(self):
        generated = self.blog_result.get("1.0", "end").strip()
        if not generated:
            messagebox.showinfo(APP_NAME, "먼저 블로그 초안을 생성하거나 결과창에 내용을 입력하세요.")
            return
        images = self._today_blog_images() if self.blog_auto_images.get() else []
        self.status.set(
            f"웨일에 제목·본문을 입력하고 사진 {len(images)}개를 첨부해 임시저장합니다."
        )
        self._start_naver_task(
            "웨일 블로그 임시저장",
            self.naver_bot.save_naver_draft,
            self.blog_id.get().strip() or "macdcross",
            generated,
            images,
        )

    def _poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if self._handle_runtime_event(event):
                    continue
                if kind == "status":
                    self.status.set(event[1])
                elif kind == "error":
                    self.status.set("오류")
                    messagebox.showerror(APP_NAME, event[1])
                elif kind == "images_done":
                    _, done, failed, output = event
                    self.status.set(f"{done}개 저장 완료 · 실패 {len(failed)}개 · {output}")
                    messagebox.showinfo(APP_NAME, f"{done}개 크롭·화질 개선 완료\n실패 {len(failed)}개")
                    self.refresh_images()
                elif kind == "keywords":
                    _, seed, result = event
                    merged = []
                    failed_sources = []
                    for source, words in result.items():
                        usable = [word for word in words if normalize_keyword(word)]
                        if usable:
                            merged.extend(usable)
                        else:
                            failed_sources.append(source)
                    merged = list(dict.fromkeys(merged))
                    self.keyword_text.delete("1.0", "end")
                    if merged:
                        self.keyword_text.insert("1.0", "\n".join(merged))
                    self.seed.set(seed)
                    self.topic.set(compose_related_topic(seed, merged))
                    self._update_keyword_queue(observed=merged)
                    if merged and failed_sources:
                        self.status.set(
                            f"'{seed}' 연관 검색어 {len(merged)}개 · "
                            f"{', '.join(failed_sources)} 조회 결과 없음"
                        )
                    elif merged:
                        self.status.set(f"'{seed}' 연관 검색어 {len(merged)}개 조회 완료")
                    else:
                        self.status.set(
                            f"'{seed}' 연관 검색어 조회 결과 없음 · "
                            f"{', '.join(failed_sources) or '전체 출처'}"
                        )
                elif kind == "realtime":
                    result = event[1]
                    self.show_realtime_sources(result)
                elif kind == "related_split":
                    (
                        _,
                        request_id,
                        seed,
                        requested_prefix,
                        full_result,
                        prefix_result,
                    ) = event
                    if (
                        request_id != self.related_request_id
                        or normalize_keyword(self.seed.get()) != seed
                    ):
                        continue
                    prefix, full_keywords, prefix_keywords = (
                        split_related_keywords(seed, full_result, prefix_result)
                    )
                    self.keyword_text.delete("1.0", "end")
                    self.keyword_prefix_text.delete("1.0", "end")
                    if full_keywords:
                        self.keyword_text.insert("1.0", "\n".join(full_keywords))
                    if prefix_keywords:
                        self.keyword_prefix_text.insert(
                            "1.0", "\n".join(prefix_keywords)
                        )
                    self.selected_keyword_label.set(f"전체 문구: {seed}")
                    self.prefix_keyword_label.set(
                        f"앞 단어 추가 검색: {prefix}"
                        if prefix
                        else "앞 단어 추가 검색: 여러 단어를 입력하면 표시됩니다"
                    )
                    combined_keywords = full_keywords + prefix_keywords
                    self.topic.set(
                        compose_related_topic(seed, combined_keywords)
                    )
                    self._update_keyword_queue(observed=combined_keywords)
                    full_failed = [
                        source for source, words in full_result.items() if not words
                    ]
                    prefix_failed = [
                        source for source, words in prefix_result.items() if not words
                    ]
                    failed_parts = []
                    if full_failed:
                        failed_parts.append(
                            f"전체 문구 실패: {', '.join(full_failed)}"
                        )
                    if requested_prefix and prefix_failed:
                        failed_parts.append(
                            f"앞 단어 실패: {', '.join(prefix_failed)}"
                        )
                    failed_text = (
                        f" · {' / '.join(failed_parts)}" if failed_parts else ""
                    )
                    if combined_keywords:
                        prefix_count = (
                            f" · 앞 단어 추가 {len(prefix_keywords)}개"
                            if prefix else ""
                        )
                        self.status.set(
                            f"전체 문구 {len(full_keywords)}개"
                            f"{prefix_count} 조회 완료{failed_text}"
                        )
                    else:
                        self.status.set(f"연관 검색어 조회 결과 없음{failed_text}")
                elif kind == "realtime_groups":
                    groups = self.topic_history.filter_groups(event[1])
                    self.realtime_groups = groups
                    self._render_keyword_groups(groups)
                    total = sum(len(words) for words in groups.values())
                    failed_sources = [
                        source for source, words in groups.items() if not words
                    ]
                    failed_text = (
                        f" · 조회 실패: {', '.join(failed_sources)}"
                        if failed_sources else ""
                    )
                    self.status.set(
                        f"일회성 키워드 제외 · 실시간 검색어 {total}개 수집 완료"
                        f"{failed_text}"
                    )
                elif kind == "blog":
                    self.blog_result.delete("1.0", "end")
                    self.blog_result.insert("1.0", event[1])
                    self.status.set("블로그 초안 생성 완료")
                elif kind == "google_image_search":
                    _, keyword, translated, url = event
                    self.last_google_image_url = url
                    self.google_image_keyword.set(
                        f"선택한 검색어: {keyword}"
                    )
                    self.google_image_translation.set(
                        f"영문 번역: {translated}"
                    )
                    self.status.set(
                        f"'{translated}' Google 이미지 검색 결과를 웨일에서 열었습니다."
                    )
                elif kind == "google_capture_done":
                    _, keyword, translated, output_dir, count = event
                    self.last_google_capture_dir = Path(output_dir)
                    self.folder.set(output_dir)
                    self.google_image_keyword.set(
                        f"선택한 검색어: {keyword}"
                    )
                    self.google_image_translation.set(
                        f"영문 번역: {translated}"
                    )
                    self.refresh_images()
                    self.status.set(
                        f"Google 이미지 {count}장 크롭 저장 완료 · {output_dir}"
                    )
                elif kind == "google_enhance_done":
                    _, output_dir, count = event
                    self.folder.set(output_dir)
                    self.refresh_images()
                    self.status.set(
                        f"이미지 {count}장 화질·해상도 개선 완료 · {output_dir}"
                    )
                elif kind == "auto_topic":
                    _, groups, topic, keywords, _sources = event
                    self.realtime_groups = groups
                    self._render_keyword_groups(groups)
                    self.seed.set(topic)
                    self.topic.set(topic)
                    self.selected_keyword_label.set(
                        f"자동 선정 롱테일 검색어: {topic}"
                    )
                    self.keyword_text.delete("1.0", "end")
                    self.keyword_prefix_text.delete("1.0", "end")
                    self.keyword_text.insert("1.0", "\n".join(keywords))
                    self.status.set(
                        f"'{topic}' 자동 선정 · 연관 검색어 {len(keywords)}개"
                    )
                elif kind == "auto_error":
                    self.status.set(event[1])
                elif kind == "cli_status":
                    self.cli_capability_text.set(event[1])
                elif kind == "cli_idle":
                    self._set_cli_runtime_controls(False)
                elif kind == "cli_topic_consumed":
                    self.realtime_groups = self.topic_history.filter_groups(self.realtime_groups)
                    consumed = event[2] if len(event) > 2 else [event[1]]
                    self._update_keyword_queue(consumed=consumed)
                    self._render_keyword_groups(self.realtime_groups)
                    self.status.set(f"발행한 키워드 '{event[1]}' 제거 완료 · 다음 회차에는 다른 키워드를 선택합니다.")
                elif kind == "cli_preparing":
                    self.cli_article = None
                    self.blog_result.delete("1.0", "end")
                    self.status.set(f"'{event[1]}' 글·이미지 준비 중")
                elif kind == "cli_article":
                    self.cli_article = event[1]
                    self.blog_result.delete("1.0", "end")
                    self.blog_result.insert("1.0", event[1]["text"])
                    self.status.set("검수 완료 · 8문단과 이미지 준비됨 · 실행 자료 폴더에서 검수 기록 확인")
                elif kind == "cli_publication":
                    result = event[1]
                    self.status.set(("네이버 발행 완료: " + str(result.get("url", ""))) if result.get("published") else ("네이버 임시저장 완료" if result.get("saved") else ("네이버 글 입력 완료" if result.get("status") == "prepared" else str(result.get("message", "완료 확인 필요")))))
                elif kind == "cli_ranking":
                    self.cli_log.configure(state="normal")
                    self.cli_log.insert("end", "\n예상 관심 순위 (실제 CTR 아님)\n")
                    for rank, row in enumerate(event[1], 1):
                        self.cli_log.insert("end", f"{rank}. {row['topic']} · {row['score']} · {row['reason']}\n")
                    self.cli_log.configure(state="disabled")
                elif kind == "naver_log":
                    if hasattr(self, "progress_panel"):
                        self.progress_panel.append(f"[{datetime.now():%H:%M:%S}] {event[1]}")
                    if hasattr(self, "cli_log"):
                        self.cli_log.configure(state="normal")
                        self.cli_log.insert("end", f"[{datetime.now():%H:%M:%S}] {event[1]}\n")
                        lines = int(self.cli_log.index("end-1c").split(".")[0])
                        if lines > 5000:
                            self.cli_log.delete("1.0", f"{lines - 5000 + 1}.0")
                        self.cli_log.see("end")
                        self.cli_log.configure(state="disabled")
                    self.status.set(event[1])
                elif kind == "phone_topic":
                    self.topic.set(event[1])
        except queue.Empty:
            pass
        self._maybe_launch_automation()
        self.root.after(100, self._poll)

    def close(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self._cancel_automatic_resume()
        self.full_auto_stop.set()
        self.naver_bot.stop()
        try:
            if hasattr(self, "cli_preferences"):
                current = next(p for p in self.cli_preferences["prompts"] if p["id"] == self.cli_active_prompt)
                edited = self.base_text.get("1.0", "end").strip()
                if edited:
                    current["text"] = edited
                self._save_cli_selection()
            self.settings.update(self._general_settings_snapshot())
            save_json(CONFIG_FILE, {**self.settings, "cli_workflow": self.cli_preferences,
                                   "blog_prompt": self.base_text.get("1.0", "end").strip()})
        except (OSError, ValueError) as exc:
            self._closing = False
            self.status.set(f"설정 저장에 실패해 창을 유지합니다. 작업은 중지했습니다: {exc}")
            self._naver_log(f"종료 설정 저장 실패: {exc}")
            return
        self._finish_close_when_idle()

    def _finish_close_when_idle(self):
        if self._browser_task_busy():
            self.status.set("작업이 안전하게 중지되면 프로그램을 종료합니다.")
            self.root.after(100, self._finish_close_when_idle)
            return
        try:
            self.naver_bot.close()
        except Exception:
            self._report_uncaught_exception("브라우저 종료 예외", *sys.exc_info())
        finally:
            self._naver_log("프로그램 종료 · Blog")
            self._close_diagnostics()
            self.root.destroy()


def main():
    wait_for_restart_parent(sys.argv[1:])
    try:
        with application_instance_lock(APP_DIR):
            root = Tk()
            application = None
            try:
                application = PictureCleanerApp(root)
                with capture_thread_exceptions(application._report_uncaught_exception):
                    root.mainloop()
            except Exception:
                if application is not None:
                    application._report_uncaught_exception("프로그램 예외", *sys.exc_info())
                else:
                    diagnostics = BlogDiagnostics(APP_DIR / "logs" / "blog.log")
                    diagnostics.exception("프로그램 시작 예외", *sys.exc_info())
                    diagnostics.close()
                raise
            finally:
                if application is not None:
                    application._close_diagnostics()
    except ApplicationAlreadyRunning as exc:
        notice = Tk()
        notice.withdraw()
        try:
            messagebox.showinfo(APP_NAME, str(exc), parent=notice)
        finally:
            notice.destroy()


if __name__ == "__main__":
    main()
