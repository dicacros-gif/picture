from __future__ import annotations

import json
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait


THANKS = [
    "방문해 주시고 따뜻한 댓글 남겨주셔서 감사합니다 😊 오늘도 좋은 하루 보내세요!",
    "정성스러운 댓글 정말 감사합니다. 덕분에 큰 힘이 됩니다!",
    "관심 있게 읽어주시고 소중한 말씀 남겨주셔서 감사해요 😊",
    "좋은 댓글 남겨주셔서 감사합니다. 행복한 일 가득한 하루 되세요!",
    "귀한 시간 내어 읽어주시고 댓글까지 남겨주셔서 정말 감사합니다.",
    "따뜻한 소통 감사합니다 😊 앞으로도 유익한 이야기로 자주 찾아뵐게요!",
    "공감해 주셔서 감사합니다. 남겨주신 댓글 덕분에 힘이 나네요!",
    "소중한 댓글 감사드립니다. 오늘도 건강하고 기분 좋은 하루 보내세요!",
    "좋은 말씀 감사합니다 😊 다음 글도 알차게 준비해 보겠습니다!",
    "함께 이야기 나눠주셔서 감사합니다. 늘 행복한 일만 가득하세요!",
    "꼼꼼하게 읽어주신 마음이 느껴져 정말 감사합니다 😊",
    "반가운 댓글 남겨주셔서 감사해요. 앞으로도 자주 소통해요!",
]

