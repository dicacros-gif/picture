"""Offline Mac adapter tests: no Naver account, browser, or publication access."""
import hashlib
import io
import json
import struct
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "windows"))
sys.path.insert(0, str(REPO / "mac-app" / "backend"))

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from mac_naver import MacNaverAutomation, _arm64_macho
from naver_automation import NaverAutomation


def macho(cpu=0x0100000C):
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu) + b"\0" * 128


def archived(payload=None, filename="chromedriver-mac-arm64/chromedriver"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(filename, macho() if payload is None else payload)
    return output.getvalue()


class ImmediateWait:
    def __init__(self, driver, *_args, **_kwargs):
        self.driver = driver

    def until(self, callback):
        value = callback(self.driver)
        if not value:
            raise TimeoutException("offline condition not satisfied")
        return value


class MacNaverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bot = MacNaverAutomation(self.root, MagicMock())
        self.url = "https://storage.googleapis.com/chrome-for-testing-public/150.0.7871.20/mac-arm64/chromedriver-mac-arm64.zip"
        self.metadata = json.dumps({"builds": {"150.0.7871": {"downloads": {
            "chromedriver": [
                {"platform": "win64", "url": "https://example.invalid/windows.exe"},
                {"platform": "mac-x64", "url": "https://example.invalid/intel.zip"},
                {"platform": "mac-arm64", "url": self.url},
            ]}}}}).encode()

    def test_download_selects_arm64_verifies_version_and_reuses_hash_checked_cache(self):
        with patch.object(self.bot, "_bounded_get", side_effect=[self.metadata, archived()]) as fetch, \
                patch.object(self.bot, "_driver_major", return_value="150") as version:
            path = self.bot._ensure_chromedriver("150.0.7871.99")
            self.assertEqual(path.name, "chromedriver-150-mac-arm64")
            self.assertEqual(path.read_bytes(), macho())
            self.assertEqual(fetch.call_args_list[1].args[0], self.url)
            self.assertEqual(self.bot._ensure_chromedriver("150.0.7871.99"), path)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(version.call_count, 2, "Cached binary must still report its real version")

    def test_wrong_version_cannot_replace_existing_driver(self):
        with patch.object(self.bot, "_bounded_get", side_effect=[self.metadata, archived()]), \
                patch.object(self.bot, "_driver_major", return_value="149"):
            with self.assertRaisesRegex(RuntimeError, "버전"):
                self.bot._ensure_chromedriver("150.0.7871.99")
        self.assertFalse((self.root / "webdrivers/chromedriver-150-mac-arm64").exists())
        self.assertEqual(list((self.root / "webdrivers").glob("*.tmp")), [])

    def test_windows_and_intel_binaries_are_rejected_before_execution(self):
        for payload in [b"MZ" + b"\0" * 100, macho(0x01000007), b"#!/bin/sh\necho fake"]:
            with self.subTest(payload=payload[:8]), \
                    patch.object(self.bot, "_bounded_get", side_effect=[self.metadata, archived(payload)]), \
                    patch.object(self.bot, "_driver_major") as version:
                with self.assertRaisesRegex(RuntimeError, "ARM64"):
                    self.bot._ensure_chromedriver("150.0.7871.99")
                version.assert_not_called()

    def test_tampered_cache_is_not_executed(self):
        directory = self.root / "webdrivers"
        directory.mkdir()
        (directory / "chromedriver-150-mac-arm64").write_bytes(macho())
        (directory / "chromedriver-150-mac-arm64.json").write_text(json.dumps({"sha256": "wrong"}))
        with patch.object(self.bot, "_bounded_get", side_effect=[self.metadata, archived()]) as fetch, \
                patch.object(self.bot, "_driver_major", return_value="150") as version:
            self.bot._ensure_chromedriver("150.0.7871.99")
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(version.call_count, 1)

    def test_archive_path_traversal_is_never_extracted(self):
        with patch.object(self.bot, "_bounded_get", side_effect=[self.metadata, archived(filename="../../evil")]):
            with self.assertRaisesRegex(RuntimeError, "압축 내용"):
                self.bot._ensure_chromedriver("150.0.7871.99")
        self.assertFalse((self.root / "evil").exists())

    def test_download_url_only_allows_official_version_and_arm64(self):
        self.assertTrue(self.bot._valid_download_url(self.url, "150"))
        for url in [self.url.replace("https:", "http:"), self.url.replace("storage.googleapis.com", "evil.invalid"),
                    self.url.replace("mac-arm64", "mac-x64"), self.url + "?redirect=1",
                    self.url.replace("150.0", "149.0"), self.url.replace("https://", "https://user:secret@"),
                    self.url.replace("150.0.7871.20", "../150.0.7871.20")]:
            self.assertFalse(self.bot._valid_download_url(url, "150"), url)

    def test_cache_path_cannot_escape_or_follow_symlink(self):
        with self.assertRaisesRegex(RuntimeError, "폴더 밖"):
            self.bot._cache_path("../outside")
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "외부 링크"):
                self.bot._cache_path("driver")

    def test_native_and_universal_arm64_headers_are_checked(self):
        self.assertTrue(_arm64_macho(macho()))
        fat = struct.pack(">II", 0xCAFEBABE, 1) + struct.pack(">IIIII", 0x0100000C, 0, 28, len(macho()), 0) + macho()
        self.assertTrue(_arm64_macho(fat))
        self.assertFalse(_arm64_macho(fat[:-16]))
        self.assertFalse(_arm64_macho(macho(0x01000007)))
        self.assertFalse(_arm64_macho(b""))

    def test_bounded_get_closes_oversized_or_redirect_responses(self):
        response = MagicMock(status_code=200)
        response.headers = {"Content-Length": "1000"}
        with patch("mac_naver.requests.get", return_value=response) as get:
            with self.assertRaisesRegex(RuntimeError, "크기 제한"):
                self.bot._bounded_get("https://example.invalid", 20)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            response.close.assert_called_once()
        response = MagicMock(status_code=302, headers={})
        with patch("mac_naver.requests.get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "응답"):
                self.bot._bounded_get("https://example.invalid", 20)
            response.close.assert_called_once()

    def test_chunked_download_cannot_exceed_size_limit(self):
        response = MagicMock(status_code=200, headers={})
        response.iter_content.return_value = [b"123", b"456"]
        with patch("mac_naver.requests.get", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "크기 제한"):
                self.bot._bounded_get("https://example.invalid", 5)
            response.close.assert_called_once()

    def test_debug_version_is_bound_to_local_expected_port(self):
        info = {"Browser": "Chrome/150.0.7871.20", "webSocketDebuggerUrl": "ws://127.0.0.1:9339/devtools/browser/abc"}
        with patch.object(self.bot, "_bounded_get", return_value=json.dumps(info)):
            self.assertEqual(self.bot._debug_browser_version(), "150.0.7871.20")
        for endpoint in ["ws://evil.invalid:9339/devtools/browser/abc", "ws://127.0.0.1:9222/devtools/browser/abc"]:
            info["webSocketDebuggerUrl"] = endpoint
            with patch.object(self.bot, "_bounded_get", return_value=json.dumps(info)):
                with self.assertRaisesRegex(RuntimeError, "전용 Whale"):
                    self.bot._debug_browser_version()

    def test_attach_uses_existing_profile_and_close_only_quits_webdriver(self):
        driver = MagicMock()
        with patch.object(self.bot, "_supports_arm64", return_value=True), \
                patch.object(self.bot, "_debug_browser_version", return_value="150.0.7871.20"), \
                patch.object(self.bot, "_ensure_chromedriver", return_value=self.root / "driver"), \
                patch("mac_naver.webdriver.Chrome", return_value=driver) as chrome:
            self.assertIs(self.bot._driver(), driver)
            self.assertIs(self.bot._driver(), driver)
            self.assertEqual(chrome.call_count, 1)
            options = chrome.call_args.kwargs["options"]
            self.assertEqual(options.debugger_address, "127.0.0.1:9339")
            self.assertFalse(any("user-data-dir" in item for item in options.arguments))
            self.bot.close()
            driver.quit.assert_called_once()
            driver.execute_cdp_cmd.assert_not_called()
            self.assertIsNone(self.bot.driver)
            self.assertTrue(self.bot.attached_existing_whale)

    def test_non_mac_host_fails_before_any_browser_connection(self):
        with patch.object(self.bot, "_supports_arm64", return_value=False), \
                patch.object(self.bot, "_debug_browser_version") as read:
            with self.assertRaisesRegex(RuntimeError, "Apple Silicon"):
                self.bot._driver()
            read.assert_not_called()

    def test_login_requires_protected_navigation_and_actual_account_ui(self):
        driver = MagicMock(current_url="https://blog.naver.com/member")
        driver.get_cookies.return_value = [{"name": "NID_SES", "value": "present-but-maybe-expired"}]
        with patch("mac_naver.WebDriverWait", ImmediateWait), \
                patch.object(self.bot, "_find_across_frames", side_effect=lambda _driver, callback: callback()):
            driver.execute_script.return_value = False
            self.assertFalse(self.bot._has_naver_session(driver), "Cookie presence alone cannot pass")
            driver.execute_script.return_value = True
            self.assertTrue(self.bot._has_naver_session(driver))
            driver.get.assert_called_with("https://blog.naver.com/MyBlog.naver")
            driver.current_url = "https://nid.naver.com/nidlogin.login"
            self.assertFalse(self.bot._has_naver_session(driver))

    def test_login_never_imports_regular_browser_cookies(self):
        self.assertFalse(self.bot._import_existing_naver_session(MagicMock()))
        with patch.object(self.bot, "_has_naver_session", return_value=False), \
                patch.object(self.bot, "_import_existing_naver_session") as imported:
            with self.assertRaisesRegex(RuntimeError, "전용 Whale"):
                self.bot._require_naver_login(MagicMock())
            imported.assert_not_called()

    def test_image_fallback_uses_command_v_and_keeps_windows_control(self):
        self.assertEqual(NaverAutomation.EDITOR_MODIFIER, Keys.CONTROL)
        self.assertEqual(MacNaverAutomation.EDITOR_MODIFIER, Keys.COMMAND)
        chain = MagicMock()
        chain.key_down.return_value = chain
        chain.send_keys.return_value = chain
        chain.key_up.return_value = chain
        with patch.object(self.bot, "_copy_image_to_windows_clipboard") as copy, \
                patch.object(self.bot, "_focus_body_image_position", return_value=True), \
                patch.object(self.bot, "_image_component_count", side_effect=[0, 1]), \
                patch("naver_automation.ActionChains", return_value=chain), \
                patch("naver_automation.WebDriverWait", ImmediateWait):
            self.bot._paste_blog_image_at_position(MagicMock(), "image.png", 0, 1)
            copy.assert_called_once_with("image.png")
            chain.key_down.assert_called_once_with(Keys.COMMAND)
            chain.key_up.assert_called_once_with(Keys.COMMAND)

    def test_mac_clipboard_uses_nsimage_and_verifies_write(self):
        path = self.root / "photo.png"
        path.write_bytes(b"fixture")
        appkit = types.ModuleType("AppKit")
        appkit.NSImage = MagicMock()
        appkit.NSPasteboard = MagicMock()
        foundation = types.ModuleType("Foundation")
        foundation.NSData = MagicMock()
        with patch.dict(sys.modules, {"AppKit": appkit, "Foundation": foundation}):
            appkit.NSPasteboard.generalPasteboard.return_value.writeObjects_.return_value = True
            self.bot._copy_image_to_windows_clipboard(str(path))
            foundation.NSData.dataWithContentsOfFile_.assert_called_once_with(str(path))
            appkit.NSPasteboard.generalPasteboard.return_value.writeObjects_.return_value = False
            with self.assertRaisesRegex(RuntimeError, "복사"):
                self.bot._copy_image_to_windows_clipboard(str(path))


if __name__ == "__main__":
    unittest.main()
