from __future__ import annotations

import ctypes
from contextlib import contextmanager
import io
import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from blog_visual_style import (IMAGE_POLICY, BODY_TEXT_COLOR, line_style_runs, cover_headline,
                               quote_parts, choose_visual_style, supplement_bold_phrases)

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
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


def webdriver_bmp_text(value: str, fallback: str = "좋은 글 잘 읽었습니다.") -> str:
    """Return text that ChromeDriver send_keys can safely type.

    ChromeDriver only accepts characters in the Unicode Basic Multilingual
    Plane. Emoji and a few symbols are outside that range and otherwise make
    the whole comment fail with "only supports characters in the BMP".
    """
    text = "".join(
        character
        for character in str(value or "")
        if ord(character) <= 0xFFFF
        and not 0xD800 <= ord(character) <= 0xDFFF
    )
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text).strip()
    return text or fallback


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"replied": [], "liked": [], "neighbor_commented": []}


def _save(path: Path, state: dict | list) -> None:
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
    # macOS adapters override the platform's edit shortcut without copying the
    # publication engine. Windows retains its existing Control key behavior.
    EDITOR_MODIFIER = Keys.CONTROL
    CREATOR_ADVISOR_TRENDS_URL = (
        "https://creator-advisor.naver.com/naver_blog/macdcross/trends"
    )

    def __init__(self, data_dir: Path, log: Callable[[str], None]):
        self.data_dir = data_dir
        self.log = log
        self.stop_event = threading.Event()
        self.driver = None
        self.whale_process = None
        self.attached_existing_whale = False
        self.state_file = data_dir / "naver_comment_history.json"
        self.image_source_history_file = (
            data_dir / "internal_image_source_history.json"
        )
        self.state = _load(self.state_file)

    def stop(self):
        self.stop_event.set()
        self.log("중지 요청을 받았습니다. 현재 작업 후 안전하게 멈춥니다.")

    def reset_stop(self):
        self.stop_event.clear()

    @staticmethod
    def _extract_creator_advisor_keywords(
        text_blocks: list[str],
        category: str = "비즈니스·경제",
        limit: int = 20,
    ) -> list[str]:
        ignored = {
            "검색 유입 트렌드",
            "메인 유입 트렌드",
            "주제별 비교",
            "주제별 트렌드",
            "주제별 인기유입검색어",
            "성별,연령별 인기유입검색어",
            "유입순 보기",
            "설정순 보기",
            "new",
            category,
            category.replace("·", " "),
        }
        best: list[str] = []
        for block in text_blocks:
            lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in str(block or "").splitlines()
                if re.sub(r"\s+", " ", line).strip()
            ]
            start = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if line.replace(" ", "").replace("·", "")
                    == category.replace(" ", "").replace("·", "")
                ),
                None,
            )
            if start is None:
                continue
            values = []
            for line in lines[start + 1 :]:
                value = re.sub(
                    r"\s*(?:new|[▲△▼▽]\s*\d+|-)\s*$",
                    "",
                    line,
                    flags=re.I,
                ).strip()
                if (
                    not value
                    or value in ignored
                    or re.fullmatch(r"(?:new|[▲△▼▽]?\s*\d+|-)", value, re.I)
                    or len(value) > 80
                ):
                    continue
                if value not in values:
                    values.append(value)
                if len(values) >= limit:
                    break
            if len(values) > len(best):
                best = values
            if len(best) >= limit:
                break
        return best[:limit]

    @staticmethod
    def _extract_daum_realtime_keywords(
        text_block: str,
        limit: int = 10,
    ) -> list[str]:
        """Extract the ranked keywords shown in Daum's top-right trend card."""
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in str(text_block or "").splitlines()
            if re.sub(r"\s+", " ", line).strip()
        ]
        start = next(
            (
                index
                for index, line in enumerate(lines)
                if "실시간 트렌드" in line
            ),
            -1,
        )
        if start < 0:
            return []
        values: list[str] = []
        index = start + 1
        while index < len(lines) and len(values) < limit:
            line = lines[index]
            inline = re.match(
                r"^(?:순위\s*)?(\d{1,2})[.)]?\s+(.+?)"
                r"(?:\s+(?:N|NEW|[▲△▼▽]\s*\d*))?$",
                line,
                re.I,
            )
            value = ""
            if inline:
                rank = int(inline.group(1))
                if 1 <= rank <= limit:
                    value = inline.group(2).strip()
            elif re.fullmatch(r"\d{1,2}[.)]?", line):
                rank = int(re.sub(r"\D", "", line))
                if 1 <= rank <= limit and index + 1 < len(lines):
                    index += 1
                    while (
                        index < len(lines)
                        and re.fullmatch(
                            r"(?:위,?|동일|신규|상승|하락|유지|"
                            r"[▲△▼▽]\s*\d*|[-–—])",
                            lines[index],
                            re.I,
                        )
                    ):
                        index += 1
                    if index >= len(lines):
                        break
                    value = lines[index]
            value = re.sub(
                r"\s+(?:N|NEW|[▲△▼▽]\s*\d*)$",
                "",
                value,
                flags=re.I,
            ).strip()
            if (
                value
                and "기준" not in value
                and value not in {"위", "위,", "동일", "신규", "상승", "하락", "유지"}
                and value not in values
                and len(value) <= 80
            ):
                values.append(value)
            index += 1
        return values[:limit]

    def fetch_daum_realtime_trends(self, limit: int = 10) -> list[str]:
        """Read Daum's own top-right '실시간 트렌드' card in ordinary Whale."""
        driver = self._driver()
        driver.get("https://www.daum.net/")
        WebDriverWait(driver, 20).until(
            lambda current: current.execute_script(
                "return document.readyState"
            )
            == "complete"
        )

        def expand_trend_card(current):
            return current.execute_script(
                r"""
                const visible = node => {
                  if (!node) return false;
                  const rect = node.getBoundingClientRect();
                  const style = getComputedStyle(node);
                  return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
                };
                const nodes = Array.from(document.querySelectorAll(
                  'h1,h2,h3,strong,em,span,div'
                )).filter(visible);
                const heading = nodes.find(node =>
                  ['실시간 트렌드', '실시간'].includes(
                    (node.innerText || '').trim()
                  )
                );
                if (!heading) return false;
                let card = heading;
                for (let depth = 0; card && depth < 8; depth += 1) {
                  const text = (card.innerText || '').trim();
                  const rankLines = text.split('\n').filter(line =>
                    /^\s*(?:[1-9]|10)(?:[.)]|\s|$)/.test(line)
                  );
                  if (rankLines.length >= 10) return true;
                  const controls = Array.from(card.querySelectorAll(
                    'button,[role="button"],a'
                  )).filter(visible);
                  const labelled = controls.find(control => {
                    const label = [
                      control.getAttribute('aria-label') || '',
                      control.getAttribute('title') || '',
                      control.innerText || ''
                    ].join(' ');
                    return control.getAttribute('aria-expanded') === 'false' ||
                      /펼치|열기|더보기|실시간/.test(label);
                  });
                  if (labelled) {
                    labelled.click();
                    return true;
                  }
                  const headingRect = heading.getBoundingClientRect();
                  const arrow = controls.find(control => {
                    const rect = control.getBoundingClientRect();
                    return rect.left > headingRect.right &&
                      Math.abs(
                        (rect.top + rect.bottom) / 2 -
                        (headingRect.top + headingRect.bottom) / 2
                      ) < 80;
                  });
                  if (arrow) {
                    arrow.click();
                    return true;
                  }
                  card = card.parentElement;
                }
                return false;
                """
            )

        WebDriverWait(driver, 15).until(expand_trend_card)

        def visible_trend_rows(current):
            for trend_list in current.find_elements(
                By.CSS_SELECTOR,
                ".list_trendrank",
            ):
                rows = [
                    row
                    for row in trend_list.find_elements(
                        By.CSS_SELECTOR,
                        ".link_trendrank",
                    )
                    if row.is_displayed()
                ]
                if len(rows) >= limit:
                    return rows[:limit]
            return False

        try:
            rows = WebDriverWait(driver, 15).until(visible_trend_rows)
            keywords = []
            for row in rows:
                keyword = (
                    row.get_attribute("data-tiara-copy")
                    or next(
                        (
                            item.text.strip()
                            for item in row.find_elements(
                                By.CSS_SELECTOR,
                                ".tit_item",
                            )
                            if item.text.strip()
                        ),
                        "",
                    )
                ).strip()
                if keyword and keyword not in keywords:
                    keywords.append(keyword)
            if len(keywords) >= limit:
                self.log(
                    f"다음 실시간 트렌드 {len(keywords[:limit])}개를 가져왔습니다."
                )
                return keywords[:limit]
        except TimeoutException:
            # 접근성 텍스트 기반 파서는 DOM 클래스가 바뀐 경우를 위한 보조 경로다.
            pass

        def trend_card_text(current):
            return current.execute_script(
                r"""
                const nodes = Array.from(document.querySelectorAll(
                  'h1,h2,h3,strong,em,span,div'
                ));
                const heading = nodes.find(node => {
                  const text = (node.innerText || '').trim();
                  return text === '실시간 트렌드' || text === '실시간';
                });
                if (!heading) return '';
                let node = heading;
                while (node && node !== document.body) {
                  const text = (node.innerText || '').trim();
                  const rankLines = text.split('\n').filter(line =>
                    /^\s*(?:[1-9]|10)(?:[.)]|\s|$)/.test(line)
                  );
                  if (rankLines.length >= 10 && text.length < 2000) return text;
                  node = node.parentElement;
                }
                return '';
                """
            )

        block = WebDriverWait(driver, 20).until(trend_card_text)
        keywords = self._extract_daum_realtime_keywords(str(block), limit)
        if not keywords:
            raise RuntimeError("다음 첫 화면 우측 실시간 트렌드를 찾지 못했습니다.")
        self.log(f"다음 실시간 트렌드 {len(keywords)}개를 가져왔습니다.")
        return keywords

    def fetch_google_trending_now(self, limit: int = 100) -> list[str]:
        """Collect Google Trending Now rows, following 25-row result pages."""
        requested = max(1, min(int(limit), 100))
        driver = self._driver()
        driver.get("https://trends.google.com/trending?geo=KR&hl=ko")

        def visible_rows(current):
            rows = [
                row
                for row in current.find_elements(
                    By.CSS_SELECTOR,
                    "table tbody tr[data-row-id], table tbody tr",
                )
                if row.is_displayed() and row.text.strip()
            ]
            return rows or False

        WebDriverWait(driver, 25).until(visible_rows)
        keywords: list[str] = []
        max_pages = min(6, (requested + 24) // 25 + 1)
        for _page in range(max_pages):
            rows = visible_rows(driver) or []
            first_row = rows[0].text.strip() if rows else ""
            for row in rows:
                lines = [
                    re.sub(r"\s+", " ", line).strip()
                    for line in row.text.splitlines()
                    if re.sub(r"\s+", " ", line).strip()
                ]
                keyword = lines[0] if lines else ""
                if keyword and keyword not in keywords:
                    keywords.append(keyword)
                if len(keywords) >= requested:
                    break
            if len(keywords) >= requested:
                break

            buttons = [
                button
                for button in driver.find_elements(By.CSS_SELECTOR, "button")
                if button.is_displayed()
                and (
                    "다음 페이지" in (button.get_attribute("aria-label") or "")
                    or "next page" in (
                        button.get_attribute("aria-label") or ""
                    ).casefold()
                )
            ]
            if not buttons:
                buttons = [
                    button
                    for button in driver.find_elements(
                        By.CSS_SELECTOR,
                        "button.pYTkkf-Bz112c-LgbsSe",
                    )
                    if button.is_displayed()
                ][-1:]
            if not buttons:
                break
            next_button = buttons[-1]
            if (
                not next_button.is_enabled()
                or next_button.get_attribute("disabled") is not None
                or next_button.get_attribute("aria-disabled") == "true"
            ):
                break
            driver.execute_script("arguments[0].click()", next_button)

            def page_changed(current):
                current_rows = visible_rows(current) or []
                return bool(
                    current_rows
                    and current_rows[0].text.strip()
                    and current_rows[0].text.strip() != first_row
                )

            try:
                WebDriverWait(driver, 15).until(page_changed)
            except TimeoutException:
                break

        self.log(
            f"Google 인기 검색어 {len(keywords[:requested])}개를 가져왔습니다."
        )
        return keywords[:requested]

    def fetch_adsensefarm_realtime(self, limit: int = 50) -> list[str]:
        """Collect the visible realtime keyword cards from AdsenseFarm."""
        requested = max(1, min(int(limit), 50))
        driver = self._driver()
        driver.get("https://adsensefarm.kr/realtime")

        def visible_keywords(current):
            title = (current.title or "").casefold()
            body = current.find_element(By.TAG_NAME, "body").text.casefold()
            challenge = (
                "just a moment" in title
                or "잠시만 기다리십시오" in title
                or "보안 확인 수행 중" in body
                or "verify you are human" in body
            )
            values = current.execute_script(
                """
                const output = [];
                const headings = Array.from(document.querySelectorAll('h2'));
                for (const heading of headings) {
                    const title = (heading.innerText || '').trim();
                    if (!title.includes('실시간 검색어')) continue;
                    const card = heading.closest('.item') || heading.parentElement;
                    if (!card) continue;
                    let nodes = Array.from(
                        card.querySelectorAll('.kwds .keyword a')
                    );
                    if (!nodes.length) {
                        nodes = Array.from(card.querySelectorAll('a'));
                    }
                    for (const node of nodes.slice(0, 10)) {
                        const keyword = (node.innerText || node.textContent || '')
                            .replace(/\\s+/g, ' ').trim();
                        if (keyword) output.push(keyword);
                    }
                }
                return output;
                """
            ) or []
            normalized = []
            for value in values:
                keyword = unicodedata.normalize("NFC", str(value))
                keyword = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f]+", " ", keyword)).strip()
                if keyword and keyword not in normalized:
                    normalized.append(keyword)
            if normalized:
                return normalized
            if challenge:
                return False
            return False

        try:
            keywords = WebDriverWait(driver, 35).until(visible_keywords)
        except TimeoutException as exc:
            raise RuntimeError(
                "애드센스팜 검색어를 읽지 못했습니다. 열린 웨일 창에서 "
                "'사람인지 확인'을 완료한 뒤 다시 시도해 주세요."
            ) from exc
        result = list(keywords)[:requested]
        self.log(f"애드센스팜 실시간 검색어 {len(result)}개를 가져왔습니다.")
        return result

    def fetch_creator_advisor_trends(
        self,
        blog_id: str = "macdcross",
        category: str = "비즈니스·경제",
        limit: int = 20,
    ) -> list[str]:
        driver = self._require_naver_login(self._driver())
        url = (
            "https://creator-advisor.naver.com/naver_blog/"
            f"{urllib.parse.quote(blog_id or 'macdcross')}/trends"
        )
        if (driver.current_url or "").rstrip("/") != url.rstrip("/"):
            driver.get(url)
        WebDriverWait(driver, 25).until(
            lambda current: current.execute_script(
                "return document.readyState"
            )
            == "complete"
        )
        if "nidlogin.login" in (driver.current_url or "").casefold():
            raise RuntimeError(
                "크리에이터 어드바이저 조회에는 네이버 로그인이 필요합니다."
            )

        def click_exact_text(label: str) -> None:
            try:
                element = WebDriverWait(driver, 4).until(
                    lambda current: next(
                        (
                            item
                            for item in current.find_elements(
                                By.XPATH,
                                f"//*[normalize-space(text())='{label}']",
                            )
                            if item.is_displayed()
                        ),
                        None,
                    )
                )
                driver.execute_script("arguments[0].click()", element)
                time.sleep(0.5)
            except Exception:
                pass

        click_exact_text("검색 유입 트렌드")
        click_exact_text("주제별 인기유입검색어")
        activated = driver.execute_script(
            r"""
            const wanted = arguments[0].replace(/\s/g, '').replace(/·/g, '');
            for (const swiper of document.querySelectorAll(
                    '.u_ni_search_swiper')) {
              const slides = Array.from(swiper.querySelectorAll('.swiper-slide'));
              const index = slides.findIndex(slide => {
                const heading = ((slide.innerText || slide.textContent || '')
                  .split('\n')[0] || '').replace(/\s/g, '').replace(/·/g, '');
                return heading === wanted;
              });
              if (index < 0) continue;
              if (swiper.swiper && typeof swiper.swiper.slideTo === 'function') {
                swiper.swiper.slideTo(index, 0);
              } else {
                swiper.scrollLeft = Math.max(0, index * swiper.clientWidth);
                slides[index].scrollIntoView({block: 'nearest', inline: 'center'});
              }
              return true;
            }
            return false;
            """,
            category,
        )
        if not activated:
            raise RuntimeError(
                f"크리에이터 어드바이저에서 '{category}' 카드를 찾지 못했습니다."
            )

        def loaded_category_block(current):
            wanted = category.replace(" ", "").replace("·", "")
            for slide in current.find_elements(
                By.CSS_SELECTOR,
                ".u_ni_search_swiper .swiper-slide",
            ):
                text = (
                    slide.get_attribute("innerText")
                    or slide.get_attribute("textContent")
                    or ""
                ).strip()
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if not lines:
                    continue
                heading = lines[0].replace(" ", "").replace("·", "")
                if (
                    heading == wanted
                    and len(lines) >= min(limit, 10)
                    and "데이터를 불러오고 있습니다" not in text
                ):
                    return text
            return False

        block = WebDriverWait(driver, 25).until(loaded_category_block)
        keywords = self._extract_creator_advisor_keywords(
            [str(block)], category, limit
        )
        if not keywords:
            raise RuntimeError(
                f"크리에이터 어드바이저에서 '{category}' 키워드를 찾지 못했습니다."
            )
        self.log(
            f"크리에이터 어드바이저 {category} 인기 유입 검색어 "
            f"{len(keywords)}개를 가져왔습니다."
        )
        return keywords

    @staticmethod
    def _find_whale() -> Path:
        application_dirs = [
            Path(os.environ.get(name, "")) / "Naver/Naver Whale/Application"
            for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if os.environ.get(name)
        ]
        versioned: list[tuple[tuple[int, ...], Path]] = []
        for application_dir in application_dirs:
            if not application_dir.is_dir():
                continue
            for child in application_dir.iterdir():
                if child.is_dir() and re.fullmatch(r"\d+(?:\.\d+){3}", child.name):
                    executable = child / "whale.exe"
                    if executable.is_file():
                        versioned.append(
                            (tuple(int(part) for part in child.name.split(".")), executable)
                        )
        if versioned:
            return max(versioned, key=lambda item: item[0])[1]

        candidates = [
            application_dir / "whale.exe" for application_dir in application_dirs
        ]
        command = shutil.which("whale.exe")
        if command:
            candidates.insert(0, Path(command))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise RuntimeError(
            "네이버 웨일을 찾지 못했습니다. 웨일을 설치한 뒤 다시 실행해 주세요."
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _normal_whale_user_data() -> Path:
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise RuntimeError(
                "일반 웨일의 사용자 데이터 폴더를 찾을 수 없습니다."
            )
        return Path(local_app_data) / "Naver" / "Naver Whale" / "User Data"

    def _normal_whale_profile(self) -> tuple[Path, str]:
        user_data = self._normal_whale_user_data()
        local_state = user_data / "Local State"
        if not local_state.is_file():
            raise RuntimeError(
                "일반 웨일 프로필을 찾을 수 없습니다. 웨일을 한 번 실행한 뒤 다시 시도해 주세요."
            )

        try:
            text = local_state.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                "일반 웨일 프로필 정보를 읽을 수 없습니다."
            ) from exc

        profile_name = ""
        try:
            profile_name = str(
                json.loads(text).get("profile", {}).get("last_used", "")
            ).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            match = re.search(r'"last_used"\s*:\s*"([^"]+)"', text)
            profile_name = match.group(1).strip() if match else ""

        def usable(name: str) -> bool:
            return bool(
                (name == "Default" or re.fullmatch(r"Profile \d+", name))
                and (user_data / name).is_dir()
            )

        if not usable(profile_name):
            candidates = [
                path
                for path in user_data.iterdir()
                if path.is_dir()
                and (path.name == "Default" or re.fullmatch(r"Profile \d+", path.name))
            ]
            candidates.sort(
                key=lambda path: (
                    (path / "Network" / "Cookies").stat().st_mtime
                    if (path / "Network" / "Cookies").is_file()
                    else 0
                ),
                reverse=True,
            )
            profile_name = candidates[0].name if candidates else ""

        if not profile_name:
            raise RuntimeError(
                "네이버 로그인이 저장된 일반 웨일 프로필을 찾을 수 없습니다."
            )
        return user_data, profile_name

    @staticmethod
    def _is_profile_lock_error(exc: BaseException) -> bool:
        return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {
            32,
            33,
        }

    def _copy_normal_whale_profile_for_import(self, staging_root: Path) -> str:
        user_data, profile_name = self._normal_whale_profile()
        source_profile = user_data / profile_name
        source_cookies = source_profile / "Network" / "Cookies"
        if not source_cookies.is_file():
            source_cookies = source_profile / "Cookies"
        if not source_cookies.is_file():
            raise RuntimeError(
                "일반 웨일 프로필에서 네이버 로그인 정보를 찾을 수 없습니다."
            )

        try:
            # Whale denies read sharing while the normal profile is running.  Probe
            # before copying any profile metadata so a failed import leaves no
            # sensitive staging files behind.
            with source_cookies.open("rb") as source:
                source.read(1)

            staging_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(user_data / "Local State", staging_root / "Local State")
            target_profile = staging_root / profile_name
            target_network = target_profile / "Network"
            target_network.mkdir(parents=True, exist_ok=True)

            for filename in ("Preferences", "Secure Preferences"):
                source = source_profile / filename
                if source.is_file():
                    shutil.copy2(source, target_profile / filename)

            target_cookies = target_network / "Cookies"
            shutil.copy2(source_cookies, target_cookies)
            for suffix in ("-wal", "-shm", "-journal"):
                source = Path(f"{source_cookies}{suffix}")
                if source.is_file():
                    shutil.copy2(source, Path(f"{target_cookies}{suffix}"))
        except OSError as exc:
            if self._is_profile_lock_error(exc):
                raise RuntimeError(
                    "일반 웨일이 열려 있어 기존 네이버 로그인을 가져올 수 없습니다. "
                    "열려 있는 일반 웨일 창을 모두 닫은 뒤 같은 버튼을 다시 눌러 주세요. "
                    "프로그램은 사용 중인 웨일을 강제로 종료하지 않습니다."
                ) from exc
            raise RuntimeError(
                "일반 웨일의 네이버 로그인 정보를 복사하지 못했습니다."
            ) from exc
        return profile_name

    @staticmethod
    def _driver_major(path: Path) -> str:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [str(path), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=flags,
            )
            match = re.search(r"ChromeDriver\s+(\d+)", result.stdout)
            return match.group(1) if match else ""
        except Exception:
            return ""

    def _ensure_chromedriver(self, chrome_version: str) -> Path:
        major = chrome_version.split(".", 1)[0]
        driver_dir = self.data_dir / "webdrivers"
        driver_dir.mkdir(parents=True, exist_ok=True)
        target = driver_dir / f"chromedriver-{major}.exe"
        if target.is_file() and self._driver_major(target) == major:
            return target

        bundled_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        bundled = bundled_root / "drivers" / "chromedriver.exe"
        if bundled.is_file() and self._driver_major(bundled) == major:
            shutil.copy2(bundled, target)
            return target

        self.log(f"웨일 Chromium {chrome_version}용 자동화 드라이버를 준비합니다.")
        build = ".".join(chrome_version.split(".")[:3])
        api_urls = [
            (
                "https://googlechromelabs.github.io/chrome-for-testing/"
                "latest-patch-versions-per-build-with-downloads.json",
                "builds",
                build,
            ),
            (
                "https://googlechromelabs.github.io/chrome-for-testing/"
                "latest-versions-per-milestone-with-downloads.json",
                "milestones",
                major,
            ),
        ]
        download_url = ""
        for api_url, collection, key in api_urls:
            try:
                entry = requests.get(api_url, timeout=25).json().get(collection, {}).get(key, {})
                choices = entry.get("downloads", {}).get("chromedriver", [])
                download_url = next(
                    (
                        item["url"]
                        for item in choices
                        if item.get("platform") in {"win64", "win32"}
                    ),
                    "",
                )
                if download_url:
                    break
            except Exception:
                continue
        if not download_url:
            raise RuntimeError(
                f"웨일 Chromium {chrome_version}와 호환되는 자동화 드라이버를 찾지 못했습니다."
            )
        response = requests.get(download_url, timeout=90)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            member = next(
                (name for name in archive.namelist() if name.endswith("/chromedriver.exe")),
                None,
            )
            if not member:
                raise RuntimeError("다운로드한 자동화 드라이버 압축 파일이 올바르지 않습니다.")
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(archive.read(member))
            temporary.replace(target)
        if self._driver_major(target) != major:
            raise RuntimeError("웨일과 자동화 드라이버 버전이 일치하지 않습니다.")
        return target

    def _launch_whale_legacy(self) -> tuple[int, str]:
        whale = self._find_whale()
        port = self._free_port()
        profile = (self.data_dir / "naver-whale-profile").resolve()
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            str(whale),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--start-maximized",
            "--disable-notifications",
            "--no-first-run",
            "about:blank",
        ]
        self.whale_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        version_url = f"http://127.0.0.1:{port}/json/version"
        for _ in range(40):
            if self.whale_process.poll() is not None:
                break
            try:
                info = requests.get(version_url, timeout=1).json()
                match = re.search(r"Chrome/([\d.]+)", info.get("Browser", ""))
                if match:
                    return port, match.group(1)
            except Exception:
                time.sleep(0.25)
        self._stop_whale_process()
        raise RuntimeError("네이버 웨일 자동화 연결을 시작하지 못했습니다.")

    def _find_running_automation_whale(self) -> tuple[int, str] | None:
        """Reuse only the exact app profile and the CDP port owned by that process."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            netstat = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=flags,
            )
            processes = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                    "Get-CimInstance Win32_Process -Filter \"Name='whale.exe'\" | "
                    "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=flags,
            )
        except Exception:
            return None

        owned_endpoints: set[tuple[int, int]] = set()
        try:
            rows = json.loads(processes.stdout.lstrip("\ufeff").strip() or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                if not isinstance(row, dict) or type(row.get("ProcessId")) is not int:
                    continue
                port = self._owned_whale_command_port(row.get("CommandLine", ""))
                if port is not None:
                    owned_endpoints.add((row["ProcessId"], port))
        except (TypeError, ValueError, AttributeError):
            return None

        candidates: list[int] = []
        for match in re.finditer(
            r"^\s*TCP\s+127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$",
            netstat.stdout,
            re.MULTILINE | re.IGNORECASE,
        ):
            port, pid = int(match.group(1)), int(match.group(2))
            if (pid, port) in owned_endpoints:
                candidates.append(port)

        for port in dict.fromkeys(candidates):
            try:
                info = requests.get(
                    f"http://127.0.0.1:{port}/json/version",
                    timeout=1,
                ).json()
                match = re.search(r"Chrome/([\d.]+)", info.get("Browser", ""))
                websocket = str(info.get("webSocketDebuggerUrl", ""))
                socket_url = urllib.parse.urlparse(websocket)
                if match and socket_url.scheme == "ws" and socket_url.hostname == "127.0.0.1" and socket_url.port == port:
                    return port, match.group(1)
            except Exception:
                continue
        return None

    def _owned_whale_command_port(self, command_line: str) -> int | None:
        if not isinstance(command_line, str) or not command_line or os.name != "nt":
            return None
        # CommandLineToArgvW handles both whole quoted arguments and a quoted
        # value after '='. A substring search can match an unrelated user-agent.
        parser = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
        parser.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        parser.restype = ctypes.POINTER(ctypes.c_wchar_p)
        release = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_void_p
        count = ctypes.c_int()
        arguments = parser(command_line, ctypes.byref(count))
        if not arguments:
            return None
        try:
            values = [arguments[index] for index in range(count.value)]
        finally:
            release(arguments)
        if not values or Path(values[0]).name.casefold() != "whale.exe":
            return None
        options = {}
        for index, argument in enumerate(values[1:], 1):
            name, separator, value = argument.partition("=")
            if name == "--type":
                return None
            if name not in {"--user-data-dir", "--remote-debugging-port"}:
                continue
            if name in options:
                return None
            options[name] = value if separator else (values[index + 1] if index + 1 < len(values) else "")
        profile = options.get("--user-data-dir", "")
        raw_port = options.get("--remote-debugging-port", "")
        try:
            expected = (self.data_dir / "naver-whale-profile").resolve()
            actual = Path(profile)
            if not profile or not actual.is_absolute() or actual.resolve() != expected:
                return None
            port = int(raw_port)
        except (OSError, ValueError):
            return None
        return port if 0 < port < 65536 else None

    def _start_whale_instance(
        self,
        user_data_dir: Path,
        profile_name: str = "",
        *,
        headless: bool = False,
    ) -> tuple[subprocess.Popen, int, str]:
        whale = self._find_whale()
        port = self._free_port()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(whale),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--start-maximized",
            "--disable-notifications",
            "--no-first-run",
        ]
        if profile_name:
            command.append(f"--profile-directory={profile_name}")
        if headless:
            command.extend(["--headless=new", "--disable-gpu"])
        command.append("about:blank")
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        version_url = f"http://127.0.0.1:{port}/json/version"
        for _ in range(40):
            if process.poll() is not None:
                break
            try:
                info = requests.get(version_url, timeout=1).json()
                match = re.search(r"Chrome/([\d.]+)", info.get("Browser", ""))
                if match:
                    return process, port, match.group(1)
            except Exception:
                time.sleep(0.25)
        self._terminate_owned_process(process)
        raise RuntimeError(
            "네이버 웨일 자동화 연결을 시작하지 못했습니다."
        )

    def _launch_whale(self) -> tuple[int, str]:
        existing = self._find_running_automation_whale()
        if existing:
            self.attached_existing_whale = True
            self.whale_process = None
            self.log(
                "이미 열려 있는 로그인된 웨일 자동화 창에 다시 연결합니다."
            )
            return existing

        self.attached_existing_whale = False
        # 최초 버전에서 안정적으로 동작했던 전용 프로필 실행 경로를 사용한다.
        return self._launch_whale_legacy()

    def _driver(self):
        if self.driver:
            try:
                _ = self.driver.current_window_handle
                return self.driver
            except Exception:
                self.driver = None
                self._stop_whale_process()
        self.log("네이버 웨일을 실행하고 자동화 연결을 준비합니다.")
        port, chrome_version = self._launch_whale()
        driver_path = self._ensure_chromedriver(chrome_version)
        options = webdriver.ChromeOptions()
        options.debugger_address = f"127.0.0.1:{port}"
        try:
            self.driver = webdriver.Chrome(
                service=Service(str(driver_path)), options=options
            )
        except Exception:
            self._stop_whale_process()
            raise
        self.driver.set_page_load_timeout(60)
        self.driver.set_script_timeout(30)
        self.log(f"네이버 웨일 연결 완료 · Chromium {chrome_version}")
        return self.driver

    @staticmethod
    def _terminate_owned_process(process):
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=4)
        except Exception:
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=flags,
                )
            except Exception:
                pass

    def _stop_whale_process(self):
        process = self.whale_process
        self.whale_process = None
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=4)
        except Exception:
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=flags,
                )
            except Exception:
                pass

    def close(self):
        if self.driver:
            if not self.attached_existing_whale:
                try:
                    self.driver.execute_cdp_cmd("Browser.close", {})
                    time.sleep(0.5)
                except Exception:
                    pass
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        self.attached_existing_whale = False
        self._stop_whale_process()

    @staticmethod
    def _sanitized_naver_cookie(cookie: dict) -> dict | None:
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        domain = str(cookie.get("domain", "")).strip().lower()
        normalized_domain = domain.lstrip(".")
        if (
            not name
            or not value
            or (
                normalized_domain != "naver.com"
                and not normalized_domain.endswith(".naver.com")
            )
        ):
            return None

        result = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure", False)),
            "httpOnly": bool(cookie.get("httpOnly", False)),
        }
        same_site = cookie.get("sameSite")
        if same_site in {"Strict", "Lax", "None"}:
            result["sameSite"] = same_site
        try:
            expires = float(cookie.get("expires", -1))
            if expires > 0:
                result["expires"] = expires
        except (TypeError, ValueError):
            pass
        return result

    def _remove_import_staging(self, staging_root: Path) -> None:
        try:
            base = self.data_dir.resolve()
            target = staging_root.resolve()
        except OSError:
            return
        if (
            target.parent != base
            or not target.name.startswith("naver-login-import-")
        ):
            return
        for _ in range(4):
            try:
                shutil.rmtree(target)
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.25)

    def _import_existing_naver_session(self, target_driver) -> bool:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(
                prefix="naver-login-import-",
                dir=str(self.data_dir),
            )
        )
        temporary_process = None
        temporary_driver = None
        try:
            profile_name = self._copy_normal_whale_profile_for_import(staging_root)
            self.log(
                "일반 웨일의 기존 네이버 로그인 상태를 안전하게 확인하고 있습니다."
            )
            temporary_process, port, chrome_version = self._start_whale_instance(
                staging_root,
                profile_name,
                headless=True,
            )
            driver_path = self._ensure_chromedriver(chrome_version)
            options = webdriver.ChromeOptions()
            options.debugger_address = f"127.0.0.1:{port}"
            temporary_driver = webdriver.Chrome(
                service=Service(str(driver_path)),
                options=options,
            )
            temporary_driver.set_page_load_timeout(45)
            temporary_driver.set_script_timeout(20)
            temporary_driver.get("https://section.blog.naver.com/BlogHome.naver")
            WebDriverWait(temporary_driver, 20).until(
                lambda d: d.execute_script("return document.readyState")
                == "complete"
            )
            raw_cookies = temporary_driver.execute_cdp_cmd(
                "Network.getAllCookies", {}
            ).get("cookies", [])
            cookies = [
                prepared
                for prepared in (
                    self._sanitized_naver_cookie(cookie)
                    for cookie in raw_cookies
                )
                if prepared
            ]
            if not any(
                cookie["name"] in {"NID_SES", "NID_AUT"}
                and cookie["value"]
                for cookie in cookies
            ):
                raise RuntimeError(
                    "일반 웨일 프로필에서 활성 네이버 로그인을 확인하지 못했습니다. "
                    "일반 웨일에서 네이버 로그인 상태를 확인한 뒤 웨일 창을 모두 닫고 "
                    "다시 시도해 주세요. 계속 실패하면 '네이버 웨일 로그인 창 열기'에서 "
                    "자동화 전용 창에 한 번 로그인해 주세요."
                )

            target_driver.execute_cdp_cmd("Network.enable", {})
            target_driver.execute_cdp_cmd(
                "Network.setCookies",
                {"cookies": cookies},
            )
            self.log(
                "일반 웨일의 네이버 로그인 상태를 자동화 창으로 가져왔습니다."
            )
            return True
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "일반 웨일의 네이버 로그인 상태를 가져오지 못했습니다. "
                "일반 웨일 창을 모두 닫은 뒤 다시 시도해 주세요."
            ) from exc
        finally:
            if temporary_driver:
                try:
                    temporary_driver.execute_cdp_cmd("Browser.close", {})
                except Exception:
                    pass
                try:
                    temporary_driver.quit()
                except Exception:
                    pass
            self._terminate_owned_process(temporary_process)
            self._remove_import_staging(staging_root)

    def _has_naver_session(self, driver) -> bool:
        driver.switch_to.default_content()
        driver.get("https://section.blog.naver.com/BlogHome.naver")
        try:
            WebDriverWait(driver, 20).until(
                lambda d: d.execute_script("return document.readyState")
                == "complete"
            )
            WebDriverWait(driver, 5).until(
                lambda d: self._naver_logged_in(d)
            )
        except TimeoutException:
            pass
        current_url = (driver.current_url or "").lower()
        return bool(
            self._naver_logged_in(driver)
            and "nid.naver.com" not in current_url
            and "login" not in current_url
        )

    def open_login(self, blog_id: str):
        driver = self._driver()
        driver.get(f"https://blog.naver.com/{blog_id}")
        self.log(
            "네이버 웨일 창을 열었습니다. 이 전용 자동화 창에서 네이버에 로그인해 주세요. "
            "로그인 상태는 다음 실행에도 유지됩니다."
        )

    def open_url(self, url: str) -> None:
        if not re.match(r"^https://(?:www\.)?google\.", url, re.IGNORECASE):
            raise ValueError("허용되지 않은 외부 검색 주소입니다.")
        driver = self._driver()
        driver.get(url)
        self.log("네이버 웨일에서 Google 이미지 검색 결과를 열었습니다.")

    def _save_internal_image_source_history(self, records: list[dict]) -> None:
        if not records:
            return
        try:
            existing = json.loads(
                self.image_source_history_file.read_text(encoding="utf-8")
            )
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
        _save(
            self.image_source_history_file,
            [*existing, *records][-2000:],
        )

    @staticmethod
    def _save_clean_jpeg(image: Image.Image, path: Path) -> None:
        clean_image = Image.new("RGB", image.size)
        clean_image.paste(image.convert("RGB"))
        clean_image.save(
            path,
            "JPEG",
            quality=94,
            optimize=True,
            exif=b"",
        )

    @staticmethod
    def _download_google_preview(source_url: str) -> Image.Image | None:
        """Load the real preview source so CSS object-fit cannot crop portraits."""
        if not source_url or source_url.startswith(("blob:", "file:")):
            return None
        try:
            if source_url.startswith("data:image/"):
                header, encoded = source_url.split(",", 1)
                if ";base64" not in header:
                    return None
                import base64

                payload = base64.b64decode(encoded, validate=True)
            else:
                response = requests.get(
                    source_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                        ),
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        "Referer": "https://www.google.com/",
                    },
                    timeout=18,
                )
                response.raise_for_status()
                if len(response.content) > 30 * 1024 * 1024:
                    return None
                payload = response.content
            with Image.open(io.BytesIO(payload)) as downloaded:
                image = ImageOps.exif_transpose(downloaded).convert("RGB")
                image.load()
            if image.width < 240 or image.height < 160:
                return None
            return image
        except Exception:
            return None

    @staticmethod
    def _gently_enhance_google_image(image: Image.Image) -> Image.Image:
        image = image.convert("RGB")
        long_side = max(image.size)
        if long_side < 1800:
            ratio = min(3.0, 1800 / max(1, long_side))
            final_size = (
                max(1, round(image.width * ratio)),
                max(1, round(image.height * ratio)),
            )
            if ratio > 1.65:
                step = ratio**0.5
                image = image.resize(
                    (
                        max(1, round(image.width * step)),
                        max(1, round(image.height * step)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            image = image.resize(final_size, Image.Resampling.LANCZOS)
        image = ImageOps.autocontrast(image, cutoff=0.35, preserve_tone=True)
        image = ImageEnhance.Contrast(image).enhance(1.015)
        return image.filter(
            ImageFilter.UnsharpMask(radius=0.9, percent=55, threshold=4)
        )

    def capture_google_images(
        self,
        query: str,
        output_dir: Path,
        count: int = 10,
        enhance: bool = True,
    ) -> list[str]:
        """재사용 가능 라이선스 필터 결과의 오른쪽 미리보기 이미지만 저장한다."""
        query = (query or "").strip()
        if not query:
            raise ValueError("Google 이미지 검색어가 없습니다.")
        count = max(1, min(20, int(count)))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        driver = self._driver()
        url = "https://www.google.com/search?" + urllib.parse.urlencode(
            {
                "tbm": "isch",
                "hl": "en",
                "safe": "active",
                # Google 이미지 검색의 Creative Commons 라이선스 필터.
                "tbs": "il:cl",
                "q": query,
            }
        )
        driver.get(url)
        WebDriverWait(driver, 25).until(
            lambda d: len(
                d.find_elements(
                    By.CSS_SELECTOR,
                    "img.YQ4gaf, img.rg_i, div[data-ri] img, a img",
                )
            )
            >= 6
        )
        saved: list[str] = []
        metadata: list[dict] = []
        seen_hashes: set[str] = set()
        seen_sources: set[str] = set()
        attempted: set[str] = set()

        for _page in range(8):
            if self.stop_event.is_set():
                raise RuntimeError("사용자가 전체 자동화를 중지했습니다.")
            thumbnails = driver.find_elements(
                By.CSS_SELECTOR,
                "img.YQ4gaf, img.rg_i, div[data-ri] img, a img",
            )
            for thumbnail in thumbnails:
                if len(saved) >= count:
                    break
                try:
                    if not thumbnail.is_displayed():
                        continue
                    rectangle = thumbnail.rect
                    if rectangle["width"] < 110 or rectangle["height"] < 80:
                        continue
                    token = (
                        thumbnail.get_attribute("src")
                        or thumbnail.get_attribute("data-src")
                        or thumbnail.get_attribute("alt")
                        or str(rectangle)
                    )
                    if token in attempted:
                        continue
                    attempted.add(token)
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});",
                        thumbnail,
                    )
                    driver.execute_script("arguments[0].click();", thumbnail)
                    time.sleep(0.8)
                    viewport_width = driver.execute_script(
                        "return window.innerWidth || 1200;"
                    )
                    previews = driver.find_elements(
                        By.CSS_SELECTOR,
                        "img.sFlh5c, img.iPVvYb, img.n3VNCb, "
                        "div[role='dialog'] img, div[aria-live] img",
                    )
                    viable = []
                    for image in previews:
                        if not image.is_displayed():
                            continue
                        info = driver.execute_script(
                            """
                            const r = arguments[0].getBoundingClientRect();
                            return {
                              left: r.left, width: r.width, height: r.height,
                              naturalWidth: arguments[0].naturalWidth || 0,
                              naturalHeight: arguments[0].naturalHeight || 0
                            };
                            """,
                            image,
                        )
                        if (
                            info["left"] >= viewport_width * 0.45
                            and info["width"] >= 240
                            and info["height"] >= 160
                            and info["naturalWidth"] >= 300
                            and info["naturalHeight"] >= 200
                        ):
                            viable.append(
                                (
                                    info["naturalWidth"] * info["naturalHeight"],
                                    image,
                                )
                            )
                    if not viable:
                        continue
                    viable.sort(key=lambda item: item[0], reverse=True)
                    preview = viable[0][1]
                    source_url = (
                        driver.execute_script(
                            "return arguments[0].currentSrc || arguments[0].src || '';",
                            preview,
                        )
                        or preview.get_attribute("data-src")
                        or ""
                    )
                    result_page_url = driver.execute_script(
                        """
                        const preview = arguments[0];
                        const thumbnail = arguments[1];
                        const link = preview.closest('a[href]')
                          || thumbnail.closest('a[href]');
                        return link ? link.href : '';
                        """,
                        preview,
                        thumbnail,
                    )
                    if source_url and source_url in seen_sources:
                        continue
                    image = self._download_google_preview(source_url)
                    if image is None:
                        # Last-resort screenshot: force the image itself to "contain"
                        # so its full subject is visible and stop above caption text.
                        old_style = preview.get_attribute("style") or ""
                        try:
                            driver.execute_script(
                                """
                                arguments[0].style.setProperty(
                                  'object-fit', 'contain', 'important'
                                );
                                arguments[0].style.setProperty(
                                  'object-position', 'center center', 'important'
                                );
                                """,
                                preview,
                            )
                            time.sleep(0.15)
                            png = preview.screenshot_as_png
                        finally:
                            driver.execute_script(
                                "arguments[0].setAttribute('style', arguments[1]);",
                                preview,
                                old_style,
                            )
                        with Image.open(io.BytesIO(png)) as source:
                            image = source.convert("RGB")
                    if image.width < 240 or image.height < 160:
                        continue
                    digest = hashlib.sha256(
                        f"{image.width}x{image.height}".encode("ascii")
                        + image.tobytes()
                    ).hexdigest()
                    if digest in seen_hashes:
                        continue
                    if enhance:
                        image = self._gently_enhance_google_image(image)
                    path = output_dir / f"google_cc_{len(saved) + 1:02d}.jpg"
                    # 원본 EXIF·설명·ICC·출처 메타데이터가 따라가지 않도록
                    # 픽셀만 새 RGB 이미지에 복사해 저장한다.
                    self._save_clean_jpeg(image, path)
                    seen_hashes.add(digest)
                    if source_url:
                        seen_sources.add(source_url)
                    saved.append(str(path))
                    metadata.append(
                        {
                            "file": path.name,
                            "query": query,
                            "source_url": source_url,
                            "result_page_url": result_page_url,
                            "license_filter": "Creative Commons",
                            "captured_at": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                        }
                    )
                    self.log(
                        f"Google 이미지 오른쪽 미리보기 {len(saved)}/{count}장 저장"
                    )
                except Exception:
                    continue
            if len(saved) >= count:
                break
            driver.execute_script(
                "window.scrollBy(0, Math.max(900, window.innerHeight * 0.85));"
            )
            time.sleep(1)

        self._save_internal_image_source_history(metadata)
        if len(saved) < count:
            raise RuntimeError(
                f"Creative Commons 필터 결과에서 이미지 {count}장 중 "
                f"{len(saved)}장만 확보했습니다. 임시저장은 진행하지 않습니다."
            )
        return saved

    @staticmethod
    def _reference_source_url(urls: list[str]) -> str:
        """Resolve Google image-result links to an ordinary source page."""
        for raw in urls:
            value = str(raw or "")
            parts = urllib.parse.urlparse(value)
            if parts.hostname and re.search(r"(^|\.)google\.[a-z.]+$", parts.hostname):
                query = urllib.parse.parse_qs(parts.query)
                value = (query.get("imgrefurl") or query.get("url") or [""])[0]
                parts = urllib.parse.urlparse(value)
            if (
                parts.scheme in {"https", "http"}
                and parts.hostname
                and not re.search(r"(^|\.)google\.[a-z.]+$", parts.hostname)
                and ("/wiki/File:" in parts.path or not re.search(r"\.(?:jpe?g|png|webp|gif)(?:$|\?)", parts.path, re.I))
            ):
                return value
        return ""

    @staticmethod
    def _commons_license_evidence(source_url: str, image_url: str, page: dict) -> dict:
        """Accept only file-specific, browser-observed Wikimedia license data.

        Search filters, arbitrary page text and resizing never establish a license.
        Other sources remain unverified so the optional reference is excluded.
        """
        result = {"license_verified": False, "license_url": "", "attribution": ""}
        source = urllib.parse.urlparse(source_url)
        image_source = urllib.parse.urlparse(image_url)
        file_name = urllib.parse.unquote(source.path).split("/wiki/File:", 1)
        if (
            source.hostname != "commons.wikimedia.org"
            or len(file_name) != 2
            or image_source.hostname != "upload.wikimedia.org"
            or file_name[1].replace(" ", "_")
            not in urllib.parse.unquote(image_source.path).split("/")
            or not page.get("original_file_present")
        ):
            return result
        author = re.sub(r"\s+", " ", str(page.get("author", ""))).strip()
        for value in page.get("license_links", []):
            license_parts = urllib.parse.urlparse(str(value))
            if license_parts.hostname != "creativecommons.org":
                continue
            # Commons links to localized CC deeds (e.g. /deed.en); these
            # identify the same canonical license, not a different grant.
            license_path = re.sub(r"/deed(?:\.[A-Za-z-]+)?$", "", license_parts.path.rstrip("/"))
            public_domain = license_path in {
                "/publicdomain/zero/1.0", "/publicdomain/mark/1.0"
            }
            # Share-alike and other additional restrictions need a separate
            # compliance workflow; they remain ineligible in this publisher.
            by_license = bool(re.fullmatch(r"/licenses/by/(?:2\.0|2\.5|3\.0|4\.0)", license_path))
            if not public_domain and not (by_license and author):
                continue
            license_url = "https://creativecommons.org" + license_path + "/"
            result.update({
                "license_verified": True,
                "license_url": license_url,
                "license": "CC0 1.0" if "/zero/" in license_path else (
                    "Public Domain Mark 1.0" if public_domain else license_path.removeprefix("/licenses/").upper()
                ),
                "commercial_use_allowed": True,
                "modification_allowed": True,
                "license_evidence_url": source_url,
                "license_evidence": "Wikimedia Commons file license template and matching original image",
                "attribution": f"{file_name[1]} · {author or 'Wikimedia Commons'} · {source_url} · {license_url} · 크기 조정",
                "share_alike": "/by-sa/" in license_path,
                "attribution_required": by_license,
            })
            break
        if not result["license_verified"]:
            for template in page.get("public_domain_templates", []):
                if not isinstance(template, dict) or template != {
                    "name": "Public domain", "link_required": "false", "attribution_required": "false"
                }:
                    continue
                # Observed on Commons' NASA files: an explicit, file-specific
                # PD license template provides machine-readable reuse terms
                # without linking to a Creative Commons deed.
                result.update({
                    "license_verified": True, "license": "Public domain",
                    "license_url": urllib.parse.urlunparse(source._replace(fragment="Licensing")),
                    "commercial_use_allowed": True, "modification_allowed": True,
                    "attribution_required": False, "share_alike": False,
                    "license_evidence_url": source_url,
                    "license_evidence_type": "commons_file_public_domain_template",
                    "license_evidence": "Matching Commons original file and explicit Public domain template; link/attribution not required",
                    "public_domain_template": dict(template),
                    "attribution": f"{file_name[1]} · {author or 'Wikimedia Commons'} · {source_url}",
                })
                break
        return result

    @staticmethod
    def _commons_public_domain_license_verified(item: dict) -> bool:
        """Check the persisted, file-specific PD template evidence at delivery."""
        source_url = str(item.get("source_url", ""))
        source = urllib.parse.urlparse(source_url)
        original = urllib.parse.urlparse(str(item.get("image_url", "")))
        file_name = urllib.parse.unquote(source.path).split("/wiki/File:", 1)
        return bool(
            item.get("license_evidence_type") == "commons_file_public_domain_template"
            and item.get("license") == "Public domain"
            and item.get("public_domain_template") == {
                "name": "Public domain", "link_required": "false", "attribution_required": "false"}
            and source.scheme == "https" and source.hostname == "commons.wikimedia.org"
            and len(file_name) == 2 and original.hostname == "upload.wikimedia.org"
            and file_name[1].replace(" ", "_") in urllib.parse.unquote(original.path).split("/")
            and item.get("license_evidence_url") == source_url
            and item.get("license_url") == urllib.parse.urlunparse(source._replace(fragment="Licensing"))
        )

    @staticmethod
    def _reference_language_evidence(page: dict, observed_url: str) -> dict:
        language = str(page.get("document_language", "")).strip().lower()
        description = re.sub(r"\s+", " ", str(page.get("english_description", ""))).strip()
        document_is_english = bool(re.fullmatch(r"en(?:-[a-z0-9]+)*", language))
        description_is_english = (page.get("english_description_visible") is True
                                  and len(re.findall(r"\b[A-Za-z]{2,}\b", description)) >= 3)
        verified = document_is_english or description_is_english
        return {"source_language": "en" if verified else language,
                "english_source_verified": verified, "language_evidence_url": observed_url,
                "language_evidence": ("Observed document.documentElement.lang=" + language if document_is_english
                                      else "Visible English-labelled file description" if description_is_english else ""),
                "english_description_excerpt": description[:300] if description_is_english else ""}

    def _inspect_reference_license(self, driver, source_url: str, image_url: str, *, english_only: bool = False) -> dict:
        unverified = {"license_verified": False, "license_url": "", "attribution": ""}
        if english_only:
            unverified.update(source_language="", english_source_verified=False)
        source = urllib.parse.urlparse(source_url)
        if source.hostname != "commons.wikimedia.org" or "/wiki/File:" not in source.path:
            return unverified
        previous = driver.current_window_handle
        opened = None
        try:
            driver.switch_to.new_window("tab")
            opened = driver.current_window_handle
            request_url = source_url
            if english_only:
                query = [(key, value) for key, value in urllib.parse.parse_qsl(source.query, keep_blank_values=True)
                         if key.lower() != "uselang"]
                query.append(("uselang", "en"))
                request_url = urllib.parse.urlunparse(source._replace(query=urllib.parse.urlencode(query)))
            driver.get(request_url)
            WebDriverWait(driver, 15).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, "#file, .licensetpl")
            )
            actual = urllib.parse.urlparse(driver.current_url)
            if english_only:
                if (actual.scheme not in {"https", "http"} or actual.hostname != source.hostname
                        or urllib.parse.unquote(actual.path) != urllib.parse.unquote(source.path)):
                    return unverified
            elif driver.current_url.split("#", 1)[0] != source_url.split("#", 1)[0]:
                return unverified
            page = driver.execute_script("""
                const authorLabel = document.getElementById('fileinfotpl_aut');
                const englishDescription=[...document.querySelectorAll(
                  '.description.en, .description[lang="en"], .description [lang="en"], [lang="en"] .description'
                )].find(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
                return {
                  original_file_present: Boolean(document.querySelector('#file a[href]')),
                  author: authorLabel && authorLabel.nextElementSibling
                    ? authorLabel.nextElementSibling.innerText : '',
                  license_links: [...document.querySelectorAll('.licensetpl a[href]')]
                    .map(a => a.href),
                  public_domain_templates: [...document.querySelectorAll('.licensetpl')].map(e=>({
                    name:e.querySelector('.licensetpl_short')?.textContent.trim() || '',
                    link_required:e.querySelector('.licensetpl_link_req')?.textContent.trim() || '',
                    attribution_required:e.querySelector('.licensetpl_attr_req')?.textContent.trim() || ''
                  })),
                  document_language: document.documentElement.lang || '',
                  english_description_visible: Boolean(englishDescription),
                  english_description: englishDescription?.innerText || ''
                };
            """)
            evidence = self._commons_license_evidence(source_url, image_url, page or {})
            if english_only:
                evidence.update(self._reference_language_evidence(page or {}, driver.current_url))
            return evidence
        except Exception as exc:
            self.log(f"참고 이미지 원문 라이선스를 확인하지 못해 제외 대상으로 표시합니다: {exc}")
            return unverified
        finally:
            if opened is not None:
                try:
                    driver.close()
                finally:
                    driver.switch_to.window(previous)

    @staticmethod
    def _reference_diagnostic_url(value) -> str:
        """Keep public page identity without URL credentials, queries or blobs."""
        if not isinstance(value, str):
            return ""
        try:
            parts = urllib.parse.urlparse(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                return (parts.scheme + ":(omitted)") if parts.scheme else ""
            host = parts.hostname + (":" + str(parts.port) if parts.port else "")
            return urllib.parse.urlunparse((parts.scheme, host, parts.path, "", "", ""))[:500]
        except ValueError:
            return ""

    @contextmanager
    def _google_capture_rendering(self, driver):
        """Keep this capture tab rendering when its window is minimized/occluded."""
        enabled = False
        command = getattr(driver, "execute_cdp_cmd", None)
        if callable(command):
            try:
                command("Emulation.setFocusEmulationEnabled", {"enabled": True})
                enabled = True
            except WebDriverException:
                self.log("Google 캡처 탭의 배경 렌더링을 설정하지 못해 현재 화면 상태로 확인합니다.")
        try:
            yield
        finally:
            if enabled:
                try:
                    command("Emulation.setFocusEmulationEnabled", {"enabled": False})
                except WebDriverException:
                    # The user can close the capture tab while cancellation unwinds.
                    pass

    def capture_google_reference_candidates(
        self, keyword: str, output_dir: Path, count: int = 2, *, reuse_only: bool = False, english_only: bool = False
    ) -> list[dict]:
        keyword = str(keyword or "").strip()
        if not keyword:
            raise ValueError("Google 참고 이미지 검색어가 없습니다.")
        if english_only and (not keyword.isascii() or re.search(r"[A-Za-z]{2,}", keyword) is None):
            raise ValueError("영어 이미지 검색에는 영문 단어로 번역된 검색어가 필요합니다.")
        driver = self._driver()
        # Native focus emulation makes the selected tab's lazy image viewer
        # render without moving the user's windows or changing image CSS.
        with self._google_capture_rendering(driver):
            return self._capture_google_reference_candidates(
                driver, keyword, output_dir, count, reuse_only=reuse_only, english_only=english_only)

    def _capture_google_reference_candidates(
        self, driver, keyword: str, output_dir: Path, count: int = 2, *, reuse_only: bool = False, english_only: bool = False
    ) -> list[dict]:
        """Capture up to ten eligible previews; licensing/vision gates stay explicit.

        Screenshots contain only the image element. Embedded text or watermarks
        are preserved and must be rejected by the later CLI visual review.
        reuse_only scans past ineligible results, accepting only file-specific
        Commons licenses that permit reuse without a public attribution line.
        english_only requires an English query and observed English source-page
        language, independently of Google's language filter.
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            raise ValueError("Google 참고 이미지 검색어가 없습니다.")
        if english_only and (not keyword.isascii() or re.search(r"[A-Za-z]{2,}", keyword) is None):
            raise ValueError("영어 이미지 검색에는 영문 단어로 번역된 검색어가 필요합니다.")
        count = max(1, min(10, int(count)))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        search_options = {
            "tbm": "isch", "hl": "en" if english_only else "ko", "safe": "active", "tbs": "il:cl",
            "q": keyword + (" site:commons.wikimedia.org" if reuse_only else "")
        }
        if english_only:
            search_options["lr"] = "lang_en"
        search_url = "https://www.google.com/search?" + urllib.parse.urlencode(search_options)
        # Current Google image results use unclassified dimg_* images inside
        # result cards. YQ4gaf now also occurs on tiny suggestion chips.
        selector = "div[data-img-wrapper] img, div[data-preview-id] img, img.YQ4gaf, img.rg_i, div[data-ri] img"
        candidates: list[dict] = []
        candidate_started = None
        termination_reason = ""
        consecutive_preview_failures = 0
        diagnostics = {"query": keyword, "english_only": english_only, "reuse_only": reuse_only,
                       "requested_count": count, "selector": selector, "scan_limit": 60,
                       "candidate_time_budget_seconds": 90, "preview_failure_limit": 3,
                       "thumbnails_found": 0, "scanned_count": 0, "rejection_counts": {}, "rejections": [],
                       "result_load_samples": [], "photo_candidates": [], "preview_attempts": []}
        def safe_text(value, limit=300):
            if not isinstance(value, str):
                return ""
            return re.sub(r"https?://[^\s<>]+", lambda m: self._reference_diagnostic_url(m.group()), value)[:limit]
        def reject(rank, reason, **observed):
            counts = diagnostics["rejection_counts"]
            counts[reason] = counts.get(reason, 0) + 1
            for key in list(observed):
                if key.endswith("url"):
                    observed[key] = self._reference_diagnostic_url(observed[key])
                elif key == "error":
                    observed[key] = safe_text(observed[key], 500)
            diagnostics["rejections"].append({"rank": rank, "reason": reason, **observed})
        def finish(status):
            diagnostics.update(status=status, captured_count=len(candidates))
            diagnostics["consecutive_preview_failures"] = consecutive_preview_failures
            if candidate_started is not None:
                diagnostics["candidate_elapsed_seconds"] = round(time.monotonic() - candidate_started, 2)
            try:
                diagnostics.update(final_url=self._reference_diagnostic_url(driver.current_url),
                                   final_title=safe_text(driver.title))
            except WebDriverException:
                diagnostics["page_observation_unavailable"] = True
            _save(output_dir / "google_reference_diagnostics.json", diagnostics)
            _save(output_dir / "google_reference_manifest.json", candidates)
            counts = ", ".join(f"{key}={value}" for key, value in diagnostics["rejection_counts"].items()) or "없음"
            self.log(f"Google 참고 이미지 진단 · 검색 요소 {diagnostics['thumbnails_found']}개 · "
                     f"검토 {diagnostics['scanned_count']}개 · 확보 {len(candidates)}/{count}장 · 제외: {counts}")
        latest_photos, latest_rejections = [], []
        previous_signature, stable_polls = None, 0
        def settled_photos(d):
            nonlocal latest_photos, latest_rejections, previous_signature, stable_polls
            if self.stop_event.is_set():
                raise RuntimeError("사용자가 작업을 중지했습니다.")
            thumbnails = d.find_elements(By.CSS_SELECTOR, selector)
            latest_photos, latest_rejections = [], []
            for rank, thumbnail in enumerate(thumbnails, 1):
                try:
                    if not thumbnail.is_displayed():
                        latest_rejections.append((rank, "thumbnail_hidden", {}))
                        continue
                    rect = thumbnail.rect
                    if rect["width"] < 110 or rect.get("height", 0) < 80:
                        latest_rejections.append((rank, "thumbnail_too_small", {"width": rect["width"], "height": rect.get("height")}))
                        continue
                    element_id = thumbnail.get_attribute("id")
                    latest_photos.append((rank, thumbnail, element_id if isinstance(element_id, str) else ""))
                except StaleElementReferenceException:
                    latest_rejections.append((rank, "thumbnail_unavailable", {}))
            diagnostics["thumbnails_found"] = len(thumbnails)
            if len(diagnostics["result_load_samples"]) < 60:
                diagnostics["result_load_samples"].append({"elements": len(thumbnails), "photos": len(latest_photos)})
            signature = tuple(element_id or str(thumbnail.id) for _, thumbnail, element_id in latest_photos)
            stable_polls = stable_polls + 1 if signature and signature == previous_signature else 1
            previous_signature = signature
            # Require several unchanged photo observations. An icon alone
            # never ends the wait; sparse results receive more settling time.
            return latest_photos if latest_photos and stable_polls >= (3 if len(latest_photos) >= count else 5) else None
        try:
            if self.stop_event.is_set():
                raise RuntimeError("사용자가 작업을 중지했습니다.")
            driver.get(search_url)
            try:
                WebDriverWait(driver, 20, poll_frequency=.4).until(settled_photos)
                diagnostics["results_stabilized"] = True
            except TimeoutException:
                diagnostics["results_stabilized"] = False
            eligible_thumbnails = latest_photos
        except RuntimeError:
            finish("cancelled")
            raise
        except WebDriverException as exc:
            reject(0, "search_navigation_error", error=str(exc))
            finish("search_error")
            raise
        for rank, reason, observed in latest_rejections:
            reject(rank, reason, **observed)
        diagnostics["eligible_thumbnails"] = len(eligible_thumbnails)
        if not eligible_thumbnails:
            reject(0, "search_results_missing")
            finish("no_results")
            self.log("Google 참고 이미지 검색 결과에 사진이 없어 다음 검색어 또는 생성 이미지로 진행합니다.")
            return []
        for rank, thumbnail, element_id in eligible_thumbnails[:60]:
            try:
                diagnostics["photo_candidates"].append({"rank": rank, "id": safe_text(element_id, 120),
                    "alt": safe_text(thumbnail.get_attribute("alt")),
                    "src": self._reference_diagnostic_url(thumbnail.get_attribute("src"))})
            except StaleElementReferenceException:
                diagnostics["photo_candidates"].append({"rank": rank, "id": safe_text(element_id, 120), "stale": True})
        seen: set[str] = set()
        candidate_started = time.monotonic()
        # The scan budget applies to photographs, not icons preceding them.
        for rank, thumbnail, element_id in eligible_thumbnails[:60]:
            if self.stop_event.is_set():
                finish("cancelled")
                raise RuntimeError("사용자가 작업을 중지했습니다.")
            if len(candidates) >= count:
                break
            if time.monotonic() - candidate_started >= diagnostics["candidate_time_budget_seconds"]:
                termination_reason = "time_budget_exhausted"
                reject(rank, termination_reason)
                self.log("Google 참고 이미지 후보 검토 90초 한도에 도달해 확보한 사진을 유지하고 다음 검색어로 진행합니다.")
                break
            diagnostics["scanned_count"] += 1
            stage, preview_observations = "thumbnail", []
            try:
                def preview_ready(d):
                    if self.stop_event.is_set():
                        raise RuntimeError("사용자가 작업을 중지했습니다.")
                    viable = []
                    preview_observations.clear()
                    preview_elements = d.find_elements(By.CSS_SELECTOR, "img.sFlh5c, img.iPVvYb, img.n3VNCb, div[role='dialog'] img")
                    selector_counts = d.execute_script("""return Object.fromEntries(
                      ['img.sFlh5c','img.iPVvYb','img.n3VNCb',"div[role='dialog'] img"].map(
                        selector=>[selector,document.querySelectorAll(selector).length]));""")
                    attempt_record["selector_counts"] = ({str(key): value for key, value in selector_counts.items()
                                                          if type(value) is int} if isinstance(selector_counts, dict) else {})
                    attempt_record["selector_counts"]["combined"] = len(preview_elements)
                    for element in preview_elements:
                        try:
                            displayed = element.is_displayed()
                            info = d.execute_script("""
                                const e=arguments[0], r=e.getBoundingClientRect();
                                return {width:e.naturalWidth,height:e.naturalHeight,
                                  display_width:r.width,display_height:r.height,
                                  url:e.currentSrc||e.src||'',complete:e.complete};
                            """, element) if displayed else None
                        except StaleElementReferenceException:
                            # Google's preview switches placeholders while the
                            # full image loads. Poll the new DOM, not a new topic.
                            continue
                        if not info:
                            continue
                        if len(preview_observations) < 12:
                            preview_observations.append({key: info.get(key) for key in
                                ("width", "height", "display_width", "display_height", "complete")})
                        if (info.get("complete") and info.get("width", 0) >= 640
                                and info.get("height", 0) >= 360
                                and info.get("display_width", 0) >= 320
                                and info.get("display_height", 0) >= 200
                                and info.get("url") not in seen):
                            viable.append((info["width"] * info["height"], element, info))
                    return max(viable, key=lambda value: value[0])[1:] if viable else None
                for attempt in range(1, 3):
                    attempt_record = {"rank": rank, "attempt": attempt, "selector_counts": {}}
                    diagnostics["preview_attempts"].append(attempt_record)
                    try:
                        if self.stop_event.is_set():
                            raise RuntimeError("사용자가 작업을 중지했습니다.")
                        # Resolve the selected result again after DOM changes.
                        if element_id:
                            current = driver.find_elements(By.ID, element_id)
                            if not current:
                                raise StaleElementReferenceException("Selected photo result was replaced")
                            thumbnail = current[0]
                        stage = "preview"
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", thumbnail)
                        preview, info = WebDriverWait(driver, 5, poll_frequency=.25).until(preview_ready)
                        links = driver.execute_script("""
                            const links=[];
                            for (const start of arguments) {
                              let e=start;
                              for (let level=0;e && level<4;level++,e=e.parentElement) {
                                if(e.matches('a[href]')) links.push(e.href);
                                links.push(...[...e.querySelectorAll('a[href]')].map(a=>a.href));
                              }
                            }
                            return [...new Set(links)];
                        """, preview, thumbnail)
                        attempt_record.update(status="ready", observations=list(preview_observations))
                        break
                    except (StaleElementReferenceException, TimeoutException) as exc:
                        attempt_record.update(status="retry" if attempt == 1 else "failed",
                                              error=safe_text(str(exc)), observations=list(preview_observations))
                        if attempt == 2:
                            raise
                        self.log(f"Google 참고 이미지 {rank}번 · 미리보기 로딩을 한 번 더 확인합니다.")
                consecutive_preview_failures = 0
                stage = "source_license"
                if self.stop_event.is_set():
                    raise RuntimeError("사용자가 작업을 중지했습니다.")
                source_url = self._reference_source_url(links or [])
                evidence = self._inspect_reference_license(driver, source_url, info["url"], english_only=True) if english_only else \
                    self._inspect_reference_license(driver, source_url, info["url"])
                if english_only and evidence.get("english_source_verified") is not True:
                    seen.add(info["url"])
                    reject(rank, "english_source_unverified", source_url=source_url,
                           source_language=evidence.get("source_language", ""))
                    self.log(f"Google 참고 이미지 {rank}번 · 영어 원문 페이지를 확인하지 못해 다음 후보를 확인합니다.")
                    continue
                if reuse_only and (evidence.get("license_verified") is not True
                        or evidence.get("commercial_use_allowed") is not True
                        or evidence.get("modification_allowed") is not True
                        or evidence.get("attribution_required") is not False
                        or evidence.get("share_alike") is not False):
                    seen.add(info["url"])
                    reject(rank, "reuse_rights_unverified", source_url=source_url,
                           license_verified=evidence.get("license_verified", False), license_url=evidence.get("license_url", ""),
                           attribution_required=evidence.get("attribution_required"))
                    self.log(f"Google 참고 이미지 {rank}번 · 출처 표시 없는 재사용 권한을 확인하지 못해 다음 후보를 확인합니다.")
                    continue
                stage = "capture"
                if self.stop_event.is_set():
                    raise RuntimeError("사용자가 작업을 중지했습니다.")
                old_style = preview.get_attribute("style") or ""
                try:
                    driver.execute_script("arguments[0].style.setProperty('object-fit','contain','important');", preview)
                    with Image.open(io.BytesIO(preview.screenshot_as_png)) as capture:
                        image = capture.convert("RGB")
                finally:
                    driver.execute_script("arguments[0].setAttribute('style',arguments[1]);", preview, old_style)
                if image.width < 320 or image.height < 200:
                    reject(rank, "capture_too_small", width=image.width, height=image.height)
                    continue
                capture_size = image.size
                image = self._gently_enhance_google_image(image)
                path = output_dir / f"google_reference_{len(candidates)+1:02d}.jpg"
                self._save_clean_jpeg(image, path)
                candidate = {
                    "path": str(path.resolve()), "provider": "google", "query": keyword,
                    "capture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "search_rank": rank, "source_url": source_url, "url": source_url,
                    "image_url": info["url"], "width": image.width, "height": image.height,
                    "source_width": info["width"], "source_height": info["height"],
                    "capture_width": capture_size[0], "capture_height": capture_size[1],
                    "capture_method": "image_element_screenshot", "upscaled": image.size != capture_size,
                    "vision_reviewed": False, "license_filter": "Creative Commons (not proof)",
                    "reuse_only": reuse_only,
                    "english_only": english_only,
                    "captured_at": datetime.now().isoformat(timespec="seconds"), **evidence,
                }
                candidates.append(candidate)
                seen.add(info["url"])
            except (WebDriverException, OSError, ValueError) as exc:
                reject(rank, "preview_timeout" if isinstance(exc, TimeoutException) and stage == "preview" else "candidate_error",
                       stage=stage, error=str(exc)[:500], preview_observations=preview_observations)
                self.log(f"Google 참고 이미지 후보 {rank}번 건너뜀: {type(exc).__name__} · {safe_text(str(exc).split('Stacktrace:', 1)[0].strip(), 200)}")
                if stage in {"thumbnail", "preview"} and isinstance(exc, (StaleElementReferenceException, TimeoutException)):
                    consecutive_preview_failures += 1
                    if consecutive_preview_failures >= diagnostics["preview_failure_limit"]:
                        termination_reason = "preview_unavailable"
                        reject(rank, termination_reason, consecutive_failures=consecutive_preview_failures)
                        self.log("Google 참고 이미지 미리보기가 3개 연속 열리지 않아 다음 검색어로 진행합니다.")
                        break
            except RuntimeError:
                if self.stop_event.is_set():
                    finish("cancelled")
                raise
        self._save_internal_image_source_history(candidates)
        if termination_reason:
            diagnostics["termination_reason"] = termination_reason
        finish(termination_reason or ("complete" if len(candidates) >= count else "partial" if candidates else "no_eligible_candidates"))
        return candidates

    @staticmethod
    def _naver_logged_in(driver) -> bool:
        return any(
            cookie.get("name") in {"NID_SES", "NID_AUT"}
            and bool(cookie.get("value"))
            for cookie in driver.get_cookies()
        )

    @staticmethod
    def _clean_blog_id(blog_id: str) -> str:
        value = (blog_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{2,50}", value):
            raise ValueError("네이버 블로그 ID를 영문, 숫자, 밑줄 형식으로 확인해 주세요.")
        return value

    def _require_naver_login_legacy(self, driver) -> None:
        driver.switch_to.default_content()
        driver.get("https://section.blog.naver.com/BlogHome.naver")
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        current_url = (driver.current_url or "").lower()
        if (
            not self._naver_logged_in(driver)
            or "nid.naver.com" in current_url
            or "login" in current_url
        ):
            raise RuntimeError(
                "네이버 로그인이 필요합니다. 먼저 '네이버 웨일 로그인 창 열기'를 눌러 "
                "전용 웨일 창에서 로그인하세요."
            )

    def _require_naver_login(self, driver):
        if self._has_naver_session(driver):
            return driver

        self.log(
            "자동화 전용 창에 로그인이 없어 일반 웨일의 기존 네이버 로그인을 확인합니다."
        )
        self._import_existing_naver_session(driver)
        if self._has_naver_session(driver):
            self.log("네이버 로그인 상태 확인 완료")
            return driver
        raise RuntimeError(
            "네이버 로그인 상태를 확인하지 못했습니다. 일반 웨일 창을 모두 닫은 뒤 "
            "다시 시도하거나 '네이버 웨일 로그인 창 열기'에서 한 번 로그인해 주세요."
        )

    @staticmethod
    def _find_editor_fields(driver):
        title_selector = (
            ".se-documentTitle [contenteditable='true'], "
            ".se-title-text [contenteditable='true'], "
            "[class*='documentTitle'] [contenteditable='true'], "
            ".se-title-text .se-text-paragraph, "
            ".se-documentTitle .se-text-paragraph"
        )
        body_selector = (
            ".se-section-text .se-component.se-text "
            ".se-text-paragraph[contenteditable='true'], "
            ".se-component.se-text .se-component-content "
            ".se-text-paragraph[contenteditable='true'], "
            ".se-component.se-text .se-text-paragraph[contenteditable='true'], "
            ".se-component.se-text .se-section-text .se-text-paragraph, "
            ".se-section-text .se-module-text .se-text-paragraph"
        )

        def visible(selector):
            return [
                element
                for element in driver.find_elements(By.CSS_SELECTOR, selector)
                if element.is_displayed()
            ]

        def search(depth: int):
            titles = visible(title_selector)
            bodies = []
            for element in visible(body_selector):
                try:
                    in_title = bool(
                        driver.execute_script(
                            "return Boolean(arguments[0].closest('.se-documentTitle,"
                            " .se-title-text, [class*=documentTitle]'));",
                            element,
                        )
                    )
                except Exception:
                    in_title = "documenttitle" in (
                        element.get_attribute("class") or ""
                    ).lower()
                if not in_title:
                    bodies.append(element)
            if titles and bodies:
                return titles[0], bodies[-1]
            if depth <= 0:
                return None
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
            for frame in frames:
                try:
                    driver.switch_to.frame(frame)
                    found = search(depth - 1)
                    if found:
                        return found
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.parent_frame()
                    except Exception:
                        driver.switch_to.default_content()
            return None

        driver.switch_to.default_content()
        return search(2)

    def _open_writer(self, driver, blog_id: str, timeout: int = 45):
        blog_id = self._clean_blog_id(blog_id)
        self._require_naver_login(driver)
        driver.switch_to.default_content()
        driver.get(f"https://blog.naver.com/{blog_id}/postwrite")
        try:
            fields = WebDriverWait(driver, timeout).until(
                lambda d: self._find_editor_fields(d)
            )
        except TimeoutException as exc:
            driver.switch_to.default_content()
            current_url = (driver.current_url or "").lower()
            if "nid.naver.com" in current_url or "login" in current_url:
                raise RuntimeError(
                    "네이버 로그인 상태가 만료되었습니다. 웨일 로그인 창에서 다시 로그인해 주세요."
                ) from exc
            raise RuntimeError(
                "네이버 스마트에디터를 찾지 못했습니다. 대상 블로그 ID와 "
                "글쓰기 권한을 확인해 주세요. 발행은 실행하지 않았습니다."
            ) from exc
        self._handle_writer_recovery_prompt(driver)
        return self._find_editor_fields(driver) or fields

    @staticmethod
    def _activate_writer_tab(driver) -> None:
        """Activate only the writer already selected by this WebDriver session."""
        url = urllib.parse.urlparse(str(driver.current_url or ""))
        if (url.scheme not in {"http", "https"} or url.hostname != "blog.naver.com"
                or not (re.fullmatch(r"/[^/]+/postwrite/?", url.path, re.I)
                        or url.path.lower() == "/postwriteform.naver")):
            return
        try:
            driver.execute_cdp_cmd("Page.bringToFront", {})
        except (AttributeError, WebDriverException):
            # A provider without CDP can still select its existing window. Do
            # not enumerate or switch to unrelated tabs and never force clicks.
            driver.switch_to.window(driver.current_window_handle)

    def _handle_writer_recovery_prompt(self, driver) -> None:
        """Decline only Naver's observed recovery prompt when opening a new article."""
        self._activate_writer_tab(driver)
        def visible_dialogs():
            return [element for element in driver.find_elements(By.CSS_SELECTOR, ".se-popup, [role='dialog']")
                    if element.is_displayed()]
        dialogs = self._find_across_frames(driver, visible_dialogs) or []
        if not dialogs:
            return
        if len(dialogs) != 1:
            raise RuntimeError("글쓰기 화면에 여러 안내창이 있어 자동으로 닫지 않았습니다.")
        dialog = dialogs[0]
        text = self._normalized_text(dialog.text)
        if "작성 중인 글이 있습니다" not in text or "이어서 작성" not in text:
            raise RuntimeError("알 수 없는 글쓰기 안내창이 있어 자동으로 닫지 않았습니다.")
        candidates = [button for button in dialog.find_elements(By.CSS_SELECTOR, ".se-popup-button-cancel")
                      if button.is_displayed() and button.is_enabled() and self._normalized_text(button.text) == "취소"]
        if len(candidates) != 1:
            raise RuntimeError("이전 작성 내용 불러오기 취소 버튼을 고유하게 확인하지 못했습니다.")
        self.log("이전 작성 내용 복원 안내를 확인했습니다. 현재 편집기에서 불러오기 취소를 누릅니다.")
        candidates[0].click()
        def dismissed(_driver):
            try:
                return not dialog.is_displayed()
            except StaleElementReferenceException:
                return True
        try:
            WebDriverWait(driver, 8).until(dismissed)
        except TimeoutException as exc:
            raise RuntimeError(
                "이전 작성 내용 불러오기 취소 버튼을 눌렀지만 안내창이 닫히지 않았습니다. "
                "편집기 내용을 유지했으며 발행·저장은 실행하지 않았습니다."
            ) from exc
        self.log("새 글 작성을 위해 이전 작성 내용 불러오기를 취소했습니다. 저장된 임시글 삭제는 실행하지 않았습니다.")

    def open_blog_writer(self, blog_id: str):
        driver = self._driver()
        self._open_writer(driver, blog_id, timeout=35)
        self.log(
            "네이버 웨일에서 새 글쓰기 화면을 열었습니다. "
            "자동 입력하려면 프로그램의 '웨일로 입력·임시저장'을 누르세요."
        )

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
        response_selector = "[data-message-author-role='assistant']"
        previous_responses = driver.find_elements(By.CSS_SELECTOR, response_selector)
        previous_count = len(previous_responses)
        previous_last_id = (
            previous_responses[-1].get_attribute("data-message-id")
            if previous_responses
            else ""
        )
        previous_last_text = (
            previous_responses[-1].text.strip() if previous_responses else ""
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
            responses = driver.find_elements(By.CSS_SELECTOR, response_selector)
            latest = responses[-1] if responses else None
            text = latest.text.strip() if latest else ""
            latest_id = latest.get_attribute("data-message-id") if latest else ""
            is_new_response = bool(
                latest
                and (
                    len(responses) > previous_count
                    or (latest_id and latest_id != previous_last_id)
                    or (text and text != previous_last_text and len(responses) >= previous_count)
                )
            )
            if not is_new_response:
                time.sleep(1)
                continue
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
        lines = (text or "").lstrip("\ufeff").splitlines()
        first_index = next(
            (index for index, line in enumerate(lines) if line.strip()),
            None,
        )
        if first_index is None:
            return "블로그 글", ""
        title = re.sub(r"^\s*#+\s*", "", lines[first_index])
        title = title.replace("**", "").strip() or "블로그 글"
        body = "\n".join(lines[first_index + 1 :]).strip()
        return title[:100], body

    @staticmethod
    def _body_segments_for_images(body: str, image_count: int) -> list[str]:
        image_count = max(0, image_count)
        marker = r"\[\s*사진\s*삽입\s*위치\s*\]"
        marked = re.split(marker, body or "", flags=re.IGNORECASE)
        if len(marked) > 1:
            segments = [segment.strip() for segment in marked]
        else:
            paragraphs = [
                paragraph.strip()
                for paragraph in re.split(r"\n\s*\n+", body or "")
                if paragraph.strip()
            ]
            if not paragraphs:
                paragraphs = [(body or "").strip()]
            segment_count = image_count + 1
            segments = [""] * segment_count
            for index, paragraph in enumerate(paragraphs):
                slot = min(
                    segment_count - 1,
                    index * segment_count // max(1, len(paragraphs)),
                )
                segments[slot] = (
                    f"{segments[slot]}\n\n{paragraph}".strip()
                    if segments[slot]
                    else paragraph
                )
        wanted = image_count + 1
        if len(segments) < wanted:
            segments.extend([""] * (wanted - len(segments)))
        elif len(segments) > wanted:
            segments = segments[: image_count] + [
                "\n\n".join(segments[image_count:]).strip()
            ]
        return segments

    @staticmethod
    def _editor_text(editor) -> str:
        try:
            # SmartEditor ONE keeps its placeholder in a sibling span. Only
            # actual text nodes represent saved editor content.
            nodes = editor.find_elements(By.CSS_SELECTOR, ".__se-node")
            if nodes:
                return "\n".join(
                    (
                        node.get_attribute("innerText")
                        or node.get_attribute("textContent")
                        or ""
                    )
                    for node in nodes
                ).replace("\u200b", "").strip()
        except Exception:
            pass
        return (
            editor.get_attribute("innerText")
            or editor.get_attribute("textContent")
            or editor.get_attribute("value")
            or editor.text
            or ""
        ).replace("\u200b", "").strip()

    @staticmethod
    def _normalized_text(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").replace("\u200b", "")).strip()

    @classmethod
    def _editor_contains_text(cls, editor, expected: str) -> bool:
        wanted = cls._normalized_text(expected)
        if not wanted:
            return True
        actual = cls._normalized_text(cls._editor_text(editor))
        if len(wanted) <= 80:
            return wanted in actual
        return wanted[:40] in actual and wanted[-40:] in actual

    @classmethod
    def _editor_has_existing_content(cls, editor) -> bool:
        current = cls._normalized_text(cls._editor_text(editor))
        placeholders = {
            "",
            "제목",
            "제목을 입력하세요",
            "제목을 입력해주세요",
            "본문",
            "본문을 입력하세요",
            "본문을 입력해주세요",
            "내용을 입력하세요",
            "내용을 입력해주세요",
        }
        return current not in placeholders

    @staticmethod
    def _find_across_frames(driver, finder, depth: int = 2):
        def search(remaining: int):
            value = finder()
            if value:
                return value
            if remaining <= 0:
                return None
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
            for frame in frames:
                try:
                    driver.switch_to.frame(frame)
                    value = search(remaining - 1)
                    if value:
                        return value
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.parent_frame()
                    except Exception:
                        driver.switch_to.default_content()
            return None

        driver.switch_to.default_content()
        return search(depth)

    @classmethod
    def _image_component_count(cls, driver) -> int:
        fields = cls._find_editor_fields(driver)
        if not fields:
            return 0
        components = driver.find_elements(
            By.CSS_SELECTOR,
            ".se-component.se-image, .se-component[data-name='image']",
        )
        if components:
            return len(components)
        return len(driver.find_elements(By.CSS_SELECTOR, ".se-image-resource"))

    @classmethod
    def _find_image_inputs(cls, driver, allow_empty: bool = False):
        def finder():
            inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            image_inputs = []
            fallback_inputs = []
            for element in inputs:
                accept = (element.get_attribute("accept") or "").lower()
                if "image" in accept or any(
                    extension in accept
                    for extension in (".jpg", ".jpeg", ".png", ".webp", ".gif")
                ):
                    image_inputs.append(element)
                elif not accept:
                    fallback_inputs.append(element)
            return image_inputs or (fallback_inputs if allow_empty else [])

        return cls._find_across_frames(driver, finder)

    @classmethod
    def _open_photo_tool(cls, driver) -> bool:
        def finder():
            buttons = driver.find_elements(
                By.XPATH,
                "//*[self::button or self::a]"
                "["
                "@data-name='image' "
                "or contains(concat(' ',normalize-space(@class),' '),"
                "' se-image-toolbar-button ') "
                "or contains(@aria-label,'사진') "
                "or contains(normalize-space(.),'사진 추가') "
                "or normalize-space(.)='사진'"
                "]",
            )
            return next(
                (button for button in buttons if button.is_displayed()),
                None,
            )

        button = cls._find_across_frames(driver, finder)
        if not button:
            return False
        driver.execute_script("arguments[0].click()", button)
        return True

    def _upload_blog_images(self, driver, image_paths: list[str]) -> None:
        existing = [str(Path(path).resolve()) for path in image_paths if Path(path).is_file()]
        if not existing:
            return
        before_images = self._image_component_count(driver)
        inputs = self._find_image_inputs(driver)
        if not inputs:
            self._open_photo_tool(driver)
            try:
                inputs = WebDriverWait(driver, 8).until(
                    lambda d: self._find_image_inputs(d, allow_empty=True)
                )
            except TimeoutException as exc:
                raise RuntimeError(
                    "네이버 사진 첨부 도구를 찾지 못했습니다. 글은 임시저장하지 않고 화면에 둡니다."
                ) from exc

        upload = inputs[-1]
        multiple = upload.get_attribute("multiple") is not None
        if multiple:
            upload.send_keys("\n".join(existing))
        else:
            for index, path in enumerate(existing):
                if index:
                    inputs = self._find_image_inputs(driver)
                    if not inputs:
                        self._open_photo_tool(driver)
                        inputs = WebDriverWait(driver, 8).until(
                            lambda d: self._find_image_inputs(d, allow_empty=True)
                        )
                    upload = inputs[-1]
                upload.send_keys(path)
                WebDriverWait(driver, 45).until(
                    lambda d, expected=before_images + index + 1: (
                        self._image_component_count(d) >= expected
                    )
                )

        expected_count = before_images + len(existing)
        try:
            WebDriverWait(driver, min(120, 30 + len(existing) * 8)).until(
                lambda d: self._image_component_count(d) >= expected_count
            )
        except TimeoutException as exc:
            actual_count = self._image_component_count(driver)
            raise RuntimeError(
                f"사진 {len(existing)}개 중 {max(0, actual_count - before_images)}개만 "
                "편집기에서 확인되어 임시저장을 중단했습니다."
            ) from exc
        self.log(f"오늘 캡처 사진 {len(existing)}개 첨부를 확인했습니다.")

    @classmethod
    def _focus_body_image_position(
        cls,
        driver,
        image_index: int,
        image_count: int,
    ) -> bool:
        """Place the SmartEditor caret at an evenly spaced body paragraph."""

        def finder():
            paragraphs = driver.find_elements(
                By.CSS_SELECTOR,
                ".se-component.se-text .se-text-paragraph, "
                ".se-component[data-name='text'] .se-text-paragraph",
            )
            return [
                paragraph
                for paragraph in paragraphs
                if paragraph.is_displayed()
                and not driver.execute_script(
                    "return Boolean(arguments[0].closest("
                    "'.se-documentTitle, .se-title-text, "
                    "[class*=documentTitle]'));",
                    paragraph,
                )
            ]

        paragraphs = cls._find_across_frames(driver, finder) or []
        if paragraphs:
            slot = min(
                len(paragraphs) - 1,
                max(
                    0,
                    ((image_index + 1) * len(paragraphs))
                    // max(1, image_count + 1),
                ),
            )
            target = paragraphs[slot]
            driver.execute_script(
                """
                const target = arguments[0];
                target.scrollIntoView({block:'center', inline:'nearest'});
                target.click();
                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(target);
                range.collapse(false);
                selection.removeAllRanges();
                selection.addRange(range);
                target.dispatchEvent(new MouseEvent('mouseup', {
                  bubbles:true, cancelable:true, view:window
                }));
                """,
                target,
            )
            return True
        try:
            driver.execute_script(
                "SmartEditor.getEditor('blogpc001').focusFirstText();"
            )
            return True
        except Exception:
            return False

    @classmethod
    def _redistribute_smarteditor_images(
        cls,
        driver,
        segments: list[str],
        image_count: int,
    ) -> None:
        result = driver.execute_async_script(
            """
            const segments = arguments[0];
            const wantedImages = arguments[1];
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const instance = SmartEditor.getEditor('blogpc001');
                const data = instance.getDocumentData();
                const components = data.document.components || [];
                const textComponents = components.filter(
                  item => item['@ctype'] === 'text'
                );
                const textComponent = textComponents[0];
                const images = components.filter(
                  item => item['@ctype'] === 'image'
                ).slice(-wantedImages);
                if (!textComponent) throw new Error('text component missing');
                if (images.length !== wantedImages) {
                  throw new Error(
                    `image components ${images.length}/${wantedImages}`
                  );
                }
                const firstBodyIndex = components.findIndex(
                  item => textComponents.includes(item) || images.includes(item)
                );
                const replaced = new Set([...textComponents, ...images]);
                const before = components.slice(0, firstBodyIndex)
                  .filter(item => !replaced.has(item));
                const after = components.slice(firstBodyIndex)
                  .filter(item => !replaced.has(item));
                const makeId = () => 'SE-' + crypto.randomUUID();
                const templateParagraph = textComponent.value[0];
                const nodeStyle = (
                  templateParagraph.nodes[0].style ||
                  {'@ctype':'nodeStyle'}
                );
                const paragraphStyle = (
                  templateParagraph.style ||
                  {align:'left','@ctype':'paragraphStyle'}
                );
                const makeText = value => {
                  const component = JSON.parse(JSON.stringify(textComponent));
                  component.id = makeId();
                  const lines = String(value || '').split(/\\r?\\n/);
                  component.value = (lines.length ? lines : ['']).map(line => ({
                    id: makeId(),
                    nodes: [{
                      id: makeId(),
                      value: line || '\\u200b',
                      style: {...nodeStyle},
                      '@ctype':'textNode'
                    }],
                    style: {...paragraphStyle},
                    '@ctype':'paragraph'
                  }));
                  return component;
                };
                const arranged = [];
                for (let index = 0; index < wantedImages + 1; index++) {
                  arranged.push(makeText(segments[index] || ''));
                  if (index < wantedImages) arranged.push(images[index]);
                }
                data.document.components = [...before, ...arranged, ...after];
                await Promise.resolve(instance.setDocumentData(data));
                const verified = instance.getDocumentData();
                const types = (verified.document.components || [])
                  .map(item => item['@ctype']);
                const verifiedImages = types.filter(
                  type => type === 'image'
                ).length;
                done({
                  ok: verifiedImages >= wantedImages,
                  types,
                  images: verifiedImages
                });
              } catch (error) {
                done({ok:false, error:String(error)});
              }
            })();
            """,
            segments,
            image_count,
        )
        if not result or not result.get("ok"):
            detail = result.get("error", "") if isinstance(result, dict) else ""
            raise RuntimeError(
                "네이버 본문 문단 사이에 사진을 재배치하지 못했습니다. "
                f"{detail}"
            )

    @staticmethod
    def _copy_image_to_windows_clipboard(image_path: str) -> None:
        import win32clipboard
        import win32con

        with Image.open(image_path) as source:
            converted = source.convert("RGB")
            output = io.BytesIO()
            converted.save(output, "BMP")
            dib = output.getvalue()[14:]
        last_error = None
        for _attempt in range(10):
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.15)
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        raise RuntimeError(
            f"Windows 이미지 클립보드를 열지 못했습니다: {last_error}"
        )

    def _paste_blog_image_at_position(
        self,
        driver,
        image_path: str,
        image_index: int,
        image_count: int,
    ) -> None:
        before_images = self._image_component_count(driver)
        if not self._focus_body_image_position(
            driver,
            image_index,
            image_count,
        ):
            raise RuntimeError("사진을 붙여 넣을 본문 문단을 선택하지 못했습니다.")
        self._copy_image_to_windows_clipboard(image_path)
        ActionChains(driver).key_down(self.EDITOR_MODIFIER).send_keys("v").key_up(
            self.EDITOR_MODIFIER
        ).perform()
        try:
            WebDriverWait(driver, 45).until(
                lambda d: self._image_component_count(d) > before_images
            )
        except TimeoutException as exc:
            raise RuntimeError(
                f"폴더 사진을 본문 {image_index + 1}번째 위치에 "
                "붙여 넣지 못했습니다."
            ) from exc

    def _insert_body_with_images(
        self,
        driver,
        body_box,
        body: str,
        image_paths: list[str],
    ) -> None:
        existing = [
            str(Path(path).resolve())
            for path in image_paths
            if Path(path).is_file()
        ]
        if not existing:
            self._replace_editor_text(driver, body_box, body)
            return
        try:
            modern_editor = bool(
                driver.execute_script(
                    "return Boolean(window.SmartEditor && "
                    "SmartEditor.getEditor('blogpc001'));"
                )
            )
        except Exception:
            modern_editor = False
        if modern_editor:
            # Upload one image at a time. After each upload, rebuild the live
            # document as text/image/text so the photo toolbar is available
            # again and the final images stay between the intended paragraphs.
            segments = self._body_segments_for_images(body, len(existing))
            combined_body = "\n\n".join(
                segment for segment in segments if segment
            )
            self._replace_editor_text(driver, body_box, combined_body)
            try:
                for index, image_path in enumerate(existing):
                    if self.stop_event.is_set():
                        raise RuntimeError("사용자가 전체 자동화를 중지했습니다.")
                    if index:
                        self._focus_body_image_position(
                            driver,
                            index,
                            len(existing),
                        )
                    self._upload_blog_images(driver, [image_path])
                    progressive_segments = self._body_segments_for_images(
                        body,
                        index + 1,
                    )
                    self._redistribute_smarteditor_images(
                        driver,
                        progressive_segments,
                        index + 1,
                    )
            except Exception as primary_error:
                inserted = min(
                    len(existing),
                    self._image_component_count(driver),
                )
                self.log(
                    "기본 사진 첨부가 중단되어 폴더 사진 붙여넣기 "
                    f"백업으로 전환합니다: {primary_error}"
                )
                for index in range(inserted, len(existing)):
                    if self.stop_event.is_set():
                        raise RuntimeError("사용자가 전체 자동화를 중지했습니다.")
                    self._paste_blog_image_at_position(
                        driver,
                        existing[index],
                        index,
                        len(existing),
                    )
                    fallback_segments = self._body_segments_for_images(
                        body,
                        index + 1,
                    )
                    self._redistribute_smarteditor_images(
                        driver,
                        fallback_segments,
                        index + 1,
                    )
                if self._image_component_count(driver) < len(existing):
                    raise RuntimeError(
                        f"사진 {len(existing)}장 중 일부를 본문에 넣지 못했습니다."
                    )
                self.log(
                    f"폴더 사진 붙여넣기 백업으로 사진 "
                    f"{len(existing) - inserted}장을 추가했습니다."
                )
            self.log(
                f"새 스마트에디터 본문 문단 사이에 사진 "
                f"{len(existing)}장을 순서대로 배치했습니다."
            )
            return
        segments = self._body_segments_for_images(body, len(existing))
        self._replace_editor_text(driver, body_box, segments[0])
        for index, image_path in enumerate(existing):
            if self.stop_event.is_set():
                raise RuntimeError("사용자가 전체 자동화를 중지했습니다.")
            body_box.click()
            body_box.send_keys(Keys.END)
            self._upload_blog_images(driver, [image_path])
            fields = WebDriverWait(driver, 20).until(
                lambda d: self._find_editor_fields(d)
            )
            body_box = fields[1]
            following_text = segments[index + 1]
            if following_text:
                self._replace_editor_text(
                    driver,
                    body_box,
                    following_text,
                )
        self.log(
            f"본문 문단 사이에 사진 {len(existing)}장을 순서대로 배치했습니다."
        )

    @classmethod
    def _find_draft_buttons(cls, driver):
        def finder():
            candidates = driver.find_elements(
                By.XPATH,
                "//*[self::button or self::a]"
                "[contains(normalize-space(.),'임시저장') "
                "or normalize-space(.)='저장' "
                "or contains(@class,'save_btn')]",
            )
            safe = []
            for button in candidates:
                text = cls._normalized_text(button.text)
                classes = (button.get_attribute("class") or "").lower()
                current_save_button = (
                    text == "저장"
                    and "save_btn" in classes
                    and "save_count" not in classes
                )
                if (
                    not button.is_displayed()
                    or not button.is_enabled()
                    or "발행" in text
                    or not (
                        text.startswith("임시저장")
                        or current_save_button
                    )
                ):
                    continue
                try:
                    in_dialog = bool(
                        driver.execute_script(
                            "return Boolean(arguments[0].closest("
                            "'[role=dialog], [class*=modal], [class*=popup]'));",
                            button,
                        )
                    )
                except Exception:
                    in_dialog = False
                if not in_dialog:
                    safe.append(button)
            return safe

        return cls._find_across_frames(driver, finder)

    @classmethod
    def _draft_save_confirmed(cls, driver) -> bool:
        def finder():
            try:
                probe = bool(
                    driver.execute_script(
                        """
                        if (window.__pictureCleanerDraftSaved) return true;
                        const recent = performance.getEntriesByType('resource')
                          .filter(item => item.startTime >=
                            (window.__pictureCleanerSaveStarted || Number.MAX_VALUE));
                        return recent.some(item =>
                          /(temporary|autosave|draft|postwrite|save)/i.test(item.name));
                        """
                    )
                )
                if probe:
                    return True
            except Exception:
                pass
            notices = driver.find_elements(
                By.XPATH,
                "//*[contains(normalize-space(.),'저장되었습니다') "
                "or contains(normalize-space(.),'임시저장 완료') "
                "or normalize-space(.)='저장됨']",
            )
            return any(element.is_displayed() for element in notices)

        return bool(cls._find_across_frames(driver, finder))

    def save_naver_draft(
        self, blog_id: str, generated_text: str, image_paths: list[str]
    ) -> None:
        if not (generated_text or "").strip():
            raise ValueError("네이버에 입력할 블로그 내용이 없습니다.")
        driver = self._driver()
        title, body = self._split_title_body(generated_text)
        title_box, body_box = self._open_writer(driver, blog_id)
        if self._editor_has_existing_content(title_box) or self._editor_has_existing_content(
            body_box
        ):
            raise RuntimeError(
                "네이버 글쓰기 화면에 기존 작성 내용이 있어 덮어쓰지 않았습니다. "
                "웨일에서 기존 글을 보관하거나 새 글 빈 화면으로 만든 뒤 다시 실행해 주세요."
            )
        self._replace_editor_text(driver, title_box, title)
        self._insert_body_with_images(
            driver,
            body_box,
            body,
            image_paths,
        )
        self.log("생성된 제목과 본문을 네이버 글쓰기에 입력했습니다.")

        try:
            draft_buttons = WebDriverWait(driver, 15).until(
                lambda d: self._find_draft_buttons(d)
            )
        except TimeoutException as exc:
            raise RuntimeError(
                "네이버 임시저장 버튼을 찾지 못했습니다. "
                "발행하지 않고 입력된 화면을 그대로 유지합니다."
            ) from exc
        button = draft_buttons[0]
        driver.execute_script(
            """
            window.__pictureCleanerDraftSaved = false;
            window.__pictureCleanerSaveStarted = performance.now();
            const observer = new MutationObserver(() => {
              const text = document.body ? document.body.innerText : '';
              if (/저장되었습니다|임시저장 완료|저장됨/.test(text)) {
                window.__pictureCleanerDraftSaved = true;
                observer.disconnect();
              }
            });
            observer.observe(document.documentElement, {
              childList: true, subtree: true, characterData: true
            });
            """
        )
        driver.execute_script("arguments[0].click()", button)
        try:
            confirmed = WebDriverWait(driver, 15).until(
                lambda d: self._draft_save_confirmed(d)
            )
        except TimeoutException:
            confirmed = False

        current_url = (driver.current_url or "").lower()
        if "postview" in current_url:
            raise RuntimeError(
                "예상하지 못한 페이지 이동이 감지되었습니다. 웨일 화면에서 게시 상태를 즉시 확인해 주세요."
            )
        if confirmed:
            self.log("네이버 임시저장 완료를 확인했습니다. 발행 버튼은 누르지 않았습니다.")
        else:
            self.log(
                "임시저장 버튼은 눌렀지만 완료 안내를 자동 확인하지 못했습니다. "
                "발행 버튼은 누르지 않았으며 웨일 화면은 그대로 유지했습니다."
            )

    @staticmethod
    def _validate_publish_article(article: dict) -> tuple[str, list[str], list[dict]]:
        title = str(article.get("title", "")).strip()
        paragraphs = article.get("paragraphs")
        images = article.get("images")
        if not title or len(title) > 100 or "\n" in title:
            raise ValueError("발행 제목은 1~100자의 한 줄이어야 합니다.")
        if not isinstance(paragraphs, list) or len(paragraphs) != 8:
            raise ValueError("발행 본문에는 정확히 8개 문단이 필요합니다.")
        if any(not isinstance(p, str) or not p.strip() for p in paragraphs):
            raise ValueError("8개 본문 구역 각각은 비어 있지 않은 문자열이어야 합니다.")
        paragraphs = [p.strip() for p in paragraphs]
        public_text = "\n".join([title, *paragraphs])
        if "*" in public_text or re.search(r"(?m)^\s*#{1,6}\s|!\[|\[[^\]\n]+\]\(", public_text):
            raise ValueError("게시 본문에는 별표나 Markdown 표기를 넣을 수 없습니다. 기본 편집기 서식을 사용하세요.")
        if re.search(r"https?://", public_text, re.I):
            raise ValueError("출처 URL은 비공개 검수 기록에만 보관해야 합니다.")
        bold_terms = article.get("bold_terms", [])
        if not isinstance(bold_terms, list) or any(not isinstance(term, str) or "\n" in term for term in bold_terms):
            raise ValueError("강조 단어 목록은 줄바꿈 없는 문자열 목록이어야 합니다.")
        reviews = article.get("reviews")
        review_flags = ("approved", "facts_verified", "sources_verified", "search_intent_satisfied", "natural_korean")
        if article.get("ready_to_publish") is not True or not isinstance(reviews, list) or not 1 <= len(reviews) <= 4:
            raise ValueError("선택한 CLI 검수 단계를 통과한 발행 준비 결과가 필요합니다.")
        final_reviews = article.get("final_reviews", [])
        if not isinstance(final_reviews, list):
            raise ValueError("최종 교차 검수 기록이 올바르지 않습니다.")
        for stage in [*reviews, *final_reviews]:
            review = stage.get("review") if isinstance(stage, dict) else None
            if (not isinstance(review, dict) or not stage.get("provider")
                    or any(review.get(flag) is not True for flag in review_flags)
                    or review.get("issues")):
                raise ValueError("본문 사실·출처·자연스러운 문장 검수에서 통과하지 못한 항목이 있습니다.")
        content_hash = hashlib.sha256(json.dumps(
            {"title": title, "paragraphs": paragraphs}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()
        if article.get("reviewed_content_sha256") != content_hash:
            raise ValueError("CLI 검수 이후 제목 또는 본문이 변경되었거나 검수 해시가 없습니다.")
        if not isinstance(images, list) or not 6 <= len(images) <= 16:
            raise ValueError("검수한 이미지 6~16장과 첫 생성 표지 사진이 필요합니다.")
        generated = [item for item in images if isinstance(item, dict) and item.get("provider") != "google"]
        if not 1 <= len(generated) <= 6:
            raise ValueError("생성 표지 사진을 포함한 생성 이미지 1~6장이 필요합니다. 부족분은 권리 확인된 참고 이미지로 보충하세요.")
        if sum(isinstance(item, dict) and item.get("provider") == "google" for item in images) > 10:
            raise ValueError("Google 참고 이미지는 최대 10장까지 배치할 수 있습니다.")
        checked: list[dict] = []
        seen: set[str] = set()
        for item in images:
            if not isinstance(item, dict):
                raise ValueError("이미지 배치 정보가 올바르지 않습니다.")
            position = item.get("paragraph_index")
            if type(position) is not int or not 0 <= position <= 7:
                raise ValueError("각 사진의 paragraph_index는 0~7 정수여야 합니다.")
            if item.get("requires_final_semantic_review", False) is not False:
                raise ValueError("최종 본문 문맥에 대한 이미지 검수가 아직 완료되지 않았습니다.")
            if "reviewed_paragraph_sha256" in item:
                section_hash = hashlib.sha256(paragraphs[position].encode("utf-8")).hexdigest()
                if item["reviewed_paragraph_sha256"] != section_hash:
                    raise ValueError("시각 검수 이후 사진의 배치 구역 또는 해당 본문 내용이 변경되었습니다. 다시 검수해야 합니다.")
            path = Path(str(item.get("path", ""))).resolve()
            if not path.is_file():
                raise ValueError(f"발행 이미지 파일이 없습니다: {path}")
            with Image.open(path) as image:
                image.verify()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if item.get("sha256") != digest:
                raise ValueError("시각 검수 이후 이미지 파일이 변경되었거나 검수 해시가 없습니다.")
            if digest in seen:
                raise ValueError("같은 이미지가 중복되어 발행을 중단했습니다.")
            seen.add(digest)
            if item.get("provider") == "google":
                if item.get("license_verified") is not True or not item.get("license_url"):
                    raise ValueError("Google 참고 이미지의 원문 라이선스가 확인되지 않았습니다.")
                if item.get("commercial_use_allowed") is not True or item.get("modification_allowed") is not True:
                    raise ValueError("Google 참고 이미지의 재사용 및 크기 조정 권한이 확인되지 않았습니다.")
                if item.get("share_alike"):
                    raise ValueError("동일조건변경허락 참고 이미지는 자동 발행 대상에서 제외합니다.")
                if item.get("attribution_required"):
                    raise ValueError("공개 출처 표시가 필요한 참고 이미지는 이 글의 자동 발행 대상에서 제외합니다.")
                license_parts = urllib.parse.urlparse(str(item["license_url"]))
                cc_public_domain = (license_parts.scheme == "https" and license_parts.hostname == "creativecommons.org"
                                    and license_parts.path.rstrip("/") in {"/publicdomain/zero/1.0", "/publicdomain/mark/1.0"})
                if (not (cc_public_domain or NaverAutomation._commons_public_domain_license_verified(item))
                        or item.get("attribution_required") is not False):
                    raise ValueError("참고 이미지에는 출처 표시가 필요 없는 CC0 또는 공개 도메인 라이선스 확인이 필요합니다.")
            reviews = item.get("reviews")
            if item.get("approved") is not True or not isinstance(reviews, list) or not reviews or any(
                not isinstance(review, dict) or review.get("approved") is not True for review in reviews
            ):
                raise ValueError("모든 발행 이미지에는 통과한 CLI 시각 검수 기록이 필요합니다.")
            if item.get("provider") == "google" and (item.get("caption_text") or item.get("caption_applied")):
                caption = item.get("caption_text")
                if (not isinstance(caption, str) or not 1 <= len(caption) <= 10
                        or not re.search(r"[가-힣]", caption) or re.search(r"https?://|www\.|[\r\n]", caption, re.I)
                        or item.get("caption_applied") is not True or item.get("original_text_free") is not True
                        or item.get("caption_placement") != "top" or item.get("caption_layout") != "separate_band"
                        or type(item.get("caption_band_height")) is not int or item["caption_band_height"] <= 0
                        or item.get("cover_headline") or item.get("cover_text_applied")):
                    raise ValueError("Google 참고 이미지에는 원본 글자 없음 검수와 상단 별도 영역의 10자 이내 한글 캡션이 필요합니다.")
                for review in reviews:
                    if (any(review.get(flag) is not True for flag in ("caption_exact", "caption_legible", "no_other_text"))
                            or review.get("text_free") is not False
                            or not isinstance(review.get("detected_text"), str)
                            or re.sub(r"\s+", "", review["detected_text"]) != re.sub(r"\s+", "", caption)):
                        raise ValueError("Google 참고 이미지에 추가한 캡션의 정확성·가독성·다른 글자 없음 검수가 필요합니다.")
            checked.append({**item, "path": str(path), "sha256": digest})
        # Stable sort retains caller order when more than one picture shares a paragraph.
        checked.sort(key=lambda item: item["paragraph_index"])
        if checked[0].get("provider") == "google" or checked[0]["paragraph_index"] != 0:
            raise ValueError("첫 사진은 첫 구역에 배치한 검수된 생성 표지 사진이어야 합니다.")
        if article.get("image_policy") == IMAGE_POLICY:
            expected_headline = cover_headline(article.get("cover_headline", ""))
            if (not checked or checked[0].get("provider") == "google" or checked[0]["paragraph_index"] != 0
                    or checked[0].get("cover_headline") != expected_headline or checked[0].get("cover_text_applied") is not True
                    or checked[0].get("cover_aspect_ratio") != "1:1" or checked[0].get("width") != checked[0].get("height")
                    or checked[0].get("cover_text_color") not in {"#8CE88C", "#EF3340"}):
                raise ValueError("첫 생성 사진의 한글 후킹 문구를 확인하지 못했습니다.")
            for index, item in enumerate(checked):
                if item.get("provider") != "google" and item.get("image_policy") != IMAGE_POLICY:
                    raise ValueError("새 이미지 생성 규칙과 다른 사진이 포함되어 있습니다.")
                for review in item["reviews"]:
                    if index == 0:
                        if (any(review.get(key) is not True for key in ("cover_text_exact", "cover_text_legible", "no_other_text",
                                "square_1_to_1", "no_human_face", "bold_gothic", "text_shadow_visible", "approved_text_color"))
                                or review.get("text_free") is not False
                                or re.sub(r"\s+", "", str(review.get("detected_text", ""))) != re.sub(r"\s+", "", expected_headline)):
                            raise ValueError("첫 사진의 한글 문구 정확성 검수가 필요합니다.")
                    elif item.get("provider") == "google" and item.get("caption_applied") is True:
                        # The source image was text-free; the separately added
                        # caption was checked against the final exported image above.
                        continue
                    elif item.get("cover_headline") or item.get("cover_text_applied") or review.get("text_free") is not True:
                        raise ValueError("두 번째 이후 사진에는 글자가 없어야 합니다.")
        return title, paragraphs, checked

    @staticmethod
    def _article_line_runs(line: str, bold_terms: list[str] | None = None) -> list[tuple[str, bool]]:
        """Split only formatting runs; visible characters remain byte-for-byte intact."""
        if not line:
            return [("", False)]
        if line.lstrip("\ufeff \t").startswith("❝"):
            return [(line, True)]
        emphasized = [False] * len(line)
        for term in bold_terms or []:
            if not term:
                continue
            start = line.find(term)
            while start >= 0:
                emphasized[start:start + len(term)] = [True] * len(term)
                start = line.find(term, start + 1)
        runs = []
        start = 0
        for index in range(1, len(line) + 1):
            if index == len(line) or emphasized[index] != emphasized[start]:
                runs.append((line[start:index], emphasized[start]))
                start = index
        return runs

    @classmethod
    def _arrange_article_document(
        cls, data: dict, paragraphs: list[str], image_ids: list[str], positions: list[int],
        *, bold_terms: list[str] | None = None, bold_style: dict | None = None, visual_style: dict | None = None,
    ) -> dict:
        """Build eight semantic text components, preserving sentence spacing inside each."""
        quote_layouts = (visual_style or {}).get("quote_layouts")
        document = json.loads(json.dumps(data))
        components = document.get("document", {}).get("components", [])
        texts = [item for item in components if item.get("@ctype") == "text"]
        images = [item for item in components if item.get("@ctype") == "image"]
        if not texts or len(images) != len(image_ids) or len(image_ids) != len(set(image_ids)):
            raise RuntimeError("본문 또는 첨부 이미지 수가 예상과 달라 재배치를 중단했습니다.")
        by_id = {item.get("id"): item for item in images}
        if set(image_ids) != set(by_id) or len(positions) != len(image_ids):
            raise RuntimeError("첨부 이미지 식별자를 확인하지 못했습니다.")
        template = texts[0]
        paragraph_template = (template.get("value") or [{}])[0]
        node_template = (paragraph_template.get("nodes") or [{}])[0]
        base_style = dict(node_template.get("style", {"@ctype": "nodeStyle"}))
        for key in bold_style or {}:
            base_style.pop(key, None)
        # The toolbar/template may retain gray or an earlier emphasis. Every
        # plain run starts black; only an explicit phrase gets decoration.
        for key in ("bold", "backgroundColor", "underline"):
            base_style.pop(key, None)
        base_style["fontColor"] = BODY_TEXT_COLOR
        fresh_id = lambda: "SE-" + str(uuid.uuid4())
        arranged = []
        for index, value in enumerate(paragraphs):
            component = json.loads(json.dumps(template))
            component["id"] = fresh_id()
            component["value"] = []
            for line in value.split("\n"):
                nodes = []
                for run, run_style in line_style_runs(line, bold_terms, index, visual_style):
                    if run_style and not bold_style:
                        raise RuntimeError("스마트에디터의 실제 굵게 서식을 확인하지 못했습니다.")
                    nodes.append({"id": fresh_id(), "@ctype": "textNode", "value": run or "\u200b",
                                  "style": {**base_style, **(bold_style if run_style else {}), **run_style}})
                component["value"].append({
                    "id": fresh_id(), "@ctype": "paragraph",
                    "style": paragraph_template.get("style", {"@ctype": "paragraphStyle", "align": "left"}),
                    "nodes": nodes,
                })
            if quote_layouts:
                if len(quote_layouts) != len(paragraphs):
                    raise ValueError("인용구 모양 수가 본문 구역 수와 다릅니다.")
                for kind, start, end in quote_parts(value, quote_layouts[index]):
                    part = json.loads(json.dumps(component))
                    part["id"] = fresh_id()
                    part["value"] = part["value"][start:end]
                    if kind == "quotation":
                        part = {"@ctype": "quotation", "id": part["id"], "align": "center",
                                "layout": quote_layouts[index], "source": None, "value": part["value"]}
                    arranged.append(part)
            else:
                arranged.append(component)
            arranged.extend(by_id[image_id] for image_id, position in zip(image_ids, positions) if position == index)
        # Fresh writer only: unknown components are unsafe to silently keep in a new article.
        header = [item for item in components if item.get("@ctype") == "documentTitle"]
        allowed = {"documentTitle", "text", "image"} | ({"quotation"} if quote_layouts else set())
        if any(item.get("@ctype") not in allowed for item in components):
            raise RuntimeError("예상하지 못한 편집기 콘텐츠가 있어 발행을 중단했습니다.")
        document["document"]["components"] = header + arranged
        return document

    @classmethod
    def _read_article_document(cls, driver) -> dict:
        if not cls._find_editor_fields(driver):
            raise RuntimeError("네이버 본문 편집기를 찾지 못했습니다.")
        result = driver.execute_script("return SmartEditor.getEditor('blogpc001').getDocumentData();")
        if not isinstance(result, dict) or not isinstance(result.get("document"), dict):
            raise RuntimeError("네이버 편집기 문서 구조를 확인하지 못했습니다.")
        return result

    @classmethod
    def _verify_article_document(
        cls, data: dict, paragraphs: list[str], image_ids: list[str], positions: list[int],
        *, bold_terms: list[str] | None = None, bold_style: dict | None = None, visual_style: dict | None = None,
    ) -> bool:
        quote_layouts = (visual_style or {}).get("quote_layouts")
        if quote_layouts:
            try:
                data = cls._collapse_quoted_document(data, paragraphs, quote_layouts)
            except (ValueError, IndexError, TypeError, KeyError):
                return False
        components = data.get("document", {}).get("components", [])
        actual_paragraphs = []
        actual_images = []
        for component in components:
            kind = component.get("@ctype")
            if kind == "text":
                rows = component.get("value", [])
                if not rows:
                    return False
                lines = []
                for row in rows:
                    values = [str(node.get("value", "")) for node in row.get("nodes", [])]
                    line = "".join(values)
                    if line == "\u200b":
                        line = ""
                    styled_runs = line_style_runs(line, bold_terms, len(actual_paragraphs), visual_style)
                    expected_runs = [(value, bool(style.get('bold'))) for value, style in styled_runs]
                    if any(emphasized for _value, emphasized in expected_runs) and not bold_style:
                        return False
                    if bold_style:
                        expected_flags = [emphasized for value, emphasized in expected_runs for _ in value]
                        actual_flags = []
                        for node, value in zip(row.get("nodes", []), values):
                            if value == "\u200b" and not line:
                                continue
                            style = node.get("style", {})
                            applied = all(style.get(key) == value for key, value in bold_style.items())
                            actual_flags.extend([applied] * len(value))
                        if actual_flags != expected_flags:
                            return False
                    expected_styles = [style for text, style in styled_runs for _ in text]
                    actual_styles = [node.get("style", {}) for node, value in zip(row.get("nodes", []), values)
                                     for _ in (value if line else "")]
                    if len(actual_styles) != len(expected_styles):
                        return False
                    for actual, expected in zip(actual_styles, expected_styles):
                        if (actual.get("fontColor", "").lower() != expected.get("fontColor", BODY_TEXT_COLOR)
                                or bool(actual.get("underline")) != bool(expected.get("underline"))
                                or actual.get("backgroundColor", "").lower() != expected.get("backgroundColor", "")):
                            return False
                    lines.append(line)
                actual_paragraphs.append("\n".join(lines))
            elif kind == "image":
                actual_images.append((component.get("id"), len(actual_paragraphs) - 1))
            elif kind != "documentTitle":
                return False
        return actual_paragraphs == paragraphs and actual_images == list(zip(image_ids, positions))

    @staticmethod
    def _collapse_quoted_document(data, paragraphs, layouts):
        """Validate exact physical component boundaries, then recover eight logical sections."""
        if len(layouts) != len(paragraphs):
            raise ValueError("인용구 수 불일치")
        original = data.get("document", {}).get("components", [])
        components = [c for c in original if c.get("@ctype") != "documentTitle"]
        collapsed = [c for c in original if c.get("@ctype") == "documentTitle"]
        cursor = 0
        for paragraph, layout in zip(paragraphs, layouts):
            rows = []
            for kind, start, end in quote_parts(paragraph, layout):
                part = components[cursor]; cursor += 1
                if part.get("@ctype") != kind or len(part.get("value") or []) != end - start:
                    raise ValueError("소제목과 본문 배치 불일치")
                if kind == "quotation" and (part.get("layout") != layout or part.get("source")):
                    raise ValueError("인용구 모양 또는 출처 불일치")
                rows.extend(part["value"])
            collapsed.append({"@ctype": "text", "value": rows})
            while cursor < len(components) and components[cursor].get("@ctype") == "image":
                collapsed.append(components[cursor]); cursor += 1
        if cursor != len(components):
            raise ValueError("예상 외 추가 문서 내용")
        return {"document": {"components": collapsed}}

    @staticmethod
    def _set_article_document(driver, data: dict) -> None:
        result = driver.execute_async_script("""
            const done=arguments[arguments.length-1], data=arguments[0];
            try { Promise.resolve(SmartEditor.getEditor('blogpc001').setDocumentData(data))
              .then(()=>done({ok:true})).catch(e=>done({ok:false,error:String(e)})); }
            catch(e) { done({ok:false,error:String(e)}); }
        """, data)
        if not result or result.get("ok") is not True:
            raise RuntimeError("8개 본문 구역과 사진 배치를 편집기에 적용하지 못했습니다.")

    @staticmethod
    def _observed_bold_style(before: dict, after: dict) -> dict:
        def first_text_node(data):
            for component in data.get("document", {}).get("components", []):
                if component.get("@ctype") == "text":
                    for row in component.get("value", []):
                        for node in row.get("nodes", []):
                            if str(node.get("value", "")).strip("\u200b \t"):
                                return node
            return {}
        original, formatted = first_text_node(before), first_text_node(after)
        if not original or original.get("value") != formatted.get("value"):
            return {}
        old_style, new_style = original.get("style", {}), formatted.get("style", {})
        return {key: value for key, value in new_style.items()
                if key != "@ctype" and old_style.get(key) != value}

    def _prepare_article_in_writer(
        self, driver, blog_id: str, title: str, paragraphs: list[str], images: list[dict],
        *, bold_terms: list[str] | None = None, visual_style: dict | None = None,
    ) -> list[str]:
        self._active_article_bold_style = None
        title_box, body_box = self._open_writer(driver, blog_id)
        if self._editor_has_existing_content(title_box) or self._editor_has_existing_content(body_box):
            raise RuntimeError("글쓰기 화면에 기존 내용이 있어 덮어쓰지 않았습니다.")
        self._replace_editor_text(driver, title_box, title)
        self._replace_editor_text(driver, body_box, "\n\n".join(paragraphs))
        initial = self._read_article_document(driver)
        if any(item.get("@ctype") == "image" for item in initial["document"].get("components", [])):
            raise RuntimeError("기존 사진이 있는 글에는 자동 발행하지 않습니다.")
        if any(style.get('bold') for index, section in enumerate(paragraphs) for line in section.split("\n")
               for _value, style in line_style_runs(line, bold_terms, index, visual_style)):
            # Observed from native Ctrl+B on SmartEditor ONE, 2026-09-12:
            # nodeStyle.bold=true renders as <b> inside the editor text node.
            self._active_article_bold_style = {"bold": True}
        image_ids: list[str] = []
        positions: list[int] = []
        for index, image in enumerate(images):
            if self.stop_event.is_set():
                raise RuntimeError("사용자가 작업을 중지했습니다.")
            if hashlib.sha256(Path(image["path"]).read_bytes()).hexdigest() != image["sha256"]:
                raise RuntimeError("업로드 직전 이미지 변경을 감지하여 발행을 중단했습니다.")
            if index:
                self._focus_body_image_position(driver, index, len(images))
            self._upload_blog_images(driver, [image["path"]])
            current = self._read_article_document(driver)
            current_ids = [item.get("id") for item in current["document"].get("components", []) if item.get("@ctype") == "image"]
            added = [value for value in current_ids if value not in image_ids]
            if len(added) != 1 or not added[0] or len(current_ids) != len(image_ids) + 1:
                raise RuntimeError("이번에 업로드한 사진 1장을 고유하게 확인하지 못했습니다.")
            image_ids.append(added[0])
            positions.append(image["paragraph_index"])
            arranged = self._arrange_article_document(current, paragraphs, image_ids, positions,
                bold_terms=bold_terms, bold_style=self._active_article_bold_style, visual_style=visual_style)
            self._set_article_document(driver, arranged)
            WebDriverWait(driver, 15).until(lambda d: self._verify_article_document(
                self._read_article_document(d), paragraphs, image_ids, positions,
                bold_terms=bold_terms, bold_style=self._active_article_bold_style, visual_style=visual_style,
            ))
        return image_ids

    @classmethod
    def _article_native_bold_rendered(cls, driver, paragraphs: list[str], bold_terms: list[str] | None = None, visual_style=None) -> bool:
        expected = [[[(value, bool(style.get('bold'))) for value, style in line_style_runs(line, bold_terms, index, visual_style)]
                     for line in section.split("\n")] for index, section in enumerate(paragraphs)]
        if not any(emphasized for section in expected for row in section for _value, emphasized in row):
            return True
        rendered = driver.execute_script("""
            return [...document.querySelectorAll('.se-component.se-text, .se-component[data-name="text"], .se-component.se-quotation')]
              .map(component=>[...component.querySelectorAll(component.classList.contains('se-quotation') ? '.se-quote .se-text-paragraph' : '.se-text-paragraph')].map(row=>{
                const walk=document.createTreeWalker(row,NodeFilter.SHOW_TEXT), nodes=[];
                while(walk.nextNode()) {
                  const node=walk.currentNode, weight=getComputedStyle(node.parentElement).fontWeight;
                  nodes.push({value:node.nodeValue,bold:weight==='bold'||weight==='bolder'||parseInt(weight,10)>=600});
                }
                return nodes;
              }));
        """)
        if not isinstance(rendered, list):
            return False
        expected_rows, actual_rows = [r for section in expected for r in section], [r for section in rendered for r in section]
        if len(expected_rows) != len(actual_rows):
            return False
        for expected_row, actual_row in zip(expected_rows, actual_rows):
            expected_text = "".join(value for value, _bold in expected_row)
            actual_text = "".join(str(node.get("value", "")) for node in actual_row).replace("\u200b", "")
            if actual_text != expected_text:
                return False
            expected_flags = [bold for value, bold in expected_row for _ in value]
            actual_flags = [bool(node.get("bold")) for node in actual_row for _ in str(node.get("value", "")).replace("\u200b", "")]
            if actual_flags != expected_flags:
                return False
        return True

    @classmethod
    def _article_native_colors_rendered(cls, driver, paragraphs, bold_terms=None, visual_style=None, *, published=False) -> bool:
        rendered = driver.execute_script("""
            return [...document.querySelectorAll('.se-component.se-text, .se-component[data-name="text"], .se-component.se-quotation')]
              .map(component=>[...component.querySelectorAll(component.classList.contains('se-quotation') ? '.se-quote .se-text-paragraph' : '.se-text-paragraph')].map(row=>{
                const walk=document.createTreeWalker(row,NodeFilter.SHOW_TEXT), nodes=[];
                while(walk.nextNode()) {
                  const node=walk.currentNode, style=getComputedStyle(node.parentElement);
                  let background='', underline=false, e=node.parentElement;
                  while(e && row.contains(e)) {
                    const s=getComputedStyle(e);
                    if(!background && s.backgroundColor!=='rgba(0, 0, 0, 0)' && s.backgroundColor!=='transparent') background=s.backgroundColor;
                    if(s.textDecorationLine.includes('underline')) underline=true;
                    e=e.parentElement;
                  }
                  const hashtag=node.parentElement.closest('span.__se-hash-tag');
                  nodes.push({value:node.nodeValue,color:style.color,background,underline,
                    native_hashtag:Boolean(hashtag && row.contains(hashtag))});
                }
                return nodes;
              }));
        """)
        def rgb(value):
            return 'rgb(' + ', '.join(str(int(value[i:i+2], 16)) for i in (1, 3, 5)) + ')' if value else ''
        if not isinstance(rendered, list):
            return False
        expected_rows = [(index, line) for index, section in enumerate(paragraphs) for line in section.split('\n')]
        actual_rows = [row for section in rendered for row in section]
        if len(expected_rows) != len(actual_rows):
            return False
        hashtag_row = next((row for row in range(len(expected_rows) - 1, -1, -1)
                            if expected_rows[row][0] == len(paragraphs) - 1
                            and re.fullmatch(r"#[^\s#]+(?:[ \t]+#[^\s#]+)*", expected_rows[row][1].strip())), None)
        for row_number, ((index, line), nodes) in enumerate(zip(expected_rows, actual_rows)):
            expected = [(char, rgb(style.get('fontColor', BODY_TEXT_COLOR)), rgb(style.get('backgroundColor', '')),
                         bool(style.get('underline'))) for text, style in line_style_runs(line, bold_terms, index, visual_style) for char in text]
            actual = [(char, node.get('color'), node.get('background', ''), bool(node.get('underline')), node.get('native_hashtag') is True)
                      for node in nodes for char in str(node.get('value', '')).replace('\u200b', '')]
            if len(actual) != len(expected):
                return False
            for wanted, observed in zip(expected, actual):
                color = observed[1]
                # Published Naver pages wrap plain footer hashtags in their own
                # blue span. Only this observed tint may replace default black;
                # explicit keyword colors and all other formatting remain exact.
                if (published and row_number == hashtag_row and observed[4]
                        and wanted[1] == 'rgb(0, 0, 0)' and color == 'rgb(56, 124, 187)'):
                    color = wanted[1]
                if (observed[0], color, observed[2], observed[3]) != wanted:
                    return False
        return True

    @classmethod
    def _article_ready_to_publish(
        cls, driver, title: str, paragraphs: list[str], image_ids: list[str], positions: list[int],
        *, bold_terms: list[str] | None = None, bold_style: dict | None = None, visual_style: dict | None = None,
    ) -> bool:
        fields = cls._find_editor_fields(driver)
        if not fields or cls._normalized_text(cls._editor_text(fields[0])) != cls._normalized_text(title):
            return False
        if cls._image_component_count(driver) != len(image_ids):
            return False
        return (cls._verify_article_document(cls._read_article_document(driver), paragraphs, image_ids, positions,
                                             bold_terms=bold_terms, bold_style=bold_style, visual_style=visual_style)
                and cls._article_native_bold_rendered(driver, paragraphs, bold_terms, visual_style)
                and (not bold_style or cls._article_native_colors_rendered(driver, paragraphs, bold_terms, visual_style)))

    @classmethod
    def _find_publish_control(cls, driver, final: bool = False):
        def finder():
            candidates = driver.find_elements(By.XPATH, "//button[normalize-space(.)='발행' or @aria-label='발행']")
            matched = []
            for button in candidates:
                if not button.is_displayed() or not button.is_enabled():
                    continue
                scope = driver.execute_script("""
                    const e=arguments[0];
                    const panel=e.closest('[role="dialog"], .layer_publish, [class*="layer_publish"], [class*="publish_layer"], [class*="publishLayer"], [class*="publish_container"], [data-testid="publish-layer"]');
                    return {inside:Boolean(panel), settings:Boolean(panel && /공개|카테고리|발행 설정|주제/.test(panel.innerText))};
                """, button) or {}
                if (final and scope.get("inside") and scope.get("settings")) or (not final and not scope.get("inside")):
                    matched.append(button)
            # Multiple matching controls are ambiguous; leave the editor for inspection.
            return matched[0] if len(matched) == 1 else None
        return cls._find_across_frames(driver, finder)

    @classmethod
    def _published_article_url(cls, driver, blog_id: str, title: str) -> str:
        driver.switch_to.default_content()
        url = str(driver.current_url or "")
        parts = urllib.parse.urlparse(url)
        if parts.hostname not in {"blog.naver.com", "m.blog.naver.com"}:
            return ""
        query = urllib.parse.parse_qs(parts.query)
        clean_path = urllib.parse.unquote(parts.path).strip("/")
        match = re.fullmatch(re.escape(blog_id) + r"/(\d+)", clean_path, re.I)
        if not match and not (
            parts.path.lower().endswith("/postview.naver")
            and (query.get("blogId") or [""])[0].lower() == blog_id.lower()
            and re.fullmatch(r"\d+", (query.get("logNo") or [""])[0])
        ):
            return ""
        def finder():
            headings = driver.find_elements(By.CSS_SELECTOR, ".se-title-text, .htitle, .pcol1 .itemSubjectBoldfont, h3.se_textarea")
            return any(element.is_displayed() and cls._normalized_text(element.text) == cls._normalized_text(title) for element in headings)
        return url if cls._find_across_frames(driver, finder) else ""

    def inspect_published_naver_article(
        self, blog_id: str, article: dict, *, expected_image_ids: list[str] | None = None
    ) -> dict:
        """Read the current published page only; never navigate, edit, save or submit."""
        blog_id = self._clean_blog_id(blog_id)
        title = str(article.get("title", "")).strip()
        paragraphs = article.get("paragraphs", [])
        images = article.get("images", [])
        if not title or not isinstance(paragraphs, list) or len(paragraphs) != 8:
            raise ValueError("게시글 검증에는 원래 제목과 8개 본문 구역이 필요합니다.")
        driver = self._driver()
        url = self._published_article_url(driver, blog_id, title)
        if not url:
            return {"verified": False, "url": "", "title_matches": False,
                    "message": "현재 페이지의 게시글 주소 또는 제목이 원문과 일치하지 않습니다."}
        snapshot = driver.execute_script("""
            const root=document.querySelector('.se-main-container');
            if(!root) return null;
            const sections=[], images=[], components=[];
            for(const component of root.querySelectorAll('.se-component')) {
              const isQuote=component.matches('.se-quotation');
              if(component.matches('.se-text, [data-name="text"]') || isQuote) {
                const rows=[...component.querySelectorAll(isQuote ? '.se-quote .se-text-paragraph' : '.se-text-paragraph')]
                  .map(row=>({nodes:[{value:(row.innerText||'').replace(/\\u200b/g,'')}]}));
                components.push({'@ctype':isQuote?'quotation':'text',value:rows,
                  layout:isQuote?[...component.classList].find(c=>c.startsWith('se-l-'))?.slice(5):undefined});
              } else if(component.matches('.se-image, [data-name="image"]')) {
                components.push({'@ctype':'image',id:component.id});
              }
              if(component.matches('.se-text, [data-name="text"]')) {
                sections.push([...component.querySelectorAll('.se-text-paragraph')]
                  .map(row=>(row.innerText||'').replace(/\\u200b/g,'')) .join('\\n'));
              } else if(component.matches('.se-image, [data-name="image"]')) {
                images.push({id:component.id,position:sections.length-1});
              }
            }
            return {sections,images,components};
        """) or {}
        actual_sections = snapshot.get("sections", [])
        actual_images = snapshot.get("images", [])
        visual_style = article.get('visual_style')
        if visual_style and 'components' in snapshot:
            try:
                collapsed = self._collapse_quoted_document({'document':{'components':snapshot['components']}},
                                                            paragraphs, visual_style['quote_layouts'])
                actual_sections, actual_images = [], []
                for part in collapsed['document']['components']:
                    if part['@ctype'] == 'text':
                        actual_sections.append('\n'.join(''.join(n['value'] for n in row['nodes']) for row in part['value']))
                    elif part['@ctype'] == 'image':
                        actual_images.append({'id':part['id'],'position':len(actual_sections)-1})
            except (ValueError, IndexError, KeyError, TypeError):
                return {'verified':False,'url':url,'message':'인용구와 본문 구역 배치가 저장된 원고와 다릅니다.'}
        expected_positions = sorted(item["paragraph_index"] for item in images)
        text_matches = (len(actual_sections) == 8 and
                        [value.strip() for value in actual_sections] == [value.strip() for value in paragraphs])
        position_matches = [item.get("position") for item in actual_images] == expected_positions
        identity_matches = (None if expected_image_ids is None else
                            [item.get("id") for item in actual_images] == expected_image_ids)
        bold_matches = self._article_native_bold_rendered(driver, paragraphs, article.get("bold_terms", []), visual_style)
        colors_match = not visual_style or self._article_native_colors_rendered(driver, paragraphs, article.get('bold_terms', []), visual_style, published=True)
        verified = text_matches and position_matches and bold_matches and colors_match and identity_matches is not False
        return {"verified": bool(verified), "url": url, "title_matches": True,
                "section_count": len(actual_sections), "sections_match": text_matches,
                "image_count": len(actual_images), "image_positions_match": position_matches,
                "image_identity_matches": identity_matches, "bold_rendered": bold_matches, "colors_rendered": colors_match}

    @classmethod
    def _publication_key(cls, blog_id: str, article: dict) -> str:
        """Compute the original submission identity without opening image files."""
        blog_id = cls._clean_blog_id(blog_id)
        if not isinstance(article, dict):
            raise ValueError("발행 기록 조회에는 원고 객체가 필요합니다.")
        title, paragraphs, images = article.get("title"), article.get("paragraphs"), article.get("images")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100 or "\n" in title:
            raise ValueError("발행 제목은 1~100자의 한 줄이어야 합니다.")
        if (not isinstance(paragraphs, list) or len(paragraphs) != 8
                or any(not isinstance(item, str) or not item.strip() for item in paragraphs)):
            raise ValueError("발행 기록 조회에는 비어 있지 않은 본문 8개 구역이 필요합니다.")
        if not isinstance(images, list) or not 6 <= len(images) <= 16:
            raise ValueError("발행 기록 조회에는 이미지 6~16장의 식별 정보가 필요합니다.")
        for item in images:
            if (not isinstance(item, dict) or type(item.get("paragraph_index")) is not int
                    or not 0 <= item["paragraph_index"] <= 7
                    or not isinstance(item.get("sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
                raise ValueError("발행 기록 조회에 필요한 이미지 해시 또는 배치 정보가 올바르지 않습니다.")
        identity = {"blog_id": blog_id.lower(), "title": title.strip(),
                    "paragraphs": [item.strip() for item in paragraphs],
                    "images": [{"sha256": item["sha256"], "position": item["paragraph_index"]}
                               for item in sorted(images, key=lambda item: item["paragraph_index"])]}
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def publication_receipt_for(self, blog_id: str, article: dict) -> dict | None:
        """Read a matching durable receipt; never open a browser or require local images."""
        key = self._publication_key(blog_id, article)
        path = self.data_dir / "publication_receipts" / f"{key}.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError("기존 발행 기록을 읽을 수 없어 중복 발행 방지를 위해 중단했습니다.") from exc
        try:
            receipt = json.loads(raw)
            if (not isinstance(receipt, dict) or receipt.get("status") not in {"published", "uncertain"}
                    or receipt.get("article_key", key) != key
                    or type(receipt.get("published")) is not bool
                    or (receipt["published"] != (receipt["status"] == "published"))):
                raise ValueError("Invalid publication receipt")
            if receipt["published"]:
                parts = urllib.parse.urlparse(str(receipt.get("url", "")))
                query = urllib.parse.parse_qs(parts.query)
                blog_key = self._clean_blog_id(blog_id).lower()
                path_matches = re.fullmatch(re.escape(blog_key) + r"/\d+/?",
                                            urllib.parse.unquote(parts.path).lstrip("/").lower())
                query_matches = (parts.path.lower().endswith("/postview.naver")
                                 and (query.get("blogId") or [""])[0].lower() == blog_key
                                 and re.fullmatch(r"\d+", (query.get("logNo") or [""])[0]))
                if (parts.scheme not in {"https", "http"}
                        or parts.hostname not in {"blog.naver.com", "m.blog.naver.com"}
                        or not (path_matches or query_matches)):
                    raise ValueError("Invalid published URL")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("기존 발행 기록을 읽을 수 없어 중복 발행 방지를 위해 중단했습니다.") from exc
        if receipt["published"] and "content_verified" not in receipt:
            receipt = {**receipt, "content_verified": False,
                       "content_verification_issues": ["이전 버전의 발행 기록에는 게시 본문·이미지 확인 결과가 없습니다."]}
        return receipt

    def _write_publication_receipt(self, path: Path, receipt: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
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
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _claim_publication_receipt(path: Path, receipt: dict) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return False
        return True

    @staticmethod
    def _arm_article_draft_confirmation(driver, token: str) -> None:
        """Observe a new save notice; an old toast or network request is not proof."""
        driver.execute_script(r"""
            if (window.__pictureCleanerArticleDraftProbe?.observer)
              window.__pictureCleanerArticleDraftProbe.observer.disconnect();
            const probe={token:arguments[0], saved:false};
            window.__pictureCleanerArticleDraftProbe=probe;
            const selector='[role="status"], [role="alert"], [class*="toast"], '
              + '[class*="notice"], [class*="notification"], [class*="layer_msg"], '
              + '[class*="save_status"], [class*="saveStatus"]';
            const visible=e=>Boolean(e.getClientRects().length)
              && getComputedStyle(e).visibility!=='hidden';
            const success=e=>{
              if(e.closest('.se-component, [contenteditable="true"], textarea')) return false;
              const text=(e.innerText||'').replace(/\s+/g,' ').trim();
              return /^(?:임시\s*)?저장(?:이|가)?\s*(?:되었습니다|되었어요|완료(?:되었습니다)?|됨)[.!\s]*$/.test(text);
            };
            const baseline=new WeakMap();
            for(const e of document.querySelectorAll(selector))
              baseline.set(e, {visible:visible(e), text:e.innerText||''});
            probe.observer=new MutationObserver(()=>{
              for(const e of document.querySelectorAll(selector)) {
                const before=baseline.get(e), now={visible:visible(e), text:e.innerText||''};
                if(now.visible && success(e) && (!before || !before.visible || before.text!==now.text)) {
                  probe.saved=true;
                  probe.observer.disconnect();
                  return;
                }
                baseline.set(e,now);
              }
            });
            probe.observer.observe(document.documentElement, {
              childList:true, subtree:true, characterData:true, attributes:true,
              attributeFilter:['class','style','hidden','aria-hidden']
            });
        """, token)

    @classmethod
    def _fresh_article_draft_confirmed(cls, driver, token: str) -> bool:
        def finder():
            return bool(driver.execute_script("""
                const probe=window.__pictureCleanerArticleDraftProbe;
                return Boolean(probe && probe.token===arguments[0] && probe.saved===true);
            """, token))
        return bool(cls._find_across_frames(driver, finder))

    def _save_prepared_article_draft(self, driver, prepared: dict) -> dict:
        def single_save_button(d):
            buttons = self._find_draft_buttons(d) or []
            candidates = [button for button in buttons if self._normalized_text(button.text) in {"저장", "임시저장"}]
            return candidates[0] if len(candidates) == 1 else None
        try:
            button = WebDriverWait(driver, 15).until(single_save_button)
        except TimeoutException as exc:
            raise RuntimeError("임시저장 버튼을 고유하게 찾지 못했습니다. 입력 화면을 유지합니다.") from exc
        if self.stop_event.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")
        token = str(uuid.uuid4())
        self._arm_article_draft_confirmation(driver, token)
        try:
            button.click()
            WebDriverWait(driver, 20).until(lambda d: self._fresh_article_draft_confirmed(d, token))
        except WebDriverException as exc:
            raise RuntimeError(
                "임시저장 버튼은 한 번 눌렀지만 새로운 저장 완료 안내를 확인하지 못했습니다. "
                "저장 성공으로 처리하지 않았으며 다시 누르지 않습니다. 웨일에서 확인해 주세요."
            ) from exc
        url = str(driver.current_url or "")
        parsed = urllib.parse.urlparse(url)
        if parsed.path.lower().endswith("/postview.naver") or re.fullmatch(r"/[^/]+/\d+/?", parsed.path):
            raise RuntimeError("임시저장 중 예상하지 못한 게시글 화면 이동이 감지되었습니다. 저장 완료로 처리하지 않았습니다.")
        result = {**prepared, "saved": True, "status": "draft_saved", "url": url,
                  "saved_at": datetime.now().isoformat(timespec="seconds"),
                  "message": "새로운 임시저장 완료 안내를 확인했습니다."}
        self.log(result["message"])
        return result

    def publish_naver_article(
        self, blog_id: str, article: dict, *, publish: bool = True, save_draft: bool = False
    ) -> dict:
        """Insert a reviewed article, then publish, save a draft, or leave it entered.

        A durable receipt is written before the final click. An uncertain network
        response is never retried, including on a later application run.
        """
        if publish and save_draft:
            raise ValueError("자동 발행과 임시저장을 동시에 선택할 수 없습니다. 임시저장은 publish=False로 실행하세요.")
        blog_id = self._clean_blog_id(blog_id)
        if publish:
            prior = self.publication_receipt_for(blog_id, article)
            if prior is not None:
                return {**prior, "reused_receipt": True}
        title, paragraphs, images = self._validate_publish_article(article)
        key = self._publication_key(blog_id, {"title": title, "paragraphs": paragraphs, "images": images})
        receipt_path = self.data_dir / "publication_receipts" / f"{key}.json"
        driver = self._driver()
        bold_terms = article.get("bold_terms", [])
        visual_style = article.get("visual_style")
        if visual_style is None:
            visual_style = choose_visual_style(paragraphs, article.get('bold_phrases'),
                                               article.get('highlight_phrases'),
                                               enrich_body_bold=True, bold_terms=bold_terms)
            article["visual_style"] = visual_style
        if not isinstance(visual_style, dict) or len(visual_style.get("quote_layouts", [])) != len(paragraphs):
            raise ValueError("저장된 소제목 인용구 설정이 올바르지 않습니다.")
        # Reuse saved quote layouts and highlight colors when refreshing an
        # older prepared article. This changes presentation, never its text.
        visual_style['bold_phrases'] = supplement_bold_phrases(
            paragraphs, visual_style.get('bold_phrases'), visual_style.get('highlight_phrases'), bold_terms)
        for paragraph, layout in zip(paragraphs, visual_style['quote_layouts']):
            quote_parts(paragraph, layout)
        image_ids = self._prepare_article_in_writer(driver, blog_id, title, paragraphs, images, bold_terms=bold_terms, visual_style=visual_style)
        # Naver can show its old-draft recovery prompt after image uploads have
        # finished. Cancel only that known restore action, then verify our copy.
        self._handle_writer_recovery_prompt(driver)
        bold_style = getattr(self, "_active_article_bold_style", None)
        positions = [item["paragraph_index"] for item in images]
        if not self._article_ready_to_publish(driver, title, paragraphs, image_ids, positions,
                                              bold_terms=bold_terms, bold_style=bold_style, visual_style=visual_style):
            raise RuntimeError("제목, 8개 문단, 사진 수와 순서 검증에 실패하여 발행하지 않았습니다.")
        prepared = {"published": False, "saved": False, "status": "prepared", "url": "", "title": title,
                    "paragraph_count": 8, "image_count": len(images), "article_key": key,
                    "image_component_ids": image_ids, "image_positions": positions}
        prepared["visual_style"] = visual_style
        if save_draft:
            return self._save_prepared_article_draft(driver, prepared)
        if not publish:
            self.log("8개 문단과 사진 배치를 검증했습니다. 글쓰기 화면에 입력된 상태입니다.")
            return prepared
        if self.stop_event.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")
        opener = WebDriverWait(driver, 15).until(lambda d: self._find_publish_control(d, final=False))
        try:
            opener.click()
        except ElementClickInterceptedException:
            # Only the settings opener may be retried. The final submission
            # below always remains protected by its durable receipt.
            self._handle_writer_recovery_prompt(driver)
            if self.stop_event.is_set():
                raise RuntimeError("사용자가 작업을 중지했습니다.")
            if not self._article_ready_to_publish(driver, title, paragraphs, image_ids, positions,
                                                  bold_terms=bold_terms, bold_style=bold_style, visual_style=visual_style):
                raise RuntimeError("안내창 처리 후 내용 검증에 실패하여 발행 설정을 다시 열지 않았습니다.")
            opener = WebDriverWait(driver, 15).until(lambda d: self._find_publish_control(d, final=False))
            opener.click()
        try:
            WebDriverWait(driver, 15).until(lambda d: self._find_publish_control(d, final=True))
        except TimeoutException as exc:
            raise RuntimeError(
                "발행 설정 창에서 최종 발행 버튼을 고유하게 확인하지 못했습니다. "
                "최종 버튼은 누르지 않았으며 입력된 글과 사진을 화면에 유지합니다."
            ) from exc
        # Recheck after the panel opens; user edits or upload failures must not slip through.
        if not self._article_ready_to_publish(driver, title, paragraphs, image_ids, positions,
                                              bold_terms=bold_terms, bold_style=bold_style, visual_style=visual_style):
            raise RuntimeError("발행 직전 내용 검증에 실패하여 최종 발행 버튼을 누르지 않았습니다.")
        final_button = self._find_publish_control(driver, final=True)
        if final_button is None or self.stop_event.is_set():
            raise RuntimeError("최종 발행 버튼을 확인하지 못했거나 작업이 중지되었습니다.")
        receipt = {**prepared, "status": "uncertain", "submitted_at": datetime.now().isoformat(timespec="seconds"),
                   "message": "발행 제출을 시도했습니다. 완료가 확인되지 않으면 웨일에서 게시 상태를 확인하세요. 자동 재시도하지 않습니다."}
        if not self._claim_publication_receipt(receipt_path, receipt):
            # Another running application acquired this article while we prepared it.
            prior = self.publication_receipt_for(blog_id, article)
            return {**(prior or receipt), "reused_receipt": True,
                    "message": "다른 실행의 발행 기록이 있어 최종 발행을 누르지 않았습니다."}
        try:
            final_button.click()
            url = WebDriverWait(driver, 30).until(lambda d: self._published_article_url(d, blog_id, title))
        except (WebDriverException, TimeoutError) as exc:
            receipt["error"] = str(exc)
            self._write_publication_receipt(receipt_path, receipt)
            self.log(receipt["message"])
            return receipt
        receipt.update({"published": True, "status": "published", "url": url,
                        "content_verified": False,
                        "content_verification_issues": ["게시 본문·이미지 확인이 아직 완료되지 않았습니다."],
                        "message": "게시글 주소와 제목을 확인했습니다."})
        # Persist the confirmed submission before inspecting content. Inspection
        # failure must never turn a completed publication into a retryable draft.
        self._write_publication_receipt(receipt_path, receipt)
        snapshot = {}
        try:
            def content_ready(_driver):
                nonlocal snapshot
                inspected = self.inspect_published_naver_article(
                    blog_id, {**article, "title": title, "paragraphs": paragraphs, "images": images},
                    expected_image_ids=image_ids,
                )
                if not isinstance(inspected, dict):
                    raise ValueError("게시 본문·이미지 검사 결과 형식이 올바르지 않습니다.")
                snapshot = inspected
                return snapshot.get("verified") is True
            WebDriverWait(driver, 10).until(content_ready)
            receipt["content_verified"] = True
            receipt["content_verification_issues"] = []
            receipt["message"] = "게시글 주소·제목·본문 8개 구역과 이미지 배치를 확인했습니다."
        except Exception as exc:
            labels = {"title_matches": "게시글 제목이 다릅니다.", "sections_match": "본문 구역이 누락되거나 내용이 다릅니다.",
                      "image_positions_match": "사진 수 또는 배치가 다릅니다.",
                      "image_identity_matches": "게시 사진의 식별 정보가 다릅니다.",
                      "bold_rendered": "중요 내용의 굵은 서식을 확인하지 못했습니다.",
                      "colors_rendered": "본문 강조 색상을 확인하지 못했습니다."}
            issues = [message for flag, message in labels.items() if snapshot.get(flag) is False]
            if isinstance(snapshot.get("message"), str) and snapshot["message"]:
                issues.append(snapshot["message"])
            receipt["content_verification_issues"] = issues or [f"게시 본문·이미지 확인을 완료하지 못했습니다: {exc}"]
            receipt["message"] = "발행은 완료됐지만 게시 내용 확인이 필요합니다. 원고를 보존하며 자동 재발행하지 않습니다."
        receipt["content_verification"] = snapshot
        self._write_publication_receipt(receipt_path, receipt)
        self.log(f"네이버 발행 완료 확인: {url}")
        self.log(receipt["message"])
        return receipt

    @classmethod
    def _replace_editor_text(cls, driver, editor, text: str) -> None:
        try:
            modern_editor = bool(
                driver.execute_script(
                    "return Boolean(window.SmartEditor && "
                    "SmartEditor.getEditor('blogpc001'));"
                )
            )
        except Exception:
            modern_editor = False
        if modern_editor:
            is_title = bool(
                driver.execute_script(
                    "return Boolean(arguments[0].closest("
                    "'.se-documentTitle, .se-title-text, "
                    "[class*=documentTitle]'));",
                    editor,
                )
            )
            result = driver.execute_async_script(
                """
                const value = arguments[0];
                const isTitle = arguments[1];
                const done = arguments[arguments.length - 1];
                (async () => {
                  try {
                    const instance = SmartEditor.getEditor('blogpc001');
                    const data = await instance.getDocumentData();
                    const components = data.document.components || [];
                    const type = isTitle ? 'documentTitle' : 'text';
                    const component = components.find(
                      item => item['@ctype'] === type
                    );
                    if (!component) throw new Error(type + ' component missing');
                    const makeId = () => 'SE-' + crypto.randomUUID();
                    if (isTitle) {
                      const paragraph = component.title[0];
                      const style = paragraph.nodes[0].style || {
                        '@ctype': 'nodeStyle'
                      };
                      paragraph.nodes = [{
                        id: makeId(), value, style, '@ctype': 'textNode'
                      }];
                    } else {
                      const oldParagraph = component.value[0];
                      const nodeStyle = oldParagraph.nodes[0].style || {
                        '@ctype': 'nodeStyle'
                      };
                      const paragraphStyle = oldParagraph.style || {
                        align: 'left', '@ctype': 'paragraphStyle'
                      };
                      const lines = value.split(/\\r?\\n/);
                      component.value = lines.map(line => ({
                        id: makeId(),
                        nodes: [{
                          id: makeId(),
                          value: line || '\\u200b',
                          style: {...nodeStyle},
                          '@ctype': 'textNode'
                        }],
                        style: {...paragraphStyle},
                        '@ctype': 'paragraph'
                      }));
                    }
                    await instance.setDocumentData(data);
                    const actual = isTitle
                      ? instance.getDocumentTitle()
                      : instance.getContentText();
                    done({ok: true, actual: actual || ''});
                  } catch (error) {
                    done({ok: false, error: String(error)});
                  }
                })();
                """,
                text,
                is_title,
            )
            if not result or not result.get("ok"):
                detail = result.get("error", "") if isinstance(result, dict) else ""
                raise RuntimeError(
                    "네이버 새 스마트에디터에 내용을 입력하지 못했습니다. "
                    f"{detail}"
                )
            wanted = cls._normalized_text(text)
            actual = cls._normalized_text(result.get("actual", ""))
            if wanted and (
                wanted[:40] not in actual
                or (len(wanted) > 80 and wanted[-40:] not in actual)
            ):
                raise RuntimeError(
                    "네이버 새 스마트에디터 입력 결과를 확인하지 못했습니다. "
                    "발행과 임시저장은 실행하지 않았습니다."
                )
            return
        editor.click()
        try:
            driver.execute_script(
                """
                const selection = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(arguments[0]);
                selection.removeAllRanges();
                selection.addRange(range);
                """,
                editor,
            )
        except Exception:
            editor.send_keys(cls.EDITOR_MODIFIER, "a")
        editor.send_keys(Keys.BACKSPACE)
        try:
            editor.send_keys(text)
        except WebDriverException:
            pass
        if cls._editor_contains_text(editor, text):
            return
        inserted = driver.execute_script(
            """
            const element = arguments[0];
            const value = arguments[1];
            element.focus();
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(element);
            selection.removeAllRanges();
            selection.addRange(range);
            const ok = document.execCommand('insertText', false, value);
            element.dispatchEvent(new InputEvent('input', {
              bubbles: true, inputType: 'insertText', data: value
            }));
            return ok;
            """,
            editor,
            text,
        )
        try:
            verified = bool(inserted) and bool(
                WebDriverWait(driver, 8).until(
                    lambda _driver: cls._editor_contains_text(editor, text)
                )
            )
        except TimeoutException:
            verified = False
        if not verified:
            raise RuntimeError(
                "네이버 편집기에 제목 또는 본문을 정확히 입력하지 못했습니다. "
                "발행과 임시저장은 실행하지 않았습니다."
            )

    @staticmethod
    def _switch_to_post_frame(driver) -> None:
        driver.switch_to.default_content()
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe#mainFrame, iframe[name='mainFrame']")
        if frames:
            driver.switch_to.frame(frames[0])

    @staticmethod
    def _open_comments(driver) -> bool:
        def comments_visible(active):
            # Empty posts can have a writer without any comment-list element.
            return any(
                element.is_displayed()
                for element in active.find_elements(
                    By.CSS_SELECTOR,
                    "ul.u_cbox_list, .u_cbox_write_box, .u_cbox_write_area",
                )
            )

        if comments_visible(driver):
            return True
        candidates = driver.find_elements(
            By.CSS_SELECTOR,
            "a.btn_comment._cmtList, a._floating_bottom_btn_comment, "
            "a[class*='_cmtList']",
        )
        for element in candidates:
            try:
                if element.is_displayed():
                    driver.execute_script("arguments[0].click()", element)
                    WebDriverWait(driver, 15).until(comments_visible)
                    return True
            except Exception:
                continue
        return comments_visible(driver)

    @staticmethod
    def _top_level_comments(driver):
        for comment_list in driver.find_elements(By.CSS_SELECTOR, "ul.u_cbox_list"):
            if not comment_list.is_displayed():
                continue
            comments = comment_list.find_elements(
                By.XPATH, "./li[contains(@class,'u_cbox_comment')]"
            )
            if comments:
                return comments
        return []

    @classmethod
    def _top_level_comment_by_no(cls, driver, comment_no: str):
        for _ in range(3):
            try:
                return next(
                    (
                        comment
                        for comment in cls._top_level_comments(driver)
                        if cls._comment_no(comment) == comment_no
                    ),
                    None,
                )
            except StaleElementReferenceException:
                time.sleep(0.2)
        return None

    @staticmethod
    def _load_all_comments(driver, limit: int = 2000) -> None:
        for _ in range(100):
            comments = NaverAutomation._top_level_comments(driver)
            if len(comments) >= limit:
                return
            more = next(
                (
                    button
                    for button in driver.find_elements(
                        By.CSS_SELECTOR,
                        "a.u_cbox_btn_more, button.u_cbox_btn_more, "
                        ".u_cbox_paginate a[class*='more']",
                    )
                    if button.is_displayed()
                ),
                None,
            )
            if not more:
                return
            before = len(comments)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'})", more)
            driver.execute_script("arguments[0].click()", more)
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: len(NaverAutomation._top_level_comments(d)) > before
                )
            except TimeoutException:
                return

    @classmethod
    def _own_reply_exists(cls, comment, blog_id: str) -> bool:
        replies = comment.find_elements(By.CSS_SELECTOR, ".u_cbox_reply_area li.u_cbox_comment")
        for reply in replies:
            profile = reply.find_elements(By.CSS_SELECTOR, "a.u_cbox_name")
            href = profile[0].get_attribute("href") if profile else ""
            if cls._comment_author_matches(href or "", blog_id):
                return True
        return False

    @staticmethod
    def _comment_no(comment) -> str:
        identity = "|".join(
            (comment.get_attribute(attribute) or "")
            for attribute in (
                "data-param",
                "data-ui-indexes",
                "class",
                "id",
                "data-info",
            )
        )
        patterns = (
            r"""commentNo['"]?\s*[:=-]\s*['"]?(\d+)""",
            r"idx-commentNo-(\d+)",
            r"__comment_(\d+)",
        )
        for pattern in patterns:
            match = re.search(pattern, identity, re.I)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def _comment_action_elements(cls, comment, selector: str):
        comment_no = cls._comment_no(comment)
        output = []
        for element in comment.find_elements(By.CSS_SELECTOR, selector):
            element_identity = "|".join(
                (element.get_attribute(attribute) or "")
                for attribute in (
                    "data-param",
                    "data-ui-indexes",
                    "class",
                    "id",
                )
            )
            if not comment_no or comment_no in element_identity:
                output.append(element)
        return output

    @classmethod
    def _visible_like_is_on(cls, comment) -> bool:
        try:
            return any(
                candidate.is_displayed()
                and (
                    "u_cbox_btn_recomm_on"
                    in (candidate.get_attribute("class") or "")
                    or re.search(
                        r"""state['"]?\s*:\s*['"]?on""",
                        (candidate.get_attribute("data-ui-indexes") or ""),
                        re.I,
                    )
                )
                for candidate in cls._comment_action_elements(
                    comment,
                    ".u_cbox_btn_recomm",
                )
            )
        except WebDriverException:
            return False

    def _like_comment(self, comment, key: str) -> bool:
        if self.stop_event.is_set():
            return False
        buttons = [
            button
            for button in self._comment_action_elements(
                comment,
                ".u_cbox_btn_recomm",
            )
            if button.is_displayed()
        ]
        if not buttons:
            return False
        if self._visible_like_is_on(comment):
            return False
        button = next(
            (
                candidate
                for candidate in buttons
                if "u_cbox_btn_recomm_on"
                not in (candidate.get_attribute("class") or "")
            ),
            buttons[0],
        )
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});"
            "arguments[0].click();",
            button,
        )
        WebDriverWait(self.driver, 10).until(
            lambda _driver: self._visible_like_is_on(comment)
        )
        # Naver toggles the duplicate on/off buttons immediately. Give its
        # vote request time to settle and reject optimistic UI rollbacks.
        time.sleep(1.0)
        if not self._visible_like_is_on(comment):
            raise RuntimeError("댓글 공감이 서버에 반영되지 않았습니다.")
        liked_history = self.state.setdefault("liked", [])
        if key not in liked_history:
            liked_history.append(key)
        return True

    def _reply(self, comment, phrase: str, blog_id: str) -> bool:
        if self.stop_event.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")
        phrase = webdriver_bmp_text(phrase)
        comment_no = self._comment_no(comment)
        if not comment_no:
            raise RuntimeError("답글 대상 댓글 번호를 확인하지 못했습니다.")
        buttons = [
            button
            for button in self._comment_action_elements(
                comment,
                ".u_cbox_btn_reply",
            )
            if button.is_displayed()
        ]
        if not buttons:
            raise RuntimeError("대상 댓글의 답글 버튼을 찾지 못했습니다.")
        self.driver.execute_script("arguments[0].click()", buttons[0])
        self._submit_comment_once(self.driver, phrase, blog_id, comment_no)
        return True

    @staticmethod
    def _comment_key(comment, url: str) -> str:
        comment_no = NaverAutomation._comment_no(comment)
        if comment_no:
            return f"{url}|{comment_no}"
        identity = "|".join(
            (comment.get_attribute(attribute) or "")
            for attribute in ("id", "class", "data-info", "data-param", "data-ui-indexes")
        )
        text = (comment.text or "").strip()
        digest = hashlib.sha1(f"{identity}|{text}".encode("utf-8")).hexdigest()[:16]
        return f"{url}|{digest}"

    @classmethod
    def _comment_elements(cls, driver, selector: str, comment_no: str = ""):
        # Exclude other reply forms and reacquire the root after every render.
        root = cls._top_level_comment_by_no(driver, comment_no) if comment_no else driver
        if root is None:
            return []
        elements = []
        for element in root.find_elements(By.CSS_SELECTOR, selector):
            owner = driver.execute_script(
                "return arguments[0].closest('li.u_cbox_comment')", element
            )
            if (comment_no and owner == root) or (not comment_no and owner is None):
                elements.append(element)
        return elements

    @classmethod
    def _visible_comment_editor(cls, driver, comment_no: str = ""):
        selectors = (
            ".u_cbox_write_area textarea.u_cbox_text, "
            ".u_cbox_write_box textarea.u_cbox_text, "
            ".u_cbox_write_area [contenteditable='true'].u_cbox_text, "
            ".u_cbox_write_box [contenteditable='true'].u_cbox_text, "
            ".u_cbox_write_area [contenteditable='true'][role='textbox'], "
            ".u_cbox_write_box [contenteditable='true'][role='textbox'], "
            ".u_cbox_write_area textarea, "
            ".u_cbox_write_box textarea"
        )
        return next(
            (
                element
                for element in cls._comment_elements(driver, selectors, comment_no)
                if element.is_displayed() and element.is_enabled()
            ),
            None,
        )

    @classmethod
    def _activate_comment_editor(cls, driver, comment_no: str = ""):
        # A visible contenteditable can still be inactive behind Naver's guide.
        # Fire the guide's activation event before returning even a visible editor.
        launchers = cls._comment_elements(
            driver,
            ".u_cbox_write_box .u_cbox_guide, "
            ".u_cbox_write_area .u_cbox_guide",
            comment_no,
        )
        for launcher in launchers:
            try:
                if not launcher.is_displayed():
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'})",
                    launcher,
                )
                driver.execute_script("arguments[0].click()", launcher)
                break
            except StaleElementReferenceException:
                continue
        return cls._visible_comment_editor(driver, comment_no)

    @staticmethod
    def _comment_editor_value(editor) -> str:
        attribute = "value" if editor.tag_name.lower() in {"textarea", "input"} else "textContent"
        return " ".join(str(editor.get_property(attribute) or "").split())

    @classmethod
    def _fill_comment_editor(cls, driver, phrase: str, comment_no: str = ""):
        phrase = webdriver_bmp_text(phrase)
        expected = " ".join(phrase.split())
        for attempt in range(3):
            try:
                editor = WebDriverWait(
                    driver, 12, ignored_exceptions=(StaleElementReferenceException,)
                ).until(lambda active: cls._activate_comment_editor(active, comment_no))
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].focus(); arguments[0].click();", editor
                )
                try:
                    editor.send_keys(cls.EDITOR_MODIFIER, "a")
                    editor.send_keys(Keys.BACKSPACE)
                    editor.send_keys(phrase)
                except StaleElementReferenceException:
                    raise
                except WebDriverException:
                    # Use the same input/change events when native typing fails.
                    pass
                if cls._comment_editor_value(editor) != expected:
                    driver.execute_script(
                        """
                        const editor = arguments[0], value = arguments[1];
                        editor.focus();
                        if (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT') {
                          const proto = editor.tagName === 'TEXTAREA'
                            ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                          Object.getOwnPropertyDescriptor(proto, 'value').set.call(editor, value);
                        } else {
                          editor.textContent = value;
                        }
                        editor.dispatchEvent(new InputEvent('input', {
                          bubbles: true, inputType: 'insertText', data: value
                        }));
                        editor.dispatchEvent(new Event('change', {bubbles: true}));
                        """, editor, phrase
                    )
                # Input handlers can replace the editor or reject/truncate text.
                editor = cls._visible_comment_editor(driver, comment_no)
                if editor is None or cls._comment_editor_value(editor) != expected:
                    raise RuntimeError("댓글 문구가 입력창에 정확히 입력되지 않아 등록하지 않았습니다.")
                return editor
            except StaleElementReferenceException:
                if attempt == 2:
                    raise RuntimeError("댓글 입력창이 계속 갱신되어 입력하지 못했습니다.")
            except TimeoutException as exc:
                raise RuntimeError("댓글 입력창을 활성화하지 못했습니다.") from exc

    @classmethod
    def _visible_comment_upload(cls, driver, comment_no: str = ""):
        editor = cls._visible_comment_editor(driver, comment_no)
        if editor is None:
            return None
        # The upload is a sibling of write_area in current Naver write_box.
        form = driver.execute_script(
            "return arguments[0].closest('.u_cbox_write_box') || "
            "arguments[0].closest('.u_cbox_write_area')", editor
        )
        if form is None:
            return None
        return next(
            (
                element
                for element in form.find_elements(By.CSS_SELECTOR, ".u_cbox_btn_upload")
                if element.is_displayed() and element.is_enabled()
                and element.get_attribute("aria-disabled") != "true"
                and not re.search(r"(?:^|[\s_-])(?:disabled|disable)(?:$|[\s_-])",
                                  element.get_attribute("class") or "")
            ),
            None,
        )

    @staticmethod
    def _comment_records(driver, comment_no: str = "") -> list[dict]:
        return driver.execute_script(
            r"""
            const parentNo = String(arguments[0] || '');
            const number = item => {
              const identity = ['id','class','data-param','data-ui-indexes','data-info']
                .map(key => item.getAttribute(key) || '').join('|');
              for (const pattern of [/commentNo['"]?\s*[:=-]\s*['"]?(\d+)/i,
                                     /idx-commentNo-(\d+)/, /__comment_(\d+)/]) {
                const match = identity.match(pattern);
                if (match) return match[1];
              }
              return '';
            };
            const all = [...document.querySelectorAll('li.u_cbox_comment')];
            const roots = all.filter(item => !item.parentElement.closest('li.u_cbox_comment'));
            const parent = parentNo ? roots.find(item => number(item) === parentNo) : null;
            const items = parentNo
              ? (parent ? all.filter(item => item.parentElement.closest('li.u_cbox_comment') === parent) : [])
              : roots;
            return items.filter(item => item.getClientRects().length).map(item => {
              const own = selector => [...item.querySelectorAll(selector)].find(
                el => el.closest('li.u_cbox_comment') === item);
              return {id: number(item),
                      author: own('a.u_cbox_name')?.href || '',
                      text: own('.u_cbox_contents')?.textContent || ''};
            });
            """, comment_no
        ) or []

    @staticmethod
    def _comment_author_matches(href: str, blog_id: str) -> bool:
        parsed = urllib.parse.urlparse(href)
        if parsed.hostname not in {"blog.naver.com", "m.blog.naver.com"}:
            return False
        query = urllib.parse.parse_qs(parsed.query)
        author = query.get("blogId", [""])[0] or parsed.path.strip("/").split("/")[0]
        return bool(blog_id) and author.casefold() == blog_id.casefold()

    @classmethod
    def _new_comment_record(cls, driver, before: set[str], phrase: str,
                            blog_id: str, comment_no: str = ""):
        for record in cls._comment_records(driver, comment_no):
            if (record.get("id") and record["id"] not in before
                    and cls._comment_author_matches(record.get("author", ""), blog_id)
                    and " ".join(record.get("text", "").split()) == " ".join(phrase.split())):
                return record
        return None

    @classmethod
    def _submit_comment(cls, driver, phrase: str, blog_id: str, comment_no: str = "", *,
                        stop_event=None, before_submit=None):
        def check_stop():
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("사용자가 작업을 중지했습니다. 댓글을 등록하지 않았습니다.")
        check_stop()
        if not blog_id.strip():
            raise RuntimeError("댓글 작성자를 확인할 네이버 블로그 ID가 필요합니다.")
        phrase = webdriver_bmp_text(phrase)
        cls._fill_comment_editor(driver, phrase, comment_no)
        check_stop()
        before = {record["id"] for record in cls._comment_records(driver, comment_no)}
        try:
            upload = WebDriverWait(
                driver, 10, ignored_exceptions=(StaleElementReferenceException,)
            ).until(lambda active: cls._visible_comment_upload(active, comment_no))
        except TimeoutException as exc:
            raise RuntimeError("댓글을 입력했지만 등록 버튼이 활성화되지 않았습니다.") from exc
        editor = cls._visible_comment_editor(driver, comment_no)
        if editor is None or cls._comment_editor_value(editor) != " ".join(phrase.split()):
            raise RuntimeError("등록 직전 댓글 내용이 달라져 등록하지 않았습니다.")
        check_stop()
        if before_submit is not None:
            before_submit(before)
        # Click exactly once. Never retry submission after an uncertain response.
        try:
            driver.execute_script("arguments[0].click()", upload)
            return WebDriverWait(driver, 20).until(
                lambda active: cls._new_comment_record(active, before, phrase, blog_id, comment_no)
            )
        except WebDriverException as exc:
            raise RuntimeError(
                "등록을 시도했지만 새 댓글 번호·작성자·내용을 확인하지 못했습니다. "
                "중복 방지를 위해 재등록하지 않습니다. 해당 글에서 등록 여부를 확인하세요."
            ) from exc

    @staticmethod
    def _comment_target_url(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname in {"blog.naver.com", "m.blog.naver.com"}:
            path = urllib.parse.unquote(parsed.path).strip("/")
            direct = re.fullmatch(r"([^/]+)/(\d+)", path)
            query = urllib.parse.parse_qs(parsed.query)
            author = direct[1] if direct else (query.get("blogId") or [""])[0]
            number = direct[2] if direct else (query.get("logNo") or [""])[0]
            if author and re.fullmatch(r"\d+", number):
                return f"https://blog.naver.com/{author.lower()}/{number}"
        # Local fixture pages have no account data. Avoid retaining a data URL's
        # complete HTML in the small submission receipt.
        return "page-sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _submit_comment_once(self, driver, phrase: str, blog_id: str, comment_no: str = ""):
        """Resume read-only confirmation after an uncertain click, including after restart."""
        if self.stop_event.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")
        phrase = webdriver_bmp_text(phrase)
        target = self._comment_target_url(str(driver.current_url or ""))
        identity = {"target": target, "author": blog_id.casefold(), "parent_comment": comment_no}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        path = self.data_dir / "comment_receipts" / f"{key}.json"
        if path.exists():
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(prior, dict) or prior.get("identity") != identity
                        or prior.get("status") not in {"confirmed", "uncertain"}
                        or not isinstance(prior.get("phrase"), str) or not isinstance(prior.get("before_ids"), list)
                        or any(not isinstance(value, str) for value in prior["before_ids"])):
                    raise ValueError("Invalid comment receipt")
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError("이전 댓글 등록 기록을 읽을 수 없어 중복 등록을 중단했습니다.") from exc
            if prior["status"] == "confirmed":
                return {**prior.get("record", {}), "reused_receipt": True}
            record = self._new_comment_record(driver, set(prior["before_ids"]), prior["phrase"], blog_id, comment_no)
            if record:
                prior.update(status="confirmed", record=record)
                self._write_publication_receipt(path, prior)
                return {**record, "reused_receipt": True}
            raise RuntimeError("이전 댓글 등록 결과가 아직 확인되지 않았습니다. 새 문구로 재등록하지 않고 해당 글을 보존합니다.")
        receipt = {"version": 1, "identity": identity, "status": "uncertain", "phrase": phrase,
                   "submitted_at": datetime.now().isoformat(timespec="seconds")}
        def claim(before):
            receipt["before_ids"] = sorted(before)
            if not self._claim_publication_receipt(path, receipt):
                raise RuntimeError("다른 실행의 댓글 제출 기록이 있어 등록 버튼을 누르지 않았습니다.")
        record = self._submit_comment(driver, phrase, blog_id, comment_no,
                                      stop_event=self.stop_event, before_submit=claim)
        receipt.update(status="confirmed", record=record)
        self._write_publication_receipt(path, receipt)
        return record

    @classmethod
    def _write_neighbor_comment(cls, driver, phrase: str, blog_id: str) -> None:
        cls._submit_comment(driver, phrase, blog_id)

    def run_own_posts(self, blog_id: str, days: int, interval: int, do_like: bool = True):
        if self.stop_event.is_set():
            self.log("중지된 댓글 작업을 시작하지 않았습니다.")
            return
        driver = self._require_naver_login(self._driver())
        driver.get("https://section.blog.naver.com/BlogHome.naver")
        if False and not any(
            cookie.get("name") in {"NID_SES", "NID_AUT"}
            for cookie in driver.get_cookies()
        ):
            raise RuntimeError(
                "네이버 로그인이 필요합니다. 먼저 '네이버 웨일 로그인 창 열기'를 눌러 로그인하세요."
            )
        urls = recent_post_urls(blog_id, days)
        self.log(f"최근 {days}일 글 {len(urls)}개를 확인합니다.")
        replied = skipped = liked = 0
        for post_index, url in enumerate(urls, 1):
            if self.stop_event.is_set():
                break
            driver.get(url)
            self._switch_to_post_frame(driver)
            self.log(f"[{post_index}/{len(urls)}] 댓글 확인 중: {driver.title[:45]}")
            if not self._open_comments(driver):
                continue
            self._load_all_comments(driver)
            comments = self._top_level_comments(driver)
            comment_nos = []
            for comment in comments:
                try:
                    comment_no = self._comment_no(comment)
                except StaleElementReferenceException:
                    continue
                if comment_no and comment_no not in comment_nos:
                    comment_nos.append(comment_no)
            for comment_no in comment_nos:
                if self.stop_event.is_set():
                    break
                for stale_attempt in range(3):
                    try:
                        comment = self._top_level_comment_by_no(
                            driver,
                            comment_no,
                        )
                        if not comment:
                            self.log(
                                f"  댓글 {comment_no} 재탐색 실패 · 건너뜁니다."
                            )
                            break
                        key = f"{url}|{comment_no}"
                        if do_like:
                            try:
                                if self._like_comment(comment, key):
                                    liked += 1
                            except StaleElementReferenceException:
                                raise
                            except Exception as exc:
                                self.log(
                                    f"  하트 실패: {str(exc)[:100]}"
                                )

                        # A vote or a preceding reply can rebuild the entire
                        # comment list. Never keep using the old WebElement.
                        comment = self._top_level_comment_by_no(
                            driver,
                            comment_no,
                        )
                        if not comment:
                            raise StaleElementReferenceException(
                                "댓글 목록이 갱신되어 요소를 다시 찾습니다."
                            )
                        if self._own_reply_exists(comment, blog_id):
                            skipped += 1
                            break

                        # History is only a cache. The live page is
                        # authoritative because older versions stored the
                        # service id "201" instead of the real comment id.
                        if key in self.state.get("replied", []):
                            self.state["replied"].remove(key)
                        phrase = webdriver_bmp_text(random.choice(THANKS))
                        try:
                            if self._reply(comment, phrase, blog_id):
                                replied += 1
                                replied_history = self.state.setdefault(
                                    "replied",
                                    [],
                                )
                                if key not in replied_history:
                                    replied_history.append(key)
                                _save(self.state_file, self.state)
                                self.log(f"  답글 완료: {phrase}")
                                if (
                                    interval > 0
                                    and self.stop_event.wait(interval)
                                ):
                                    break
                        except StaleElementReferenceException:
                            raise
                        except Exception as exc:
                            self.log(f"  답글 실패: {str(exc)[:100]}")
                        break
                    except StaleElementReferenceException:
                        if stale_attempt >= 2:
                            self.log(
                                f"  댓글 {comment_no} 화면 갱신 반복 · "
                                "다음 댓글로 이동합니다."
                            )
                            break
                        time.sleep(0.4)
            _save(self.state_file, self.state)
        self.log(f"내 글 작업 완료 · 답글 {replied} · 기존 답글 스킵 {skipped} · 하트 {liked}")

    def _neighbor_urls(self, driver, own_blog_id: str, maximum: int) -> list[str]:
        driver = self._require_naver_login(driver)
        driver.get("https://section.blog.naver.com/BlogHome.naver")
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        if False and not any(
            cookie.get("name") in {"NID_SES", "NID_AUT"}
            for cookie in driver.get_cookies()
        ):
            raise RuntimeError(
                "네이버 로그인이 필요합니다. 먼저 '네이버 웨일 로그인 창 열기'를 눌러 로그인하세요."
            )
        output: list[str] = []
        total = maximum
        page = 1
        while len(output) < maximum and (page - 1) * 10 < total:
            if self.stop_event.is_set():
                break
            payload = driver.execute_async_script(
                """
                const page = arguments[0];
                const done = arguments[arguments.length - 1];
                fetch('/ajax/BuddyPostList.naver?page=' + page + '&groupId=0',
                      {credentials: 'include'})
                    .then(response => response.text())
                    .then(done)
                    .catch(error => done('ERROR:' + String(error)));
                """,
                page,
            )
            if not isinstance(payload, str) or payload.startswith("ERROR:"):
                raise RuntimeError("네이버 전체이웃 새글 목록을 불러오지 못했습니다.")
            cleaned = payload.lstrip()
            if cleaned.startswith(")]}',"):
                cleaned = cleaned.split("\n", 1)[1]
            data = json.loads(cleaned)
            result = data.get("result", {})
            posts = result.get("buddyPostList", [])
            total = int(result.get("buddyPostTotalCount", len(posts)) or 0)
            if page == 1:
                self.log(
                    f"전체이웃 새글 중 최대 {maximum}개 후보를 확인합니다 "
                    f"(전체 목록 {total}개)."
                )
            if not posts:
                break
            for post in posts:
                href = str(post.get("postUrl", "")).strip()
                blog = str(post.get("domainIdOrBlogId", "")).strip()
                log_no = str(post.get("logNo", "")).strip()
                if not blog or not log_no:
                    path_match = re.search(
                        r"blog\.naver\.com/([^/?#]+)/(\d{10,})", href
                    )
                    if path_match:
                        blog, log_no = path_match.groups()
                if blog and log_no:
                    href = (
                        "https://blog.naver.com/PostView.naver"
                        f"?blogId={blog}&logNo={log_no}"
                    )
                if not href or own_blog_id.lower() in href.lower() or href in output:
                    continue
                output.append(href)
                if len(output) >= maximum:
                    break
            page += 1
        if not output:
            self.log("전체이웃 새글이 없거나 이웃 목록을 불러올 수 없습니다.")
        return output

    def run_neighbor_posts(self, blog_id: str, interval: int, maximum: int):
        if self.stop_event.is_set():
            self.log("중지된 이웃 댓글 작업을 시작하지 않았습니다.")
            return
        driver = self._driver()
        urls = self._neighbor_urls(driver, blog_id, maximum * 3)
        done = skipped = 0
        for url in urls:
            if done >= maximum or self.stop_event.is_set():
                break
            match = re.search(r"(?:logNo=|/)(\d{10,})", url)
            log_no = match.group(1) if match else url
            if log_no in self.state.get("neighbor_commented", []):
                skipped += 1
                continue
            driver.get(url)
            self._switch_to_post_frame(driver)
            self.log(f"이웃 새글 확인: {driver.title[:55]}")
            if not self._open_comments(driver):
                self.log("  댓글 영역을 찾지 못해 건너뜁니다.")
                continue
            phrase = webdriver_bmp_text(random.choice(NEIGHBOR_COMMENTS))
            try:
                self._submit_comment_once(driver, phrase, blog_id)
                done += 1
                self.state.setdefault("neighbor_commented", []).append(log_no)
                _save(self.state_file, self.state)
                self.log(f"  이웃 댓글 완료: {phrase}")
                if interval > 0 and self.stop_event.wait(interval):
                    break
            except Exception as exc:
                self.log(f"  작성 실패: {str(exc)[:100]}")
        self.log(f"이웃 새글 작업 완료 · 작성 {done} · 중복 스킵 {skipped}")