NEIGHBOR_COMMENTS = [
    "정성스럽게 정리해 주신 글 잘 읽었습니다 😊 유익한 내용 감사합니다!",
    "관심 있던 내용인데 덕분에 이해하기 쉬웠어요. 좋은 글 감사합니다!",
    "알찬 정보 잘 보고 갑니다. 오늘도 행복한 하루 보내세요 😊",
    "공감하며 재미있게 읽었습니다. 다음 글도 기대할게요!",
    "좋은 내용 공유해 주셔서 감사합니다. 덕분에 많이 배웠어요 😊",
    "꼼꼼한 설명 덕분에 유익하게 읽었습니다. 즐거운 하루 되세요!",
    "흥미로운 이야기 잘 읽고 갑니다. 정성스러운 포스팅 감사합니다!",
    "읽을수록 도움이 되는 글이네요. 좋은 정보 감사드립니다 😊",
    "덕분에 새로운 내용을 알게 됐어요. 편안한 하루 보내세요!",
    "유익한 글 잘 봤습니다. 앞으로도 좋은 소식 자주 나눠주세요 😊",
]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"replied": [], "liked": [], "neighbor_commented": []}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def recent_post_urls(blog_id: str, days: int = 10) -> list[str]:
    response = requests.get(
        f"https://rss.blog.naver.com/{blog_id}.xml",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    # "10일치"는 현재 시각 기준 240시간이 아니라 오늘을 포함한 달력 날짜 10일이다.
    korea = timezone(timedelta(hours=9))
    now_korea = datetime.now(korea)
    cutoff = (now_korea - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    result = []
    for item in root.findall(".//item"):
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        try:
            date = parsedate_to_datetime(published)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        match = re.search(r"(?:logNo=|/)(\d{10,})", link)
        if date >= cutoff and match:
            result.append(
                f"https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={match.group(1)}"
            )
    return list(dict.fromkeys(result))


class NaverAutomation:
    def __init__(self, data_dir: Path, log: Callable[[str], None]):
        self.data_dir = data_dir
        self.log = log
        self.stop_event = threading.Event()
        self.driver = None
        self.state_file = data_dir / "naver_comment_history.json"
        self.state = _load(self.state_file)

    def stop(self):
        self.stop_event.set()
        self.log("중지 요청을 받았습니다. 현재 작업 후 안전하게 멈춥니다.")

    def _driver(self):
        if self.driver:
            return self.driver
        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={self.data_dir / 'naver-chrome-profile'}")
        options.add_argument("--disable-notifications")
        options.add_argument("--start-maximized")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.driver = webdriver.Chrome(options=options)
        return self.driver

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def open_login(self, blog_id: str):
        driver = self._driver()
        driver.get(f"https://blog.naver.com/{blog_id}")
        self.log("Chrome 창을 열었습니다. 로그인되어 있지 않다면 네이버에 로그인해 주세요.")

    def open_chatgpt_login(self):
        driver = self._driver()
        driver.get("https://chatgpt.com/")
        self.log("ChatGPT를 열었습니다. 로그인 후 왼쪽에서 'Phone 미래 전망'이 보이는지 확인하세요.")

    def generate_phone_future(self, prompt: str, timeout_seconds: int = 420) -> str:
        driver = self._driver()
        driver.get("https://chatgpt.com/")
        wait = WebDriverWait(driver, 40)
        links = wait.until(
            lambda d: d.find_elements(
                By.XPATH,
                "//*[self::a or self::button][contains(normalize-space(.), 'Phone 미래 전망')]",
            )
        )
        driver.execute_script("arguments[0].click()", links[0])
        self.log("ChatGPT의 Phone 미래 전망을 열었습니다.")
        editor = wait.until(
            lambda d: next(
                (
                    e for e in d.find_elements(
                        By.CSS_SELECTOR, "#prompt-textarea, div[contenteditable='true']"
                    )
                    if e.is_displayed()
                ),
                None,
            )
        )
        editor.click()
        editor.send_keys(prompt)
        editor.send_keys(Keys.ENTER)
        self.log("실시간 검색어와 전체 연관 키워드를 입력했습니다. 글 생성을 기다립니다.")

        started = time.time()
        last_text = ""
        stable_since = time.time()
        while time.time() - started < timeout_seconds:
            if self.stop_event.is_set():
                raise RuntimeError("사용자가 작업을 중지했습니다.")
            responses = driver.find_elements(
                By.CSS_SELECTOR,
                "[data-message-author-role='assistant'] .markdown, "
                "[data-message-author-role='assistant']",
            )
            text = responses[-1].text.strip() if responses else ""
            if text and text != last_text:
                last_text = text
                stable_since = time.time()
            stop_buttons = driver.find_elements(
                By.CSS_SELECTOR, "button[data-testid='stop-button'], button[aria-label*='중지']"
            )
            if text and not any(b.is_displayed() for b in stop_buttons) and time.time() - stable_since >= 5:
                self.log("Phone 미래 전망의 블로그 글 생성을 완료했습니다.")
                return text
            time.sleep(2)
        raise TimeoutError("ChatGPT 응답 대기 시간이 초과되었습니다.")

    @staticmethod
    def _split_title_body(text: str) -> tuple[str, str]:
        lines = [line.strip() for line in text.replace("**", "").splitlines()]
        title = next((line.lstrip("# ").strip() for line in lines if line.strip()), "블로그 글")
        body_lines = list(lines)
        for index, line in enumerate(body_lines):
            if line.strip():
                body_lines.pop(index)
                break
        return title[:100], "\n".join(body_lines).strip()

    def save_naver_draft(
        self, blog_id: str, generated_text: str, image_paths: list[str]
    ) -> None:
        driver = self._driver()
        title, body = self._split_title_body(generated_text)
        driver.get(f"https://blog.naver.com/{blog_id}/postwrite")
        wait = WebDriverWait(driver, 45)
        title_boxes = wait.until(
            lambda d: [
                e for e in d.find_elements(
                    By.CSS_SELECTOR,
                    ".se-documentTitle [contenteditable='true'], "
                    ".se-title-text [contenteditable='true']",
                )
                if e.is_displayed()
            ]
        )
        title_boxes[0].click()
        title_boxes[0].send_keys(title)
        body_boxes = [
            e for e in driver.find_elements(
                By.CSS_SELECTOR,
                ".se-component-content [contenteditable='true'], "
                ".se-text-paragraph[contenteditable='true']",
            )
            if e.is_displayed()
        ]
        if not body_boxes:
            raise RuntimeError("네이버 본문 입력 영역을 찾지 못했습니다.")
        body_boxes[-1].click()
        body_boxes[-1].send_keys(body)
        self.log("생성된 제목과 본문을 네이버 글쓰기에 입력했습니다.")

        existing = [str(Path(p)) for p in image_paths if Path(p).is_file()]
        if existing:
            file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            if not file_inputs:
                photo_buttons = driver.find_elements(
                    By.XPATH,
                    "//*[self::button or self::a][contains(@aria-label,'사진') "
                    "or contains(normalize-space(.),'사진')]",
                )
                if photo_buttons:
                    driver.execute_script("arguments[0].click()", photo_buttons[0])
                    time.sleep(1)
                    file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            if file_inputs:
                file_inputs[-1].send_keys("\n".join(existing))
                self.log(f"오늘 캡처 사진 {len(existing)}개를 첨부했습니다.")
                time.sleep(min(20, 3 + len(existing) * 2))
            else:
                self.log("사진 업로드 입력을 찾지 못해 본문만 임시저장합니다.")

        # '발행'은 절대 선택하지 않는다. 임시저장/저장 버튼만 정확히 찾는다.
        draft_buttons = driver.find_elements(
            By.XPATH,
            "//button[normalize-space()='임시저장' or normalize-space()='저장']"
            "|//a[normalize-space()='임시저장' or normalize-space()='저장']",
        )
        visible = [button for button in draft_buttons if button.is_displayed()]
        if not visible:
            raise RuntimeError("네이버 임시저장 버튼을 찾지 못했습니다. 발행하지 않고 화면에 그대로 둡니다.")
        driver.execute_script("arguments[0].click()", visible[0])
        self.log("네이버 임시저장까지 완료했습니다. 발행 버튼은 누르지 않았습니다.")

    @staticmethod
    def _open_comments(driver) -> bool:
        candidates = driver.find_elements(By.CSS_SELECTOR, "a.btn_comment._cmtList, a._floating_bottom_btn_comment")
        for element in candidates:
            try:
                if element.is_displayed():
                    driver.execute_script("arguments[0].click()", element)
                    WebDriverWait(driver, 10).until(
                        lambda d: d.find_elements(By.CSS_SELECTOR, "ul.u_cbox_list")
                    )
                    return True
            except Exception:
                continue
        return bool(driver.find_elements(By.CSS_SELECTOR, "ul.u_cbox_list"))

    @staticmethod
    def _own_reply_exists(comment, blog_id: str) -> bool:
        replies = comment.find_elements(By.CSS_SELECTOR, ".u_cbox_reply_area li.u_cbox_comment")
        for reply in replies:
            names = reply.find_elements(By.CSS_SELECTOR, ".u_cbox_nick")
            profile = reply.find_elements(By.CSS_SELECTOR, "a.u_cbox_name")
            name = names[0].text.strip() if names else ""
            href = profile[0].get_attribute("href") if profile else ""
            if blog_id.lower() in (href or "").lower() or name in {"초심", "AIT경제", "비즈니스"}:
                return True
        return False

    def _like_comment(self, comment, key: str) -> bool:
        buttons = comment.find_elements(By.CSS_SELECTOR, ".u_cbox_btn_recomm")
        if not buttons:
            return False
        button = buttons[0]
        classes = button.get_attribute("class") or ""
        pressed = button.get_attribute("aria-pressed") or ""
        if "u_cbox_btn_on" in classes or pressed.lower() == "true":
            return False
        self.driver.execute_script("arguments[0].click()", button)
        self.state.setdefault("liked", []).append(key)
        return True

    def _reply(self, comment, phrase: str) -> bool:
        buttons = comment.find_elements(By.CSS_SELECTOR, ".u_cbox_btn_reply")
        if not buttons:
            return False
        self.driver.execute_script("arguments[0].click()", buttons[0])
        time.sleep(0.5)
        inputs = comment.find_elements(By.CSS_SELECTOR, "textarea.u_cbox_text, .u_cbox_write_area textarea")
        uploads = comment.find_elements(By.CSS_SELECTOR, ".u_cbox_btn_upload")
        if not inputs or not uploads:
            return False
        inputs[-1].clear()
        inputs[-1].send_keys(phrase)
        self.driver.execute_script("arguments[0].click()", uploads[-1])
        return True

    def run_own_posts(self, blog_id: str, days: int, interval: int, do_like: bool = True):
        self.stop_event.clear()
        driver = self._driver()
        urls = recent_post_urls(blog_id, days)
        self.log(f"최근 {days}일 글 {len(urls)}개를 확인합니다.")
        replied = skipped = liked = 0
        for post_index, url in enumerate(urls, 1):
            if self.stop_event.is_set():
                break
            driver.get(url)
            self.log(f"[{post_index}/{len(urls)}] 댓글 확인 중: {driver.title[:45]}")
            if not self._open_comments(driver):
                continue
            comments = driver.find_elements(By.CSS_SELECTOR, "ul.u_cbox_list > li.u_cbox_comment")
            for comment in comments:
                if self.stop_event.is_set():
                    break
                match = re.search(r"comment_(\d+)", comment.get_attribute("class") or "")
                comment_id = match.group(1) if match else ""
                key = f"{url}|{comment_id}"
                if do_like:
                    try:
                        if self._like_comment(comment, key):
                            liked += 1
                    except Exception:
                        pass
                if key in self.state.get("replied", []) or self._own_reply_exists(comment, blog_id):
                    skipped += 1
                    continue
                phrase = random.choice(THANKS)
                try:
                    if self._reply(comment, phrase):
                        replied += 1
                        self.state.setdefault("replied", []).append(key)
                        _save(self.state_file, self.state)
                        self.log(f"  답글 완료: {phrase}")
                        if interval > 0 and self.stop_event.wait(interval):
                            break
                except Exception as exc:
                    self.log(f"  답글 실패: {str(exc)[:100]}")
            _save(self.state_file, self.state)
        self.log(f"내 글 작업 완료 · 답글 {replied} · 기존 답글 스킵 {skipped} · 하트 {liked}")

    @staticmethod
    def _neighbor_urls(driver, own_blog_id: str, maximum: int) -> list[str]:
        driver.get("https://section.blog.naver.com/BlogHome.naver")
        time.sleep(2)
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='PostView.naver'][href*='logNo=']")
        output = []
        for link in links:
            href = link.get_attribute("href") or ""
            if own_blog_id.lower() in href.lower() or href in output:
                continue
            output.append(href)
            if len(output) >= maximum:
                break
        return output

    def run_neighbor_posts(self, blog_id: str, interval: int, maximum: int):
        self.stop_event.clear()
        driver = self._driver()
        urls = self._neighbor_urls(driver, blog_id, maximum * 3)
        done = skipped = 0
        for url in urls:
            if done >= maximum or self.stop_event.is_set():
                break
            log_no = (re.search(r"logNo=(\d+)", url) or [None, url])[1]
            if log_no in self.state.get("neighbor_commented", []):
                skipped += 1
                continue
            driver.get(url)
            self.log(f"이웃 새글 확인: {driver.title[:55]}")
            if not self._open_comments(driver):
                continue
            inputs = driver.find_elements(By.CSS_SELECTOR, ".u_cbox_write_area textarea")
            uploads = driver.find_elements(By.CSS_SELECTOR, ".u_cbox_write_area .u_cbox_btn_upload")
            if not inputs or not uploads:
                continue
            phrase = random.choice(NEIGHBOR_COMMENTS)
            try:
                inputs[0].clear()
                inputs[0].send_keys(phrase)
                driver.execute_script("arguments[0].click()", uploads[0])
                done += 1
                self.state.setdefault("neighbor_commented", []).append(log_no)
                _save(self.state_file, self.state)
                self.log(f"  이웃 댓글 완료: {phrase}")
                if interval > 0 and self.stop_event.wait(interval):
                    break
            except WebDriverException as exc:
                self.log(f"  작성 실패: {str(exc)[:100]}")
        self.log(f"이웃 새글 작업 완료 · 작성 {done} · 중복 스킵 {skipped}")
