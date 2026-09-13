"""Offline account/profile/driver guards; no installed browser is started."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import TimeoutException

from blog_browser import (BlogBrowser, account_browser_dir, assert_blog_account_target,
                          create_blog_browser, normalize_browser, _blog_id_from_url)
from naver_automation import NaverAutomation


HOME = "https://section.blog.naver.com/BlogHome.naver"


def own_link(href, text="내 블로그", visible=True):
    element = MagicMock()
    element.text = text
    element.is_displayed.return_value = visible
    element.get_attribute.side_effect = lambda name: href if name == "href" else ""
    return element


def logged_driver(blog_id="writer_one"):
    driver = MagicMock()
    driver.current_url = HOME
    driver.get_cookies.return_value = [{"name": "NID_SES", "value": "offline-fake-session"}]
    driver.find_elements.return_value = [own_link(f"https://blog.naver.com/{blog_id}")]
    driver.get.side_effect = lambda url: setattr(driver, "current_url", url)
    return driver


class ImmediateWait:
    def __init__(self, driver, *args, **kwargs):
        self.driver = driver

    def until(self, callback):
        result = callback(self.driver)
        if not result:
            raise TimeoutException("offline condition unsatisfied")
        return result


class TempBrowserTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class BrowserFactoryTests(TempBrowserTestCase):

    def test_default_whale_retains_dedicated_profile_and_receipt_root(self):
        bot = create_blog_browser(self.root, lambda _: None)
        self.assertIsInstance(bot, NaverAutomation)
        self.assertEqual(bot.browser, "whale")
        self.assertEqual(bot.data_dir, self.root.resolve())
        self.assertEqual(bot.profile_dir, self.root / "naver-whale-profile")
        self.assertEqual(bot.state_file.parent, self.root)

    def test_account_and_browser_paths_are_stable_and_distinct(self):
        first = create_blog_browser(self.root, lambda _: None, "edge", account_id="one", blog_id="writer_one")
        again = create_blog_browser(self.root, lambda _: None, "edge", account_id="one")
        second = create_blog_browser(self.root, lambda _: None, "edge", account_id="two")
        chrome = create_blog_browser(self.root, lambda _: None, "chrome", account_id="one")
        self.assertEqual(first.profile_dir, again.profile_dir)
        self.assertEqual(len({first.profile_dir, second.profile_dir, chrome.profile_dir}), 3)
        self.assertEqual(len({first.driver_cache_dir, second.driver_cache_dir, chrome.driver_cache_dir}), 3)
        self.assertEqual(first.target_blog_id, "writer_one")

    def test_untrusted_account_key_cannot_escape_root_or_collide_after_slugging(self):
        for key in ("../../outside", "C:\\Users\\someone", "한글 계정", "."):
            with self.subTest(key=key):
                self.assertTrue(account_browser_dir(self.root, key, "edge").is_relative_to(self.root))
        self.assertNotEqual(account_browser_dir(self.root, "A B", "edge"),
                            account_browser_dir(self.root, "A/B", "edge"))

    def test_unknown_browser_rejected_before_files_created(self):
        with self.assertRaises(ValueError):
            create_blog_browser(self.root / "missing", lambda _: None, "firefox")
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(normalize_browser(" Chrome "), "chrome")

    def test_target_binding_cannot_be_changed_on_live_instance(self):
        bot = create_blog_browser(self.root, lambda _: None, "edge", blog_id="Writer_One")
        self.assertEqual(bot.bind_target_blog("writer_one"), "writer_one")
        with self.assertRaises(ValueError):
            bot.bind_target_blog("writer_two")
        with patch.object(NaverAutomation, "publish_naver_article") as publish:
            with self.assertRaises(ValueError):
                bot.publish_naver_article("writer_two", {})
            publish.assert_not_called()

    def test_stop_and_reset_contract_is_unchanged(self):
        bot = create_blog_browser(self.root, lambda _: None, "edge")
        bot.stop()
        with patch.object(bot, "_find_installed_browser") as find:
            with self.assertRaisesRegex(RuntimeError, "중지"):
                bot._driver()
            find.assert_not_called()
        bot.reset_stop()
        self.assertFalse(bot.stop_event.is_set())

    def test_browser_logs_never_misidentify_edge_as_whale(self):
        logs = []
        bot = create_blog_browser(self.root, logs.append, "edge")
        bot.log("네이버 웨일 연결 완료, 웨일 로그인")
        self.assertIn("Microsoft Edge", logs[0])
        self.assertNotIn("웨일", logs[0])


class DriverIsolationTests(TempBrowserTestCase):
    def make_paths(self, browser="edge"):
        bot = create_blog_browser(self.root / browser, lambda _: None, browser)
        executable = self.root / ("msedge.exe" if browser == "edge" else "chrome.exe")
        executable.touch()
        driver_path = bot.driver_cache_dir / "test-version" / ("msedgedriver.exe" if browser == "edge" else "chromedriver.exe")
        driver_path.parent.mkdir(parents=True)
        driver_path.touch()
        return bot, executable, driver_path

    def test_manager_uses_only_installed_browser_and_account_driver_cache(self):
        bot, browser, path = self.make_paths()
        with patch("blog_browser.SeleniumManager") as manager:
            manager.return_value.binary_paths.return_value = {"driver_path": str(path), "browser_path": str(browser)}
            self.assertEqual(bot._resolve_driver(browser), path)
            args = manager.return_value.binary_paths.call_args.args[0]
        self.assertEqual(args[args.index("--cache-path") + 1], str(bot.driver_cache_dir))
        self.assertEqual(args[args.index("--browser-path") + 1], str(browser))
        for flag in ("--avoid-browser-download", "--skip-driver-in-path", "--skip-browser-in-path", "--avoid-stats"):
            self.assertIn(flag, args)

    def test_manager_global_driver_or_other_browser_is_rejected(self):
        bot, browser, path = self.make_paths()
        unrelated = self.root / "msedgedriver.exe"
        unrelated.touch()
        for driver_result, browser_result in ((unrelated, browser), (path, self.root / "other.exe")):
            with self.subTest(driver=driver_result, browser=browser_result), patch("blog_browser.SeleniumManager") as manager:
                manager.return_value.binary_paths.return_value = {"driver_path": str(driver_result), "browser_path": str(browser_result)}
                with self.assertRaisesRegex(RuntimeError, "드라이버 경로"):
                    bot._resolve_driver(browser)

    def test_edge_and_chrome_launch_only_dedicated_profile_without_remote_attach(self):
        for browser in ("edge", "chrome"):
            with self.subTest(browser=browser):
                bot, executable, path = self.make_paths(browser)
                driver = MagicMock()
                constructor = "blog_browser.webdriver.Edge" if browser == "edge" else "blog_browser.webdriver.Chrome"
                with patch.object(bot, "_find_installed_browser", return_value=executable), \
                     patch.object(bot, "_resolve_driver", return_value=path), \
                     patch.object(bot, "_check_driver_version"), patch(constructor, return_value=driver) as launch:
                    self.assertIs(bot._driver(), driver)
                    options = launch.call_args.kwargs["options"]
                    service = launch.call_args.kwargs["service"]
                    self.assertEqual(options.binary_location, str(executable))
                    self.assertIn(f"--user-data-dir={bot.profile_dir}", options.arguments)
                    self.assertFalse(options.debugger_address)
                    self.assertEqual(Path(service.path), path)
                    for forbidden in ("--remote-debugging-port", "--no-sandbox", "--disable-web-security", "--disable-blink-features"):
                        self.assertFalse(any(arg.startswith(forbidden) for arg in options.arguments))
                    self.assertIs(bot._driver(), driver)
                    self.assertEqual(launch.call_count, 1)

    def test_version_mismatch_closes_only_owned_driver(self):
        bot, executable, path = self.make_paths()
        driver = MagicMock()
        with patch.object(bot, "_find_installed_browser", return_value=executable), \
             patch.object(bot, "_resolve_driver", return_value=path), \
             patch.object(bot, "_check_driver_version", side_effect=RuntimeError("mismatch")), \
             patch("blog_browser.webdriver.Edge", return_value=driver), \
             patch.object(bot, "_terminate_owned_process") as terminate:
            with self.assertRaises(RuntimeError):
                bot._driver()
            driver.quit.assert_called_once()
            terminate.assert_not_called()
            self.assertIsNone(bot.driver)

    def test_environment_cannot_replace_account_driver_with_global_executable(self):
        bot, executable, path = self.make_paths()
        unrelated = self.root / "global-msedgedriver.exe"
        unrelated.touch()
        with patch.dict(os.environ, {"SE_EDGEDRIVER": str(unrelated)}), \
             patch.object(bot, "_find_installed_browser", return_value=executable), \
             patch.object(bot, "_resolve_driver", return_value=path), \
             patch("blog_browser.webdriver.Edge") as launch:
            with self.assertRaises(RuntimeError):
                bot._driver()
            launch.assert_not_called()

    def test_edge_first_three_version_parts_must_match(self):
        bot, _, path = self.make_paths()
        driver = MagicMock()
        driver.capabilities = {"browserVersion": "140.0.3485.54"}
        for version, accepted in (("140.0.3485.10", True), ("140.0.3486.10", False), ("139.0.3485.54", False)):
            with self.subTest(version=version), patch("blog_browser.subprocess.run", return_value=subprocess.CompletedProcess([], 0, f"MSEdgeDriver {version}")):
                if accepted:
                    bot._check_driver_version(driver, path)
                else:
                    with self.assertRaisesRegex(RuntimeError, "버전"):
                        bot._check_driver_version(driver, path)

    def test_chrome_driver_major_version_must_match(self):
        bot, _, path = self.make_paths("chrome")
        driver = MagicMock()
        driver.capabilities = {"browserVersion": "140.0.3485.54"}
        with patch("blog_browser.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ChromeDriver 140.0.7339.41")):
            bot._check_driver_version(driver, path)
        with patch("blog_browser.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ChromeDriver 139.0.7339.41")):
            with self.assertRaisesRegex(RuntimeError, "버전"):
                bot._check_driver_version(driver, path)

    def test_existing_whale_lifecycle_is_delegated_without_touching_mac_class(self):
        bot = create_blog_browser(self.root, lambda _: None)
        with patch.object(NaverAutomation, "_driver", return_value="existing-owned-whale") as driver:
            self.assertEqual(bot._driver(), "existing-owned-whale")
            driver.assert_called_once()
        with patch.object(NaverAutomation, "close") as close:
            bot.close()
            close.assert_called_once()

    def test_find_installed_browser_does_not_download_browser(self):
        installation = self.root / "Google" / "Chrome" / "Application" / "chrome.exe"
        installation.parent.mkdir(parents=True)
        installation.touch()
        with patch.dict(os.environ, {"PROGRAMFILES": str(self.root)}, clear=True), patch("blog_browser.shutil.which", return_value=None):
            self.assertEqual(BlogBrowser._find_installed_browser("chrome"), installation)
            with self.assertRaisesRegex(RuntimeError, "설치된 Microsoft Edge"):
                BlogBrowser._find_installed_browser("edge")


class AccountIdentityTests(TempBrowserTestCase):
    def test_explicit_own_blog_link_matches_target_without_exposing_session(self):
        result = assert_blog_account_target(logged_driver(), "WRITER_ONE")
        self.assertEqual(result, {"authenticated": True, "blog_id": "writer_one", "evidence": "visible_own_blog_navigation"})
        self.assertNotIn("offline-fake-session", str(result))

    def test_foreign_blog_or_ambiguous_account_links_block(self):
        for links in ([own_link("https://blog.naver.com/other_writer")],
                      [own_link("https://blog.naver.com/writer_one"), own_link("https://blog.naver.com/other_writer")]):
            driver = logged_driver()
            driver.find_elements.return_value = links
            with self.assertRaisesRegex(RuntimeError, "다릅니다"):
                assert_blog_account_target(driver, "writer_one")

    def test_hidden_or_generic_post_links_are_not_account_evidence(self):
        driver = logged_driver()
        driver.find_elements.return_value = [own_link("https://blog.naver.com/writer_one", visible=False),
                                             own_link("https://blog.naver.com/writer_one", text="게시글 제목")]
        with self.assertRaisesRegex(RuntimeError, "확인하지 못했습니다"):
            assert_blog_account_target(driver, "writer_one")
        driver.get.assert_not_called()

    def test_observed_myblog_redirect_can_identify_actual_blog(self):
        driver = logged_driver()
        driver.find_elements.return_value = [own_link("https://blog.naver.com/MyBlog.naver")]
        driver.get.side_effect = lambda _: setattr(driver, "current_url", "https://blog.naver.com/PostList.naver?blogId=writer_one")
        with patch("blog_browser.WebDriverWait", ImmediateWait):
            self.assertEqual(assert_blog_account_target(driver, "writer_one")["blog_id"], "writer_one")
        driver.get.assert_called_once_with("https://blog.naver.com/MyBlog.naver")

    def test_external_redirect_and_anonymous_home_never_pass(self):
        driver = logged_driver()
        driver.find_elements.return_value = [own_link("https://evil.invalid/MyBlog.naver")]
        with self.assertRaises(RuntimeError):
            assert_blog_account_target(driver, "writer_one")
        driver.get.assert_not_called()
        driver = logged_driver()
        driver.get_cookies.return_value = []
        with self.assertRaisesRegex(RuntimeError, "로그인 홈"):
            assert_blog_account_target(driver, "writer_one")

    def test_arbitrary_page_cannot_present_account_evidence(self):
        driver = logged_driver()
        driver.current_url = "https://blog.naver.com/other_writer"
        with self.assertRaisesRegex(RuntimeError, "로그인 홈"):
            assert_blog_account_target(driver, "writer_one")

    def test_writer_urls_reject_lookalike_host_duplicate_query_and_other_targets(self):
        self.assertEqual(_blog_id_from_url("https://blog.naver.com/writer_one/postwrite", writer_only=True), "writer_one")
        self.assertEqual(_blog_id_from_url("https://blog.naver.com/PostWriteForm.naver?blogId=writer_one", writer_only=True), "writer_one")
        for url in ("https://blog.naver.com.evil.invalid/writer_one/postwrite", "http://blog.naver.com/writer_one/postwrite",
                    "https://blog.naver.com/writer_one", "https://blog.naver.com/PostWriteForm.naver?blogId=writer_one&blogId=other",
                    "https://user@blog.naver.com/writer_one/postwrite"):
            self.assertEqual(_blog_id_from_url(url, writer_only=True), "")

    def test_no_general_profile_import_is_attempted_when_logged_out(self):
        for browser in ("whale", "edge", "chrome"):
            bot = create_blog_browser(self.root / browser, lambda _: None, browser, blog_id="writer_one")
            with patch.object(bot, "_has_naver_session", return_value=False), \
                 patch.object(NaverAutomation, "_import_existing_naver_session") as importer:
                with self.assertRaisesRegex(RuntimeError, "전용 브라우저"):
                    bot._require_naver_login(MagicMock())
                importer.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "가져오지 않습니다"):
                bot._import_existing_naver_session(MagicMock())

    def test_login_window_is_same_dedicated_driver_and_home(self):
        bot = create_blog_browser(self.root, lambda _: None, "edge")
        driver = logged_driver()
        with patch.object(bot, "_driver", return_value=driver):
            bot.open_login("writer_one")
        driver.get.assert_called_once_with(HOME)
        self.assertEqual(bot.target_blog_id, "writer_one")

    def test_verify_account_returns_only_observed_target_and_browser(self):
        bot = create_blog_browser(self.root, lambda _: None, "edge", blog_id="writer_one")
        driver = logged_driver()
        with patch.object(bot, "_driver", return_value=driver), patch.object(bot, "_has_naver_session", return_value=True):
            result = bot.verify_account()
        self.assertEqual(result["blog_id"], "writer_one")
        self.assertEqual(result["browser"], "edge")

    def test_wrong_account_blocks_before_opening_writer_or_finding_fields(self):
        bot = create_blog_browser(self.root, lambda _: None, "edge", blog_id="writer_one")
        driver = logged_driver("other_writer")
        with patch.object(bot, "_has_naver_session", return_value=True), patch.object(bot, "_find_editor_fields") as fields:
            with self.assertRaisesRegex(RuntimeError, "다릅니다"):
                bot._open_writer(driver, "writer_one")
        driver.get.assert_not_called()
        fields.assert_not_called()

    def test_writer_must_keep_target_even_after_confirmed_login(self):
        bot = create_blog_browser(self.root, lambda _: None, "edge", blog_id="writer_one")
        driver = logged_driver()
        driver.get.side_effect = lambda _: setattr(driver, "current_url", "https://blog.naver.com/other_writer/postwrite")
        with patch.object(bot, "_has_naver_session", return_value=True), \
             patch.object(bot, "_find_editor_fields", return_value=("title", "body")), \
             patch.object(bot, "_handle_writer_recovery_prompt"), patch("naver_automation.WebDriverWait", ImmediateWait):
            with self.assertRaisesRegex(RuntimeError, "원고를 입력하지 않았습니다"):
                bot._open_writer(driver, "writer_one")

    def test_valid_writer_restores_matching_editor_frame(self):
        bot = create_blog_browser(self.root, lambda _: None, "chrome", blog_id="writer_one")
        driver = logged_driver()
        with patch.object(bot, "_has_naver_session", return_value=True), \
             patch.object(bot, "_find_editor_fields", return_value=("title", "body")) as fields, \
             patch.object(bot, "_handle_writer_recovery_prompt"), patch("naver_automation.WebDriverWait", ImmediateWait):
            self.assertEqual(bot._open_writer(driver, "writer_one"), ("title", "body"))
        self.assertGreaterEqual(fields.call_count, 2)

    def test_final_publication_gate_rechecks_same_target_without_navigation(self):
        bot = create_blog_browser(self.root, lambda _: None, "edge", blog_id="writer_one")
        driver = logged_driver()
        driver.current_url = "https://blog.naver.com/writer_one/postwrite"
        with patch.object(NaverAutomation, "_article_ready_to_publish", return_value=True) as content:
            self.assertFalse(bot._article_ready_to_publish(driver))
            content.assert_not_called()
            bot._account_evidence = {"blog_id": "writer_one"}
            self.assertTrue(bot._article_ready_to_publish(driver))
            driver.current_url = "https://blog.naver.com/other_writer/postwrite"
            self.assertFalse(bot._article_ready_to_publish(driver))
            self.assertEqual(content.call_count, 1)
        driver.get.assert_not_called()

    def test_prior_successful_receipt_can_be_returned_without_reopening_account(self):
        bot = create_blog_browser(self.root, lambda _: None, "edge", blog_id="writer_one")
        with patch.object(bot, "publication_receipt_for", return_value={"published": True, "status": "published"}), \
             patch.object(bot, "_driver") as browser:
            result = bot.publish_naver_article("writer_one", {})
        self.assertTrue(result["published"])
        self.assertTrue(result["reused_receipt"])
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
