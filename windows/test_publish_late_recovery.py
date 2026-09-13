import unittest
from unittest.mock import MagicMock

from selenium.common.exceptions import ElementClickInterceptedException, WebDriverException

import test_naver_publish as support


class LateWriterRecoveryTests(unittest.TestCase):
    setUp = support.PublishTests.setUp
    _set_reviewed_content_hash = support.PublishTests._set_reviewed_content_hash
    _mock_draft_button = support.PublishTests._mock_draft_button

    def recovery_dialog(self, *, visible=True, text=None):
        dialog, cancel, confirm = MagicMock(), MagicMock(), MagicMock()
        dialog.text = text or ("작성 중인 글이 있습니다. 12일 오후 10시 17분에 작성중이던 내용이 있습니다. "
                               "이어서 작성하시겠습니까?")
        cancel.text, confirm.text = "취소", "확인"
        state = {"visible": visible}
        dialog.is_displayed.side_effect = lambda: state["visible"]
        cancel.is_displayed.return_value = cancel.is_enabled.return_value = True
        cancel.click.side_effect = lambda: state.update(visible=False)
        dialog.find_elements.side_effect = lambda _by, selector: [cancel] if selector == ".se-popup-button-cancel" else []
        self.driver.find_elements.side_effect = lambda _by, selector: [dialog] if selector == ".se-popup, [role='dialog']" else []
        self.app._find_across_frames = MagicMock(side_effect=lambda _driver, finder: finder())
        return state, dialog, cancel, confirm

    def assert_no_submission(self):
        self.final.click.assert_not_called()
        self.assertFalse((self.root / "publication_receipts").exists())

    def test_writer_is_activated_before_dialog_lookup_and_native_cancel(self):
        state, _dialog, cancel, _confirm = self.recovery_dialog()
        events = []
        self.driver.execute_cdp_cmd.side_effect = lambda *args: events.append("activate")
        original_find = self.driver.find_elements.side_effect
        def find(*args):
            self.assertEqual(events[0], "activate")
            events.append("find")
            return original_find(*args)
        self.driver.find_elements.side_effect = find
        self.app.log = lambda _message: events.append("log")
        def clicked():
            self.assertEqual(events[-1], "log")
            events.append("cancel")
            state["visible"] = False
        cancel.click.side_effect = clicked

        self.app._handle_writer_recovery_prompt(self.driver)

        self.driver.execute_cdp_cmd.assert_called_once_with("Page.bringToFront", {})
        self.assertLess(events.index("activate"), events.index("find"))
        self.assertLess(events.index("find"), events.index("cancel"))
        self.driver.switch_to.window.assert_not_called()

    def test_unsupported_cdp_activates_only_current_writer_handle(self):
        state, _dialog, cancel, _confirm = self.recovery_dialog()
        self.driver.current_window_handle = "existing-writer-handle"
        self.driver.execute_cdp_cmd.side_effect = WebDriverException("CDP unsupported")
        def clicked():
            self.driver.switch_to.window.assert_called_once_with("existing-writer-handle")
            state["visible"] = False
        cancel.click.side_effect = clicked

        self.app._handle_writer_recovery_prompt(self.driver)

        cancel.click.assert_called_once()
        self.driver.get.assert_not_called()
        self.driver.execute_script.assert_not_called()

    def test_activation_does_not_touch_other_pages(self):
        for url in ("https://www.google.com/search?q=photo", "https://blog.naver.com/testblog/12345",
                    "https://unrelated.example/testblog/postwrite", "https://blog.naver.com.evil.example/testblog/postwrite"):
            with self.subTest(url=url):
                self.driver.current_url = url
                self.app._activate_writer_tab(self.driver)
        self.driver.execute_cdp_cmd.assert_not_called()
        self.driver.switch_to.window.assert_not_called()

    def test_known_restore_dismissal_timeout_identifies_failed_action(self):
        _state, _dialog, cancel, confirm = self.recovery_dialog()
        cancel.click.side_effect = None

        with self.assertRaisesRegex(RuntimeError, "취소 버튼을 눌렀지만 안내창이 닫히지"):
            self.app._handle_writer_recovery_prompt(self.driver)

        cancel.click.assert_called_once()
        confirm.click.assert_not_called()
        self.assert_no_submission()

    def test_delayed_restore_is_declined_before_content_validation_and_publish(self):
        state, _dialog, cancel, confirm = self.recovery_dialog()
        def verified(*_args, **_kwargs):
            self.assertFalse(state["visible"])
            return True
        self.app._article_ready_to_publish.side_effect = verified

        result = self.app.publish_naver_article("testblog", self.article)

        self.assertTrue(result["published"])
        cancel.click.assert_called_once()
        confirm.click.assert_not_called()
        self.opener.click.assert_called_once()
        self.final.click.assert_called_once()
        self.app._prepare_article_in_writer.assert_called_once()

    def test_delayed_restore_is_declined_before_draft_save_without_publishing(self):
        state, _dialog, cancel, confirm = self.recovery_dialog()
        save_button = self._mock_draft_button()
        save_button.click.side_effect = lambda: self.assertFalse(state["visible"])

        result = self.app.publish_naver_article("testblog", self.article, publish=False, save_draft=True)

        self.assertEqual(result["status"], "draft_saved")
        cancel.click.assert_called_once()
        confirm.click.assert_not_called()
        save_button.click.assert_called_once()
        self.opener.click.assert_not_called()
        self.assert_no_submission()

    def test_intercepted_opener_recovers_once_then_rechecks_full_article(self):
        state, _dialog, cancel, confirm = self.recovery_dialog(visible=False)
        def opener_click():
            if self.opener.click.call_count == 1:
                state["visible"] = True
                raise ElementClickInterceptedException("known recovery dim")
            self.assertFalse(state["visible"])
            self.publish_panel_open = True
        self.opener.click.side_effect = opener_click

        result = self.app.publish_naver_article("testblog", self.article)

        self.assertTrue(result["published"])
        self.assertEqual(self.opener.click.call_count, 2)
        self.assertEqual(self.app._article_ready_to_publish.call_count, 3)
        self.app._prepare_article_in_writer.assert_called_once()
        cancel.click.assert_called_once()
        confirm.click.assert_not_called()
        self.final.click.assert_called_once()

    def test_racing_unknown_or_security_dialog_is_never_dismissed(self):
        state, dialog, cancel, confirm = self.recovery_dialog(visible=False, text="계정 보호를 위한 추가 인증이 필요합니다.")
        def intercepted():
            state["visible"] = True
            raise ElementClickInterceptedException("security dialog")
        self.opener.click.side_effect = intercepted

        with self.assertRaisesRegex(RuntimeError, "알 수 없는"):
            self.app.publish_naver_article("testblog", self.article)

        self.opener.click.assert_called_once()
        dialog.find_elements.assert_not_called()
        cancel.click.assert_not_called()
        confirm.click.assert_not_called()
        self.assert_no_submission()

    def test_changed_article_after_cancel_never_retries_opener_or_submits(self):
        state, _dialog, cancel, _confirm = self.recovery_dialog(visible=False)
        def intercepted():
            state["visible"] = True
            raise ElementClickInterceptedException("known recovery dim")
        self.opener.click.side_effect = intercepted
        self.app._article_ready_to_publish.side_effect = [True, False]

        with self.assertRaisesRegex(RuntimeError, "안내창 처리 후 내용 검증"):
            self.app.publish_naver_article("testblog", self.article)

        cancel.click.assert_called_once()
        self.opener.click.assert_called_once()
        self.assert_no_submission()

    def test_second_interception_stops_without_final_click_or_receipt(self):
        self.recovery_dialog(visible=False)
        self.opener.click.side_effect = ElementClickInterceptedException("persistent overlay")

        with self.assertRaises(ElementClickInterceptedException):
            self.app.publish_naver_article("testblog", self.article)

        self.assertEqual(self.opener.click.call_count, 2)
        self.assertEqual(self.app._article_ready_to_publish.call_count, 2)
        self.assert_no_submission()

    def test_final_click_interception_keeps_uncertain_receipt_and_never_retries(self):
        self.recovery_dialog(visible=False)
        self.final.click.side_effect = ElementClickInterceptedException("late final dialog")

        result = self.app.publish_naver_article("testblog", self.article)
        repeated = self.app.publish_naver_article("testblog", self.article)

        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(repeated["status"], "uncertain")
        self.assertTrue(repeated["reused_receipt"])
        self.opener.click.assert_called_once()
        self.final.click.assert_called_once()
        self.app._prepare_article_in_writer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
