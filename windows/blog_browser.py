"""Owned Windows browser sessions for independent Naver blog accounts.

No normal browser profile is attached, copied, decrypted, or imported. Driver
discovery is Selenium Manager's official installed-browser path, with a cache
inside this account's application directory. The existing Whale adapter remains
available unchanged to macOS and legacy callers.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable
from urllib.parse import parse_qs, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.selenium_manager import SeleniumManager
from selenium.webdriver.support.ui import WebDriverWait

from naver_automation import NaverAutomation


BROWSER_NAMES = {"whale": "네이버 웨일", "edge": "Microsoft Edge", "chrome": "Google Chrome"}


def normalize_browser(browser: str) -> str:
    value = str(browser or "whale").strip().lower()
    if value not in BROWSER_NAMES:
        raise ValueError("브라우저는 whale, edge, chrome 중에서 선택해 주세요.")
    return value


def account_browser_dir(data_dir: Path, account_id: str, browser: str) -> Path:
    """A stable account key never becomes a caller-controlled filesystem path."""
    browser = normalize_browser(browser)
    root = Path(data_dir).resolve()
    if not account_id:
        return root
    identity = str(account_id).strip()
    if not identity:
        raise ValueError("계정 식별자가 비어 있습니다.")
    slug = re.sub(r"[^a-z0-9_-]+", "-", identity.lower()).strip("-")[:32] or "account"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return root / "browser-accounts" / f"{slug}-{digest}" / browser


def _blog_id_from_url(url: str, *, writer_only: bool = False) -> str:
    try:
        parsed = urlparse(str(url))
        if (parsed.scheme != "https" or parsed.hostname not in {"blog.naver.com", "m.blog.naver.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            return ""
        path = parsed.path.rstrip("/")
        if writer_only:
            match = re.fullmatch(r"/([A-Za-z0-9_.-]{2,50})/postwrite", path, re.I)
            query_paths = {"/postwriteform.naver"}
        else:
            match = re.fullmatch(r"/([A-Za-z0-9_.-]{2,50})(?:/(?:\d+|postwrite))?", path, re.I)
            query_paths = {"/postlist.naver", "/postview.naver", "/postwriteform.naver",
                           "/blogprofile.naver", "/prologue/prologuelist.naver"}
        if match and not match[1].lower().endswith(".naver"):
            return match[1].lower()
        if path.lower() in query_paths:
            values = [value for key, entries in parse_qs(parsed.query).items()
                      if key.lower() == "blogid" for value in entries]
            if len(values) == 1 and re.fullmatch(r"[A-Za-z0-9_.-]{2,50}", values[0]):
                return values[0].lower()
    except (TypeError, ValueError):
        pass
    return ""


def _own_blog_links(driver) -> list[str]:
    """Read visible account navigation, never arbitrary blog/post links."""
    output = []
    for element in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
        try:
            labels = [element.text, element.get_attribute("aria-label"), element.get_attribute("title")]
            if (element.is_displayed() and any(re.sub(r"\s+", "", str(label or "")) == "내블로그"
                                               for label in labels)):
                href = str(element.get_attribute("href") or "")
                if href not in output:
                    output.append(href)
        except Exception:
            # A stale navigation item is not proof of a logged-in identity.
            continue
    return output


def _is_own_blog_redirect(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.hostname == "blog.naver.com"
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and parsed.path.lower() == "/myblog.naver" and not parsed.query)
    except ValueError:
        return False


def assert_blog_account_target(driver, blog_id: str) -> dict:
    """Resolve the logged-in user's own-blog navigation and compare its ID.

    Call on the authenticated BlogHome page, before opening/uploading a draft.
    An observed MyBlog.naver redirect may be followed read-only; unrelated URLs
    are never followed. No password or cookie contents enter the return value.
    """
    target = NaverAutomation._clean_blog_id(blog_id).lower()
    driver.switch_to.default_content()
    page = urlparse(str(driver.current_url or ""))
    if (page.scheme != "https" or page.hostname != "section.blog.naver.com"
            or page.path.lower() != "/bloghome.naver" or page.username or page.password
            or not NaverAutomation._naver_logged_in(driver)):
        raise RuntimeError("네이버 로그인 홈에서 계정 정보를 확인한 뒤 다시 실행해 주세요.")
    urls = _own_blog_links(driver)
    observed = {_blog_id_from_url(url) for url in urls} - {""}
    if not observed:
        redirects = [url for url in urls if _is_own_blog_redirect(url)]
        if redirects:
            driver.get(redirects[0])
            try:
                identity = WebDriverWait(driver, 15).until(lambda active: _blog_id_from_url(active.current_url))
                observed.add(identity)
            except Exception as exc:
                raise RuntimeError("로그인 계정의 '내 블로그' 주소를 확인하지 못했습니다. 전용 로그인 창에서 확인해 주세요.") from exc
    if not observed:
        raise RuntimeError("로그인 계정의 '내 블로그' ID를 확인하지 못했습니다. 전용 로그인 창에서 내 블로그를 확인해 주세요.")
    if observed != {target}:
        raise RuntimeError(f"전용 브라우저에 로그인한 블로그({', '.join(sorted(observed))})가 설정된 대상({target})과 다릅니다.")
    return {"authenticated": True, "blog_id": target, "evidence": "visible_own_blog_navigation"}


class BlogBrowser(NaverAutomation):
    def __init__(self, data_dir: Path, logger: Callable[[str], None], browser: str = "whale", *, blog_id: str = ""):
        self.browser = normalize_browser(browser)
        self.browser_name = BROWSER_NAMES[self.browser]
        self.target_blog_id = self._clean_blog_id(blog_id).lower() if blog_id else ""
        root = Path(data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.profile_dir = root / f"naver-{self.browser}-profile"
        self.driver_cache_dir = root / "webdrivers" if self.browser == "whale" else root / "webdrivers" / self.browser
        self._account_evidence = None

        def log(message):
            if self.browser != "whale":
                message = str(message).replace("네이버 웨일", self.browser_name).replace("웨일", self.browser_name)
            logger(message)

        super().__init__(root, log)

    def bind_target_blog(self, blog_id: str) -> str:
        target = self._clean_blog_id(blog_id).lower()
        if self.target_blog_id and target != self.target_blog_id:
            raise ValueError("이 브라우저 계정에 설정된 블로그 ID와 요청한 대상이 다릅니다. 계정 설정을 확인해 주세요.")
        self.target_blog_id = target
        return target

    @staticmethod
    def _find_installed_browser(browser: str) -> Path:
        brand, executable = (("Google/Chrome/Application", "chrome.exe") if browser == "chrome"
                             else ("Microsoft/Edge/Application", "msedge.exe"))
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidate = Path(base) / brand / executable
                if candidate.is_file():
                    return candidate.resolve()
        found = shutil.which(executable)
        if found and Path(found).is_file():
            return Path(found).resolve()
        raise RuntimeError(f"설치된 {BROWSER_NAMES[browser]}를 찾지 못했습니다. 브라우저를 설치하거나 다른 브라우저를 선택해 주세요.")

    def _resolve_driver(self, browser_path: Path) -> Path:
        self._check_owned_paths()
        self.driver_cache_dir.mkdir(parents=True, exist_ok=True)
        result = SeleniumManager().binary_paths([
            "--browser", self.browser, "--browser-path", str(browser_path),
            "--cache-path", str(self.driver_cache_dir), "--avoid-browser-download",
            "--skip-driver-in-path", "--skip-browser-in-path", "--avoid-stats", "--timeout", "45",
        ])
        driver_path = Path(str(result.get("driver_path") or "")).resolve()
        resolved_browser = Path(str(result.get("browser_path") or "")).resolve()
        expected_name = "msedgedriver.exe" if self.browser == "edge" else "chromedriver.exe"
        if (not driver_path.is_file() or driver_path.name.lower() != expected_name
                or not driver_path.is_relative_to(self.driver_cache_dir.resolve())
                or resolved_browser != browser_path.resolve()):
            raise RuntimeError(f"{self.browser_name} 전용 드라이버 경로 또는 설치된 브라우저를 확인하지 못했습니다.")
        return driver_path

    def _check_owned_paths(self):
        if any(not directory.resolve().is_relative_to(self.data_dir.resolve())
               for directory in (self.profile_dir, self.driver_cache_dir)):
            raise RuntimeError("계정 프로필 또는 드라이버 폴더가 계정 전용 저장소 밖을 가리킵니다.")

    def _check_driver_version(self, driver, path: Path) -> None:
        browser_version = str(driver.capabilities.get("browserVersion", ""))
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        match = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", result.stdout or "")
        browser_match = re.match(r"\d+\.\d+\.\d+\.\d+", browser_version)
        if not match or not browser_match or result.returncode:
            raise RuntimeError(f"{self.browser_name}와 드라이버 버전을 확인하지 못했습니다.")
        components = 3 if self.browser == "edge" else 1
        if match[1].split(".")[:components] != browser_match[0].split(".")[:components]:
            raise RuntimeError(f"{self.browser_name}와 전용 드라이버 버전이 맞지 않습니다. 브라우저 업데이트 후 다시 실행해 주세요.")

    def _driver(self):
        if self.stop_event.is_set():
            raise RuntimeError("작업 중지 요청으로 브라우저를 시작하지 않았습니다.")
        self._check_owned_paths()
        if self.browser == "whale":
            return super()._driver()
        if self.driver is not None:
            try:
                _ = self.driver.current_window_handle
                return self.driver
            except Exception:
                self.close()
        browser_path = self._find_installed_browser(self.browser)
        driver_path = self._resolve_driver(browser_path)
        if self.stop_event.is_set():
            raise RuntimeError("작업 중지 요청으로 브라우저를 시작하지 않았습니다.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        options = webdriver.EdgeOptions() if self.browser == "edge" else webdriver.ChromeOptions()
        options.binary_location = str(browser_path)
        for argument in (f"--user-data-dir={self.profile_dir}", "--start-maximized", "--disable-notifications", "--no-first-run"):
            options.add_argument(argument)
        service_class = EdgeService if self.browser == "edge" else ChromeService
        constructor = webdriver.Edge if self.browser == "edge" else webdriver.Chrome
        self.log(f"{self.browser_name} 계정 전용 프로필로 자동화 연결을 준비합니다.")
        try:
            service = service_class(str(driver_path))
            if Path(service.path).resolve() != driver_path.resolve():
                raise RuntimeError("환경 설정의 공용 드라이버가 계정 전용 드라이버를 대체하여 연결을 중단했습니다.")
            self.driver = constructor(service=service, options=options)
            self._check_driver_version(self.driver, driver_path)
            self.driver.set_page_load_timeout(60)
            self.driver.set_script_timeout(30)
        except Exception as exc:
            self.close()
            raise RuntimeError(f"{self.browser_name} 전용 창 연결에 실패했습니다. 같은 계정의 전용 창이 열려 있거나 드라이버 준비에 실패했는지 확인해 주세요.") from exc
        self.log(f"{self.browser_name} 전용 브라우저 연결 완료")
        return self.driver

    def close(self):
        self._account_evidence = None
        if self.browser == "whale":
            return super().close()
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def _import_existing_naver_session(self, _driver):
        raise RuntimeError(f"일반 브라우저의 로그인은 가져오지 않습니다. {self.browser_name} 전용 로그인 창에서 직접 로그인해 주세요.")

    def _require_naver_login(self, driver):
        if self.stop_event.is_set():
            raise RuntimeError("작업 중지 요청으로 로그인 확인을 중단했습니다.")
        if not self._has_naver_session(driver):
            self._account_evidence = None
            raise RuntimeError(f"{self.browser_name} 전용 브라우저에 네이버 로그인이 필요합니다. 해당 계정의 로그인 창에서 로그인해 주세요.")
        if self.target_blog_id:
            self._account_evidence = None
            self._account_evidence = assert_blog_account_target(driver, self.target_blog_id)
        return driver

    def open_login(self, blog_id: str):
        self.bind_target_blog(blog_id)
        driver = self._driver()
        self._account_evidence = None
        driver.get("https://section.blog.naver.com/BlogHome.naver")
        self.log(f"{self.browser_name} 계정 전용 로그인 창을 열었습니다. 대상 블로그 '{self.target_blog_id}'의 계정으로 로그인해 주세요. 로그인은 이 전용 프로필에 저장됩니다.")

    def verify_account(self, blog_id: str = "") -> dict:
        self.bind_target_blog(blog_id or self.target_blog_id)
        self._require_naver_login(self._driver())
        return {**self._account_evidence, "browser": self.browser, "browser_name": self.browser_name}

    def _open_writer(self, driver, blog_id: str, timeout: int = 45):
        target = self.bind_target_blog(blog_id)
        fields = super()._open_writer(driver, target, timeout=timeout)
        driver.switch_to.default_content()
        if _blog_id_from_url(driver.current_url, writer_only=True) != target:
            raise RuntimeError("열린 글쓰기 화면의 블로그 ID가 설정된 계정과 일치하지 않아 원고를 입력하지 않았습니다.")
        # The inherited field finder leaves Selenium in the matching iframe.
        return self._find_editor_fields(driver) or fields

    def _article_ready_to_publish(self, driver, *args, **kwargs) -> bool:
        # Recheck at the inherited publisher's final gate without navigating
        # away from (or modifying) the already populated editor.
        evidence = self._account_evidence or {}
        if (not self.target_blog_id or evidence.get("blog_id") != self.target_blog_id
                or _blog_id_from_url(driver.current_url, writer_only=True) != self.target_blog_id):
            self.log("계정 확인 기록 또는 글쓰기 대상이 달라져 최종 발행을 중단했습니다. 입력된 글은 그대로 유지합니다.")
            return False
        return super()._article_ready_to_publish(driver, *args, **kwargs)

    def publish_naver_article(self, blog_id: str, article: dict, *, publish: bool = True, save_draft: bool = False,
                              allow_quality_draft: bool = False) -> dict:
        target = self.bind_target_blog(blog_id)
        options = {'allow_quality_draft': True} if allow_quality_draft else {}
        return super().publish_naver_article(target, article, publish=publish, save_draft=save_draft, **options)


def create_blog_browser(data_dir: Path, logger: Callable[[str], None], browser: str = "whale", *,
                        account_id: str = "", blog_id: str = "") -> BlogBrowser:
    selected = normalize_browser(browser)
    return BlogBrowser(account_browser_dir(data_dir, account_id, selected), logger, selected, blog_id=blog_id)
