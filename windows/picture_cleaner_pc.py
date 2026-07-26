from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageTk
from send2trash import send2trash
from naver_automation import NaverAutomation


APP_NAME = "Picture Cleaner PC"
APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "PictureCleanerPC"
CONFIG_FILE = APP_DIR / "settings.json"
DB_FILE = APP_DIR / "keywords.json"
SUPPORTED = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


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
        if path.parent.name == "PictureCleaner":
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


def process_image(source: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image = image.crop(detect_content_bounds(image))
        long_side = max(image.size)
        if long_side < 2048:
            ratio = 2048 / long_side
            image = image.resize(
                (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        image = image.filter(ImageFilter.UnsharpMask(radius=1.2, percent=70, threshold=3))
        image = ImageEnhance.Contrast(image).enhance(1.02)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        output = output_dir / f"cleaned_{stamp}_{source.stem[:30]}.jpg"
        image.save(output, "JPEG", quality=95, optimize=True)
    return output


def normalize_keyword(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f]+", " ", value)).strip()


def fetch_autocomplete(seed: str) -> dict[str, list[str]]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    encoded = urllib.parse.quote(seed)
    endpoints = {
        "네이버": f"https://ac.search.naver.com/nx/ac?q={encoded}&con=0&frm=nv&ans=2&r_format=json&r_enc=UTF-8",
        "다음": f"https://suggest-bar.daum.net/suggest?id=daum&cate=pc&cmd=suggest&rt=json&utf_in=1&q={encoded}",
        "구글": f"https://suggestqueries.google.com/complete/search?client=firefox&hl=ko&q={encoded}",
    }
    output: dict[str, list[str]] = {}
    for name, url in endpoints.items():
        try:
            data = requests.get(url, headers=headers, timeout=12).json()
            values: list[str] = []
            if name == "네이버":
                for group in data.get("items", []):
                    for item in group:
                        if isinstance(item, list) and item:
                            values.append(str(item[0]))
            elif name == "다음":
                values = list(data.get("items", {}).get(seed, []))
                if not values and data.get("items"):
                    values = list(next(iter(data["items"].values()), []))
            else:
                values = list(data[1]) if isinstance(data, list) and len(data) > 1 else []
            output[name] = list(dict.fromkeys(normalize_keyword(v) for v in values if normalize_keyword(v)))[:15]
        except Exception:
            output[name] = []
    return output


def is_ephemeral_keyword(keyword: str) -> bool:
    """경기 결과처럼 확인 후 바로 검색 수요가 사라지는 단기 키워드를 거른다."""
    value = normalize_keyword(keyword).lower()
    patterns = (
        r"\b\d+\s*[:대-]\s*\d+\b", r"\b(vs|선발\s*라인업|경기\s*결과|스코어|중계|생중계)\b",
        r"(축구|야구|농구|배구|골프).*(결과|스코어|중계|라인업)",
        r"(결과|스코어|중계|라인업).*(축구|야구|농구|배구|골프)",
        r"(로또|복권).*(당첨|번호|추첨)", r"(당첨|추첨).*(결과|번호)",
    )
    return any(re.search(pattern, value, re.I) for pattern in patterns)


def fetch_realtime() -> dict[str, list[str]]:
    """두 페이지를 실제 브라우저로 렌더링해 4개 출처에서 각 10개를 수집한다."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    output = {"다음": [], "구글": [], "크리에이터 어드바이저": [], "네이버 시그널": []}
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1400,1200")
    options.add_argument(f"--user-agent={USER_AGENT}")
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.get("https://adsensefarm.kr/realtime")
        WebDriverWait(driver, 18).until(
            lambda d: any(
                e.text.strip() not in ("", "-")
                for e in d.find_elements(By.CSS_SELECTOR, ".item .kwds .keyword")
            )
        )
        definitions = {
            "다음": ("다음 실시간 검색어",),
            "구글": ("구글 실시간 검색어",),
            "크리에이터 어드바이저": ("크리에이터 어드바이저 검색어", "네이버 실시간 검색어"),
        }
        for source, titles in definitions.items():
            for card in driver.find_elements(By.CSS_SELECTOR, ".item"):
                heading = " ".join(e.text.strip() for e in card.find_elements(By.CSS_SELECTOR, "h2"))
                if heading not in titles:
                    continue
                output[source] = [
                    normalize_keyword(e.text)
                    for e in card.find_elements(By.CSS_SELECTOR, ".kwds .keyword")
                    if normalize_keyword(e.text) not in ("", "-")
                ][:10]
                break

        driver.get("https://www.signal.bz/")
        WebDriverWait(driver, 18).until(
            lambda d: len(d.find_elements(By.CSS_SELECTOR, ".realtime-rank .rank-text, .rank-text")) > 0
        )
        output["네이버 시그널"] = [
            normalize_keyword(e.text)
            for e in driver.find_elements(By.CSS_SELECTOR, ".realtime-rank .rank-text, .rank-text")
            if normalize_keyword(e.text)
        ][:10]
    except Exception:
        # 한 출처의 일시 오류가 있더라도 확보한 나머지 결과는 그대로 보여 준다.
        pass
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    for source, values in output.items():
        output[source] = list(dict.fromkeys(
            value for value in values
            if 2 <= len(value) <= 50
        ))
    return output


EPHEMERAL_PATTERNS = [
    r"\bvs\b", r"경기\s*(결과|중계|스코어)", r"(축구|야구|농구|배구).*(결과|중계)",
    r"선발\s*라인업", r"실시간\s*스코어", r"당첨\s*번호", r"로또\s*\d*회",
    r"오늘의\s*경기", r"몇\s*대\s*몇", r"득점\s*결과",
]


def is_ephemeral_keyword(keyword: str) -> bool:
    value = normalize_keyword(keyword).lower()
    return any(re.search(pattern, value, re.I) for pattern in EPHEMERAL_PATTERNS)


def fetch_realtime_groups() -> dict[str, list[str]]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    groups: dict[str, list[str]] = {}
    definitions = [
        ("다음", "https://adsensefarm.kr/realtime/daum.php"),
        ("구글", "https://adsensefarm.kr/realtime/googletrend.php"),
        ("크리에이터 어드바이저", "https://adsensefarm.kr/realtime/naver.php"),
    ]
    for label, url in definitions:
        try:
            values = requests.get(url, headers=headers, timeout=15).json().get("data", [])
            groups[label] = [
                normalize_keyword(str(value))
                for value in values
                if normalize_keyword(str(value)) and not is_ephemeral_keyword(str(value))
            ][:10]
        except Exception:
            groups[label] = []
    try:
        values = requests.get(
            "https://api.signal.bz/news/realtime", headers=headers, timeout=15
        ).json().get("top10", [])
        groups["네이버 시그널"] = [
            normalize_keyword(str(item.get("keyword", "")))
            for item in values
            if normalize_keyword(str(item.get("keyword", "")))
            and not is_ephemeral_keyword(str(item.get("keyword", "")))
        ][:10]
    except Exception:
        groups["네이버 시그널"] = []
    return groups


def build_prompt(topic: str, keywords: list[str], base: str, image_slots: bool) -> str:
    slot = "\n각 소제목 다음에 [사진 삽입 위치]를 한 줄로 표시하세요." if image_slots else ""
    return f"""당신은 네이버 SEO에 능숙한 전문 블로거입니다.
주제: {topic}
연관 검색어: {", ".join(keywords)}
사용자 참고 원문:
{base}

첫 줄은 호기심을 끄는 제목으로 작성하세요. 본문은 자연스러운 존댓말 문어체로 쓰고,
검색어를 억지로 반복하지 마세요. 확인할 수 없는 최신 사실은 단정하지 마세요.
소제목, 충분한 문단, 실용적인 설명과 결론을 포함하고 마지막 줄에는 관련 해시태그
10개 이상을 공백으로 구분해 작성하세요. 마크다운 기호와 출처 URL은 출력하지 마세요.{slot}
본문만 출력하세요."""


def generate_openai(api_key: str, model: str, prompt: str) -> str:
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model or "gpt-5-mini", "input": prompt, "max_output_tokens": 8000},
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("output_text"):
        return data["output_text"]
    texts = []
    for item in data.get("output", []):
        texts.extend(p.get("text", "") for p in item.get("content", []) if p.get("text"))
    return "\n".join(texts)


def generate_gemini(api_key: str, model: str, prompt: str) -> str:
    model = model or "gemini-2.5-flash"
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent",
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 8192}},
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    return "\n".join(
        part.get("text", "")
        for candidate in data.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    )


class PictureCleanerApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1180x780")
        self.root.minsize(940, 650)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.settings = load_json(CONFIG_FILE, {})
        self.keyword_db = load_json(DB_FILE, [])
        self.last_outputs: list[Path] = []
        self.preview_ref = None
        self.status = StringVar(value="준비됨")
        self.folder = StringVar(value=self.settings.get("folder", str(default_screenshot_folder())))
        self.today_only = BooleanVar(value=self.settings.get("today_only", True))
        self.recycle = BooleanVar(value=self.settings.get("recycle", False))
        self.seed = StringVar()
        self.selected_realtime = StringVar()
        self.realtime_by_source: dict[str, list[str]] = {}
        self.selected_keyword_label = StringVar(value="선택 검색어: -")
        self.keyword_checks = {}
        self.realtime_groups = {}
        self.provider = StringVar(value=self.settings.get("provider", "OpenAI"))
        # API 키는 디스크에 저장하지 않고 실행 중 메모리에만 둔다.
        self.api_key = StringVar(value="")
        self.model = StringVar(value=self.settings.get("model", "gpt-5-mini"))
        self.topic = StringVar()
        self.image_slots = BooleanVar(value=True)
        self.phone_auto_images = BooleanVar(value=True)
        self.blog_id = StringVar(value=self.settings.get("blog_id", "macdcross"))
        self.comment_days = StringVar(value="10")
        self.comment_interval = StringVar(value=self.settings.get("comment_interval", "5"))
        self.neighbor_interval = StringVar(value=self.settings.get("neighbor_interval", "60"))
        self.neighbor_max = StringVar(value=self.settings.get("neighbor_max", "5"))
        self.naver_bot = NaverAutomation(APP_DIR, self._naver_log)
        self._style()
        self._layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._poll)
        self.root.after(500, self.run_realtime)
        self.root.after(300, self.run_realtime)

    def _style(self):
        style = ttk.Style()
        style.theme_use("vista")
        style.configure("TNotebook.Tab", padding=(20, 10), font=("맑은 고딕", 10, "bold"))
        style.configure("Accent.TButton", font=("맑은 고딕", 10, "bold"), padding=(16, 9))
        style.configure("Title.TLabel", font=("맑은 고딕", 19, "bold"))
        style.configure("Sub.TLabel", font=("맑은 고딕", 10), foreground="#52606d")

    def _layout(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Picture Cleaner PC", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer, text="이미지 정리 · 실시간 연관 검색어 · 네이버 블로그 작성",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(0, 12))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        self.image_tab = ttk.Frame(tabs, padding=16)
        self.keyword_tab = ttk.Frame(tabs, padding=16)
        self.blog_tab = ttk.Frame(tabs, padding=16)
        self.comment_tab = ttk.Frame(tabs, padding=16)
        tabs.add(self.image_tab, text="1  이미지 자동 정리")
        tabs.add(self.keyword_tab, text="2  실시간 연관 검색어")
        tabs.add(self.blog_tab, text="3  네이버 블로그 자동화")
        tabs.add(self.comment_tab, text="4  댓글·이웃 소통")
        self._image_ui()
        self._keyword_ui()
        self._blog_ui()
        self._comment_ui()
        ttk.Separator(outer).pack(fill="x", pady=(12, 7))
        ttk.Label(outer, textvariable=self.status).pack(anchor="w")

    def _image_ui(self):
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

    def _keyword_ui(self):
        top = ttk.Frame(self.keyword_tab)
        top.pack(fill="x")
        ttk.Label(top, text="앱 실행 시 4개 출처에서 총 40개를 자동 수집합니다.", style="Sub.TLabel").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(top, text="실시간 40개 새로고침", command=self.run_realtime).pack(side="right")
        toolbar = ttk.Frame(self.keyword_tab)
        toolbar.pack(fill="x", pady=8)
        ttk.Button(toolbar, text="선택 항목을 블로그 주제로", command=self.use_keyword).pack(side="left")
        ttk.Button(toolbar, text="연관 검색어만 복사", command=self.copy_related_only).pack(
            side="left", padx=8
        )
        split = ttk.Panedwindow(self.keyword_tab, orient="vertical")
        split.pack(fill="both", expand=True)
        realtime_box = ttk.LabelFrame(split, text="일회성 키워드를 제외한 실시간 검색어 · 한 개만 선택", padding=8)
        related_box = ttk.LabelFrame(split, text="선택한 검색어의 연관 검색어", padding=8)
        split.add(realtime_box, weight=3)
        split.add(related_box, weight=2)
        canvas = __import__("tkinter").Canvas(realtime_box, highlightthickness=0)
        scroll = ttk.Scrollbar(realtime_box, orient="vertical", command=canvas.yview)
        self.realtime_list = ttk.Frame(canvas)
        self.realtime_list.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.realtime_list, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.keyword_text = ScrolledText(related_box, wrap="word", font=("맑은 고딕", 11))
        self.keyword_text.pack(fill="both", expand=True)

    # 최신 검색어 UI 정의. 위의 초기 버전 대신 클래스 생성 시 이 메서드가 사용된다.
    def _keyword_ui(self):
        top = ttk.Frame(self.keyword_tab)
        top.pack(fill="x")
        ttk.Entry(top, textvariable=self.seed, font=("맑은 고딕", 12)).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            top, text="연관어 조회", style="Accent.TButton", command=self.run_related
        ).pack(side="left", padx=8)
        ttk.Button(top, text="실시간 40개 새로고침", command=self.run_realtime).pack(
            side="left"
        )
        toolbar = ttk.Frame(self.keyword_tab)
        toolbar.pack(fill="x", pady=8)
        ttk.Button(
            toolbar, text="선택 항목을 블로그 주제로", command=self.use_keyword
        ).pack(side="left")
        ttk.Button(
            toolbar,
            text="연관어 내용만 복사",
            command=lambda: self.copy_widget(self.keyword_text),
        ).pack(side="left", padx=8)
        split = ttk.Panedwindow(self.keyword_tab, orient="vertical")
        split.pack(fill="both", expand=True)
        list_holder, result_holder = ttk.Frame(split), ttk.Frame(split)
        split.add(list_holder, weight=3)
        split.add(result_holder, weight=2)
        self.keyword_canvas = __import__("tkinter").Canvas(
            list_holder, highlightthickness=0
        )
        scroll = ttk.Scrollbar(
            list_holder, orient="vertical", command=self.keyword_canvas.yview
        )
        self.keyword_groups_frame = ttk.Frame(self.keyword_canvas)
        self.keyword_groups_frame.bind(
            "<Configure>",
            lambda _e: self.keyword_canvas.configure(
                scrollregion=self.keyword_canvas.bbox("all")
            ),
        )
        self.keyword_canvas.create_window(
            (0, 0), window=self.keyword_groups_frame, anchor="nw"
        )
        self.keyword_canvas.configure(yscrollcommand=scroll.set)
        self.keyword_canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Label(
            result_holder, textvariable=self.selected_keyword_label
        ).pack(anchor="w", pady=(8, 4))
        self.keyword_text = ScrolledText(
            result_holder, wrap="word", font=("맑은 고딕", 11)
        )
        self.keyword_text.pack(fill="both", expand=True)
        self._render_keyword_groups({})

    def _render_keyword_groups(self, groups):
        for child in self.keyword_groups_frame.winfo_children():
            child.destroy()
        self.keyword_checks = {}
        if not groups:
            ttk.Label(
                self.keyword_groups_frame,
                text="앱 시작과 동시에 4개 출처의 실시간 검색어를 수집합니다.",
                style="Sub.TLabel",
            ).pack(anchor="w", padx=6, pady=10)
            return
        for source in ["다음", "구글", "크리에이터 어드바이저", "네이버 시그널"]:
            card = ttk.LabelFrame(
                self.keyword_groups_frame,
                text=f"{source} 실시간 검색어",
                padding=8,
            )
            card.pack(fill="x", padx=4, pady=5)
            words = groups.get(source, [])
            if not words:
                ttk.Label(card, text="수집 결과 없음", style="Sub.TLabel").pack(
                    anchor="w"
                )
            for rank, keyword in enumerate(words, 1):
                variable = BooleanVar(value=False)
                self.keyword_checks[keyword] = variable
                ttk.Checkbutton(
                    card,
                    text=f"{rank}. {keyword}",
                    variable=variable,
                    command=lambda word=keyword: self._select_realtime_keyword(word),
                ).pack(anchor="w", pady=1)

    def _select_realtime_keyword(self, keyword):
        chosen = self.keyword_checks[keyword].get()
        for word, variable in self.keyword_checks.items():
            if word != keyword:
                variable.set(False)
        if not chosen:
            self.seed.set("")
            self.selected_keyword_label.set("선택 검색어: -")
            self.keyword_text.delete("1.0", "end")
            return
        self.seed.set(keyword)
        self.topic.set(keyword)
        self.selected_keyword_label.set(f"선택 검색어: {keyword}")
        self.run_related()

    def _blog_ui(self):
        form = ttk.Frame(self.blog_tab)
        form.pack(fill="x")
        ttk.Label(form, text="주제").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.topic).grid(row=0, column=1, columnspan=4, sticky="ew", pady=4)
        ttk.Label(form, text="AI").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            form, textvariable=self.provider, values=["OpenAI", "Gemini"], width=12, state="readonly"
        ).grid(row=1, column=1, sticky="w")
        ttk.Label(form, text="API 키").grid(row=1, column=2, padx=(12, 4))
        ttk.Entry(form, textvariable=self.api_key, show="●").grid(row=1, column=3, sticky="ew")
        ttk.Label(form, text="모델").grid(row=1, column=4, padx=(12, 4))
        ttk.Entry(form, textvariable=self.model, width=20).grid(row=1, column=5)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=2)
        ttk.Checkbutton(form, text="사진 삽입 위치 표시", variable=self.image_slots).grid(
            row=2, column=1, sticky="w", pady=4
        )
        panes = ttk.Panedwindow(self.blog_tab, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=8)
        left, right = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=1)
        ttk.Label(left, text="참고 원문 / 지시사항").pack(anchor="w")
        self.base_text = ScrolledText(left, wrap="word", font=("맑은 고딕", 10))
        self.base_text.pack(fill="both", expand=True, padx=(0, 4))
        ttk.Label(right, text="생성 결과").pack(anchor="w")
        self.blog_result = ScrolledText(right, wrap="word", font=("맑은 고딕", 10))
        self.blog_result.pack(fill="both", expand=True, padx=(4, 0))
        actions = ttk.Frame(self.blog_tab)
        actions.pack(fill="x")
        ttk.Button(actions, text="블로그 초안 생성", style="Accent.TButton", command=self.run_generate).pack(
            side="left"
        )
        ttk.Button(actions, text="결과 복사", command=lambda: self.copy_widget(self.blog_result)).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="네이버 글쓰기 열기", command=self.open_naver).pack(side="left")
        ttk.Button(actions, text="ChatGPT 열기", command=lambda: webbrowser.open("https://chatgpt.com/")).pack(
            side="left", padx=8
        )
        ttk.Separator(self.blog_tab).pack(fill="x", pady=10)
        phone = ttk.LabelFrame(self.blog_tab, text="Phone 미래 전망 → 네이버 임시저장", padding=10)
        phone.pack(fill="x")
        ttk.Label(
            phone,
            text="실시간 검색어를 분석해 연관어가 많고 조회 가능성이 높은 주제를 고른 뒤 전체 연관 키워드를 Phone 미래 전망에 입력합니다.",
            style="Sub.TLabel",
        ).pack(anchor="w")
        phone_actions = ttk.Frame(phone)
        phone_actions.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(
            phone_actions, text="오늘 캡처 사진 자동 삽입", variable=self.phone_auto_images
        ).pack(side="left")
        ttk.Button(
            phone_actions, text="ChatGPT 로그인 창", command=self.open_chatgpt_login
        ).pack(side="left", padx=8)
        ttk.Button(
            phone_actions,
            text="Phone 미래 전망 자동화 시작",
            style="Accent.TButton",
            command=self.start_phone_workflow,
        ).pack(side="left")

    def _comment_ui(self):
        form = ttk.LabelFrame(self.comment_tab, text="네이버 댓글 자동화 설정", padding=12)
        form.pack(fill="x")
        labels = [
            ("네이버 블로그 ID", self.blog_id, 18),
            ("최근 글 일수", self.comment_days, 8),
            ("답글 간격(초)", self.comment_interval, 8),
            ("이웃 댓글 간격(초)", self.neighbor_interval, 8),
            ("이웃 최대 글 수", self.neighbor_max, 8),
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
        ttk.Button(actions, text="네이버 로그인 창 열기", command=self.open_naver_login).pack(side="left")
        ttk.Button(actions, text="내 글 답글·하트 시작", style="Accent.TButton", command=self.start_own_comments).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="이웃 새글 댓글 시작", command=self.start_neighbor_comments).pack(side="left")
        ttk.Button(actions, text="작업 중지", command=self.naver_bot.stop).pack(side="left", padx=8)
        self.comment_log = ScrolledText(self.comment_tab, wrap="word", font=("맑은 고딕", 10), state="disabled")
        self.comment_log.pack(fill="both", expand=True)

    def _naver_log(self, message):
        self.events.put(("naver_log", message))

    def open_naver_login(self):
        threading.Thread(
            target=lambda: self.naver_bot.open_login(self.blog_id.get().strip() or "macdcross"),
            daemon=True,
        ).start()

    def open_chatgpt_login(self):
        threading.Thread(target=self.naver_bot.open_chatgpt_login, daemon=True).start()

    def start_phone_workflow(self):
        if not messagebox.askyesno(
            APP_NAME,
            "실시간 검색어 분석부터 ChatGPT 글 생성, 네이버 사진 첨부 및 임시저장까지 실행할까요?\n"
            "발행 버튼은 누르지 않습니다.",
        ):
            return
        config = {
            "topic": self.topic.get(),
            "base": self.base_text.get("1.0", "end").strip(),
            "folder": self.folder.get(),
            "images": self.phone_auto_images.get(),
            "blog_id": self.blog_id.get().strip() or "macdcross",
        }
        threading.Thread(target=self._phone_workflow, args=(config,), daemon=True).start()

    def _phone_workflow(self, config):
        try:
            self._naver_log("실시간 검색어를 수집하고 있습니다.")
            realtime_by_source = fetch_realtime()
            realtime = [
                keyword
                for values in realtime_by_source.values()
                for keyword in values
                if not is_ephemeral_keyword(keyword)
            ]
            manual_topic = normalize_keyword(config["topic"])
            seeds = ([manual_topic] if manual_topic else []) + realtime[:12]
            seeds = list(dict.fromkeys(seed for seed in seeds if len(seed) >= 2))
            if not seeds:
                raise RuntimeError("실시간 검색어를 가져오지 못했습니다. 블로그 주제를 직접 입력하세요.")
            scored = []
            for seed in seeds:
                related_map = fetch_autocomplete(seed)
                related = list(
                    dict.fromkeys(
                        word
                        for words in related_map.values()
                        for word in words
                        if normalize_keyword(word)
                    )
                )
                # 여러 검색엔진에서 연관어가 많이 발견될수록 지속 조회 가능성이 높다고 본다.
                scored.append((len(related), seed, related))
            scored.sort(key=lambda item: item[0], reverse=True)
            _, selected, related = scored[0]
            self.events.put(("phone_topic", selected))
            all_keywords = list(dict.fromkeys([selected] + related + realtime[:20]))
            self._naver_log(
                f"선정 주제: {selected} · 전체 연관/실시간 키워드 {len(all_keywords)}개"
            )
            prompt = build_prompt(
                selected,
                all_keywords,
                "실시간 검색어 중 조회수가 많이 나올 가능성이 있고, 연관 검색어가 풍부하며 "
                "사용자가 실제로 궁금해할 질문을 빠짐없이 반영해 주세요. "
                + config["base"],
                True,
            )
            generated = self.naver_bot.generate_phone_future(prompt)
            self.events.put(("blog", generated))
            pictures = []
            if config["images"]:
                source_folder = Path(config["folder"])
                cleaned = source_folder / "PictureCleaner"
                candidates = image_candidates(cleaned, True) if cleaned.exists() else []
                if not candidates:
                    candidates = image_candidates(source_folder, True)
                pictures = [str(path) for path in candidates[:10]]
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
        threading.Thread(
            target=self.naver_bot.run_own_posts,
            args=(self.blog_id.get().strip(), days, interval, True),
            daemon=True,
        ).start()

    def start_neighbor_comments(self):
        try:
            interval = self._positive_int(self.neighbor_interval.get(), "이웃 댓글 간격", 10)
            maximum = self._positive_int(self.neighbor_max.get(), "이웃 최대 글 수", 1)
        except ValueError as exc:
            messagebox.showinfo(APP_NAME, str(exc))
            return
        if maximum > 30:
            messagebox.showinfo(APP_NAME, "한 번에 처리할 이웃 새글은 최대 30개입니다.")
            return
        self.status.set(f"이웃 새글 {maximum}개, {interval}초 간격 작업을 바로 시작합니다.")
        threading.Thread(
            target=self.naver_bot.run_neighbor_posts,
            args=(self.blog_id.get().strip(), interval, maximum),
            daemon=True,
        ).start()

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

    def run_related(self):
        seed = normalize_keyword(self.selected_realtime.get() or self.seed.get())
        if len(seed) < 2:
            messagebox.showinfo(APP_NAME, "두 글자 이상의 검색어를 입력하세요.")
            return

        def work():
            self.events.put(("status", f"'{seed}' 연관 검색어 조회 중..."))
            result = fetch_autocomplete(seed)
            self.events.put(("keywords", seed, result))

        threading.Thread(target=work, daemon=True).start()

    def run_related(self):
        seed = normalize_keyword(self.seed.get())
        if len(seed) < 2:
            messagebox.showinfo(APP_NAME, "두 글자 이상의 검색어를 입력하세요.")
            return

        def work():
            self.events.put(("status", f"'{seed}' 연관 검색어 조회 중..."))
            self.events.put(("related_plain", seed, fetch_autocomplete(seed)))

        threading.Thread(target=work, daemon=True).start()

    def run_realtime(self):
        def work():
            self.events.put(("status", "실시간 검색어 수집 중..."))
            result = fetch_realtime()
            self.events.put(("realtime", result))

        threading.Thread(target=work, daemon=True).start()

    def use_keyword(self):
        selected = normalize_keyword(self.selected_realtime.get() or self.seed.get())
        if selected:
            self.topic.set(selected)
            self.status.set(f"블로그 주제로 설정: {selected}")

    def run_realtime(self):
        def work():
            self.events.put(("status", "4개 출처에서 실시간 검색어 40개를 수집 중..."))
            self.events.put(("realtime_groups", fetch_realtime_groups()))

        threading.Thread(target=work, daemon=True).start()

    def use_keyword(self):
        selected = self.seed.get().strip()
        if selected:
            self.topic.set(selected)
            self.status.set(f"블로그 주제로 설정: {selected}")

    def run_generate(self):
        topic = normalize_keyword(self.topic.get())
        key = self.api_key.get().strip()
        if not topic or not key:
            messagebox.showinfo(APP_NAME, "주제와 API 키를 입력하세요.")
            return
        keywords = [line.strip() for line in self.keyword_text.get("1.0", "end").splitlines() if line.strip()][:30]
        prompt = build_prompt(topic, keywords, self.base_text.get("1.0", "end").strip(), self.image_slots.get())
        provider, model = self.provider.get(), self.model.get().strip()

        def work():
            self.events.put(("status", f"{provider}에서 초안을 생성 중..."))
            try:
                text = generate_openai(key, model, prompt) if provider == "OpenAI" else generate_gemini(key, model, prompt)
                self.events.put(("blog", text.strip()))
            except Exception as exc:
                self.events.put(("error", f"초안 생성 실패\n{exc}"))

        threading.Thread(target=work, daemon=True).start()

    def copy_widget(self, widget):
        text = widget.get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set("클립보드에 복사했습니다.")

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
        for column, source in enumerate(("다음", "구글", "크리에이터 어드바이저", "네이버 시그널")):
            section = ttk.LabelFrame(self.realtime_list, text=source, padding=8)
            section.grid(row=0, column=column, sticky="nsew", padx=5, pady=3)
            values = [word for word in result.get(source, []) if not is_ephemeral_keyword(word)]
            usable_total += len(values)
            if not values:
                ttk.Label(section, text="수집 결과 없음", style="Sub.TLabel").pack(anchor="w")
            for rank, keyword in enumerate(values, 1):
                ttk.Radiobutton(
                    section,
                    text=f"{rank}. {keyword}",
                    value=keyword,
                    variable=self.selected_realtime,
                    command=lambda word=keyword: self.select_realtime_keyword(word),
                ).pack(anchor="w", fill="x", pady=2)
            self.realtime_list.columnconfigure(column, weight=1)
        self.status.set(
            f"실시간 검색어 {crawled_total}개 수집 · 일회성 제외 {usable_total}개 · 한 개를 선택하세요."
        )

    def open_naver(self):
        text = self.blog_result.get("1.0", "end").strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status.set("초안을 복사하고 네이버 글쓰기를 열었습니다. 편집기에 붙여넣으세요.")
        webbrowser.open("https://blog.naver.com/GoBlogWrite.naver")

    def _poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
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
                    lines = []
                    merged = []
                    for source, words in result.items():
                        lines.append(f"[{source}]")
                        lines.extend(words or ["조회 결과 없음"])
                        lines.append("")
                        merged.extend(words)
                    self.keyword_text.delete("1.0", "end")
                    self.keyword_text.insert("1.0", "\n".join(lines))
                    self.seed.set(seed)
                    self.keyword_db = list(dict.fromkeys(self.keyword_db + merged))[-500:]
                    save_json(DB_FILE, self.keyword_db)
                    self.status.set(f"연관 검색어 {len(set(merged))}개 조회 완료")
                elif kind == "realtime":
                    result = event[1]
                    self.show_realtime_sources(result)
                elif kind == "related_plain":
                    _, seed, result = event
                    merged = list(
                        dict.fromkeys(
                            word for words in result.values() for word in words
                        )
                    )
                    self.keyword_text.delete("1.0", "end")
                    self.keyword_text.insert(
                        "1.0", "\n".join(merged) or "연관 검색어가 없습니다."
                    )
                    self.selected_keyword_label.set(f"선택 검색어: {seed}")
                    self.keyword_db = list(
                        dict.fromkeys(self.keyword_db + merged)
                    )[-500:]
                    save_json(DB_FILE, self.keyword_db)
                    self.status.set(f"연관 검색어 {len(merged)}개 조회 완료")
                elif kind == "realtime_groups":
                    groups = event[1]
                    self.realtime_groups = groups
                    self._render_keyword_groups(groups)
                    total = sum(len(words) for words in groups.values())
                    self.status.set(
                        f"일회성 키워드 제외 · 실시간 검색어 {total}개 수집 완료"
                    )
                elif kind == "blog":
                    self.blog_result.delete("1.0", "end")
                    self.blog_result.insert("1.0", event[1])
                    self.status.set("블로그 초안 생성 완료")
                elif kind == "naver_log":
                    self.comment_log.configure(state="normal")
                    self.comment_log.insert("end", f"[{datetime.now():%H:%M:%S}] {event[1]}\n")
                    self.comment_log.see("end")
                    self.comment_log.configure(state="disabled")
                    self.status.set(event[1])
                elif kind == "phone_topic":
                    self.topic.set(event[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def close(self):
        save_json(
            CONFIG_FILE,
            {
                "folder": self.folder.get(),
                "today_only": self.today_only.get(),
                "recycle": self.recycle.get(),
                "provider": self.provider.get(),
                "model": self.model.get(),
                "blog_id": self.blog_id.get(),
                "comment_interval": self.comment_interval.get(),
                "neighbor_interval": self.neighbor_interval.get(),
                "neighbor_max": self.neighbor_max.get(),
            },
        )
        self.naver_bot.stop()
        self.naver_bot.close()
        self.root.destroy()


def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    root = Tk()
    PictureCleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
