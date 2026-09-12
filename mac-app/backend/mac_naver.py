"""macOS lifecycle adapter for the shared, verified Naver publication engine.

Electron owns a dedicated persistent Whale profile. This worker attaches only
to that instance; it never copies cookies, launches a second profile, or closes
the user's browser. All publication and image checks stay in the shared engine.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from naver_automation import NaverAutomation


_CFT = "https://googlechromelabs.github.io/chrome-for-testing/"
_DOWNLOAD_HOST = "storage.googleapis.com"
_MAX_ARCHIVE_BYTES = 40 * 1024 * 1024
_MAX_DRIVER_BYTES = 90 * 1024 * 1024


def _arm64_macho(payload: bytes) -> bool:
    """Check Mach-O CPU type before making a downloaded file executable."""
    if len(payload) < 8:
        return False
    if payload[:4] == b"\xcf\xfa\xed\xfe":
        return struct.unpack_from("<I", payload, 4)[0] == 0x0100000C
    if payload[:4] == b"\xfe\xed\xfa\xcf":
        return struct.unpack_from(">I", payload, 4)[0] == 0x0100000C
    fat = {
        b"\xca\xfe\xba\xbe": (">", 20), b"\xbe\xba\xfe\xca": ("<", 20),
        b"\xca\xfe\xba\xbf": (">", 32), b"\xbf\xba\xfe\xca": ("<", 32),
    }.get(payload[:4])
    if not fat:
        return False
    endian, entry_size = fat
    count = struct.unpack_from(endian + "I", payload, 4)[0]
    if not 1 <= count <= 32 or len(payload) < 8 + count * entry_size:
        return False
    for index in range(count):
        position = 8 + index * entry_size
        cpu = struct.unpack_from(endian + "I", payload, position)[0]
        width = "Q" if entry_size == 32 else "I"
        offset, size = struct.unpack_from(endian + width * 2, payload, position + 8)
        if cpu == 0x0100000C and size >= 8 and offset >= 8 + count * entry_size:
            if offset + size <= len(payload):
                inner = payload[offset:offset + 8]
                inner_endian = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">"}.get(inner[:4])
                if inner_endian and struct.unpack_from(inner_endian + "I", inner, 4)[0] == 0x0100000C:
                    return True
    return False


class MacNaverAutomation(NaverAutomation):
    EDITOR_MODIFIER = Keys.COMMAND

    def __init__(self, data_dir: Path, log, debug_port: int = 9339):
        super().__init__(Path(data_dir), log)
        if isinstance(debug_port, bool) or not isinstance(debug_port, int) or not 1024 <= debug_port <= 65535:
            raise ValueError("Whale 로컬 연결 포트가 올바르지 않습니다.")
        self.debug_port = debug_port
        self.attached_existing_whale = True
        self._driver_lock = threading.RLock()

    @staticmethod
    def _supports_arm64() -> bool:
        if sys.platform != "darwin":
            return False
        if platform.machine().lower() in {"arm64", "aarch64"}:
            return True
        # A GUI launched under Rosetta still runs the native driver on M1.
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return result.returncode == 0 and result.stdout.strip() == "1"
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _bounded_get(url: str, limit: int, timeout=(5, 30)) -> bytes:
        started = time.monotonic()
        response = requests.get(url, timeout=timeout, stream=True, allow_redirects=False)
        try:
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError("자동화 드라이버 다운로드 응답이 올바르지 않습니다.")
            try:
                declared = int(response.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                declared = 0
            if declared > limit:
                raise RuntimeError("자동화 드라이버 다운로드 크기 제한을 초과했습니다.")
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() - started > 90:
                    raise RuntimeError("자동화 드라이버 다운로드 제한 시간을 초과했습니다.")
                payload.extend(chunk)
                if len(payload) > limit:
                    raise RuntimeError("자동화 드라이버 다운로드 크기 제한을 초과했습니다.")
            return bytes(payload)
        finally:
            response.close()

    def _cache_path(self, name: str) -> Path:
        base = self.data_dir.resolve()
        directory = base / "webdrivers"
        if directory.is_symlink():
            raise RuntimeError("드라이버 캐시 경로에 외부 링크를 사용할 수 없습니다.")
        directory.mkdir(parents=True, exist_ok=True)
        if directory.resolve().parent != base:
            raise RuntimeError("드라이버 캐시 경로가 앱 데이터 폴더 밖입니다.")
        candidate = directory / name
        if candidate.is_symlink() or candidate.resolve().parent != directory.resolve():
            raise RuntimeError("드라이버 파일 경로가 캐시 폴더 밖입니다.")
        return candidate

    @staticmethod
    def _valid_download_url(url: str, major: str) -> bool:
        try:
            parsed = urllib.parse.urlsplit(url)
            return bool(
                parsed.scheme == "https" and parsed.hostname == _DOWNLOAD_HOST
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and not parsed.query and not parsed.fragment
                and re.fullmatch(
                    rf"/chrome-for-testing-public/{re.escape(major)}\.\d+\.\d+\.\d+/mac-arm64/chromedriver-mac-arm64\.zip",
                    parsed.path,
                )
            )
        except ValueError:
            return False

    def _ensure_chromedriver(self, chrome_version: str) -> Path:
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", chrome_version):
            raise RuntimeError("Whale의 Chromium 버전을 확인하지 못했습니다.")
        major = chrome_version.split(".", 1)[0]
        target = self._cache_path(f"chromedriver-{major}-mac-arm64")
        receipt = self._cache_path(f"chromedriver-{major}-mac-arm64.json")
        if target.is_file() and target.stat().st_size <= _MAX_DRIVER_BYTES:
            try:
                content = target.read_bytes()
                record = json.loads(receipt.read_text(encoding="utf-8"))
                if (
                    _arm64_macho(content)
                    and record.get("sha256") == hashlib.sha256(content).hexdigest()
                    and self._driver_major(target) == major
                ):
                    return target
            except (OSError, ValueError):
                pass

        self.log(f"Mac M1용 Chromium {chrome_version} 드라이버를 준비합니다.")
        build = ".".join(chrome_version.split(".")[:3])
        lookups = [
            ("latest-patch-versions-per-build-with-downloads.json", "builds", build),
            ("latest-versions-per-milestone-with-downloads.json", "milestones", major),
        ]
        download = ""
        for filename, collection, key in lookups:
            try:
                metadata = json.loads(self._bounded_get(_CFT + filename, 8 * 1024 * 1024))
                choices = metadata.get(collection, {}).get(key, {}).get("downloads", {}).get("chromedriver", [])
                download = next((str(item.get("url", "")) for item in choices
                                 if item.get("platform") == "mac-arm64"
                                 and self._valid_download_url(str(item.get("url", "")), major)), "")
                if download:
                    break
            except (requests.RequestException, ValueError, AttributeError, RuntimeError):
                continue
        if not download:
            raise RuntimeError(f"Whale Chromium {chrome_version}용 Mac M1 드라이버가 아직 없습니다. Whale 업데이트 후 다시 시도해 주세요.")
        payload = self._bounded_get(download, _MAX_ARCHIVE_BYTES, timeout=(5, 45))
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = [entry for entry in archive.infolist()
                           if entry.filename == "chromedriver-mac-arm64/chromedriver"]
                if len(members) != 1 or not 8 <= members[0].file_size <= _MAX_DRIVER_BYTES:
                    raise RuntimeError("Mac M1 드라이버 압축 내용이 올바르지 않습니다.")
                entry = members[0]
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("드라이버 압축 파일에 실행 파일 대신 링크가 있습니다.")
                content = archive.read(entry)
        except (zipfile.BadZipFile, KeyError) as exc:
            raise RuntimeError("Mac M1 드라이버 압축 파일을 읽지 못했습니다.") from exc
        if not _arm64_macho(content):
            raise RuntimeError("다운로드한 드라이버가 Mac M1 ARM64 실행 파일이 아닙니다.")
        temporary = self._cache_path(f".chromedriver-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            temporary.chmod(0o700)
            if self._driver_major(temporary) != major:
                raise RuntimeError("Whale과 Mac M1 드라이버 버전이 일치하지 않습니다.")
            os.replace(temporary, target)
            record = {"version": chrome_version, "platform": "mac-arm64",
                      "sha256": hashlib.sha256(content).hexdigest(), "download_url": download}
            receipt_tmp = self._cache_path(f".driver-receipt-{uuid.uuid4().hex}.tmp")
            try:
                receipt_tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                os.replace(receipt_tmp, receipt)
            finally:
                receipt_tmp.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _debug_browser_version(self) -> str:
        try:
            info = json.loads(self._bounded_get(
                f"http://127.0.0.1:{self.debug_port}/json/version", 64 * 1024, timeout=(2, 5)))
            endpoint = urllib.parse.urlsplit(str(info.get("webSocketDebuggerUrl", "")))
            if (endpoint.scheme != "ws" or endpoint.hostname not in {"127.0.0.1", "localhost"}
                    or endpoint.port != self.debug_port or endpoint.username or endpoint.password
                    or not endpoint.path.startswith("/devtools/browser/")):
                raise ValueError("Unexpected debug endpoint")
            match = re.search(r"(?:Headless)?Chrome/(\d+\.\d+\.\d+\.\d+)", str(info.get("Browser", "")))
            if not match:
                raise ValueError("Missing Chromium version")
            return match.group(1)
        except (requests.RequestException, ValueError, AttributeError, RuntimeError) as exc:
            raise RuntimeError("Blog 전용 Whale 연결을 확인하지 못했습니다. 앱의 ‘네이버 로그인’을 열고 다시 시도해 주세요.") from exc

    def _driver(self):
        with self._driver_lock:
            if self.driver:
                try:
                    _ = self.driver.current_window_handle
                    return self.driver
                except Exception:
                    self.close()
            if not self._supports_arm64():
                raise RuntimeError("이 Blog 버전은 Apple Silicon(M1 이상) Mac에서 실행해 주세요.")
            version = self._debug_browser_version()
            driver_path = self._ensure_chromedriver(version)
            options = webdriver.ChromeOptions()
            options.debugger_address = f"127.0.0.1:{self.debug_port}"
            options.add_experimental_option("detach", True)
            service = Service(str(driver_path))
            try:
                attached = webdriver.Chrome(service=service, options=options)
                attached.set_page_load_timeout(60)
                attached.set_script_timeout(30)
            except Exception:
                service.stop()
                raise
            self.driver = attached
            self.attached_existing_whale = True
            self.log(f"Mac 전용 Whale 연결 완료 · Chromium {version} · ARM64")
            return attached

    def close(self):
        with self._driver_lock:
            driver, self.driver = self.driver, None
            if driver:
                # Attached ChromeDriver's quit deletes only its WebDriver session.
                # Never issue Browser.close or terminate Electron's Whale process.
                try:
                    driver.quit()
                except Exception:
                    try:
                        driver.service.stop()
                    except Exception:
                        pass
            self.whale_process = None
            self.attached_existing_whale = True

    def _stop_whale_process(self):
        """The persistent browser belongs to Electron, not this worker."""
        self.whale_process = None

    def _import_existing_naver_session(self, _target_driver) -> bool:
        return False

    def _has_naver_session(self, driver) -> bool:
        driver.switch_to.default_content()
        driver.get("https://blog.naver.com/MyBlog.naver")

        def authenticated(active):
            url = urllib.parse.urlsplit(str(active.current_url or ""))
            if url.hostname not in {"blog.naver.com", "section.blog.naver.com"}:
                return False
            if "login" in url.path.lower() or not self._naver_logged_in(active):
                return False
            def logged_out_link():
                return bool(active.execute_script("""
                if (document.readyState !== 'complete') return false;
                if (document.querySelector('input[name="passwd"], input#pw')) return false;
                return [...document.querySelectorAll('a[href]')].some(a => {
                    try {
                        const u = new URL(a.href, location.href);
                        return u.hostname === 'nid.naver.com' &&
                            (u.pathname.includes('nidlogin.logout') || u.searchParams.get('mode') === 'logout');
                    } catch (_) { return false; }
                });
                """))
            return bool(self._find_across_frames(active, logged_out_link))
        try:
            return bool(WebDriverWait(driver, 12).until(authenticated))
        except TimeoutException:
            return False
        finally:
            driver.switch_to.default_content()

    def _require_naver_login(self, driver):
        if self._has_naver_session(driver):
            return driver
        raise RuntimeError("네이버 로그인이 필요하거나 만료되었습니다. Blog의 ‘네이버 로그인’에서 전용 Whale 창에 로그인한 뒤 ‘연결 확인’을 눌러 주세요. 로그인 정보는 이 전용 프로필에 유지됩니다.")

    def check_login(self) -> dict:
        active = self._has_naver_session(self._driver())
        return {"authenticated": active, "provider": "naver", "persistent_profile": True,
                "detail": "네이버 로그인 확인 완료" if active else "Blog의 네이버 로그인 버튼에서 전용 Whale 창에 로그인해 주세요."}

    @staticmethod
    def _copy_image_to_windows_clipboard(image_path: str) -> None:
        # Keep the inherited hook name to share the same upload/fallback code.
        try:
            from AppKit import NSImage, NSPasteboard
            from Foundation import NSData
        except ImportError as exc:
            raise RuntimeError("Mac 이미지 붙여넣기 구성 요소가 없습니다. Blog 앱을 다시 설치해 주세요.") from exc
        source = Path(image_path).resolve()
        if not source.is_file():
            raise RuntimeError("붙여넣을 이미지 파일이 없습니다.")
        data = NSData.dataWithContentsOfFile_(str(source))
        image = NSImage.alloc().initWithData_(data) if data is not None else None
        if image is None:
            raise RuntimeError("Mac 이미지 클립보드에 넣을 사진을 읽지 못했습니다.")
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        if not pasteboard.writeObjects_([image]):
            raise RuntimeError("Mac 이미지 클립보드에 사진을 복사하지 못했습니다.")
