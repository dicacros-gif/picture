"""Offline tests for opening settings; no browser or publication is executed."""
import json
import unittest
from unittest.mock import MagicMock

from selenium.common.exceptions import ElementClickInterceptedException

import test_naver_publish as support


class PublishPanelOpenTests(unittest.TestCase):
    setUp = support.PublishTests.setUp
    _set_reviewed_content_hash = support.PublishTests._set_reviewed_content_hash

    def assert_not_submitted(self):
        self.final.click.assert_not_called()
        self.assertFalse((self.root / 'publication_receipts').exists())

    def failed_native_opener(self):
        self.opener.click.side_effect = None
        self.app._handle_writer_recovery_prompt = MagicMock()

    def javascript_opens_panel(self, script, button=None):
        if script == 'return document.hidden === true;':
            return False
        self.assertEqual(script, 'arguments[0].click();')
        self.assertIs(button, self.opener)
        self.assertFalse((self.root / 'publication_receipts').exists())
        self.publish_panel_open = True

    def test_already_open_panel_is_reused_without_toggling_opener(self):
        self.publish_panel_open = True
        result = self.app.publish_naver_article('testblog', self.article)
        self.assertTrue(result['published'])
        self.opener.click.assert_not_called()
        self.driver.execute_script.assert_called_once_with(
            'return document.hidden === true;'
        )
        self.final.click.assert_called_once()
        self.app._prepare_article_in_writer.assert_called_once()
        self.assertEqual(self.app._article_ready_to_publish.call_count, 2)

    def test_open_panel_does_not_bypass_final_content_validation(self):
        self.publish_panel_open = True
        self.app._article_ready_to_publish.side_effect = [True, False]
        with self.assertRaisesRegex(RuntimeError, '발행 직전'):
            self.app.publish_naver_article('testblog', self.article)
        self.opener.click.assert_not_called()
        self.assert_not_submitted()

    def test_native_click_timeout_allows_one_same_opener_js_click(self):
        self.failed_native_opener()
        self.driver.execute_script.side_effect = self.javascript_opens_panel
        def final_click():
            receipt = json.loads(next((self.root / 'publication_receipts').glob('*.json')).read_text(encoding='utf-8'))
            self.assertEqual(receipt['status'], 'uncertain')
        self.final.click.side_effect = final_click
        result = self.app.publish_naver_article('testblog', self.article)
        self.assertTrue(result['published'])
        self.opener.click.assert_called_once()
        self.assertEqual(self.driver.execute_script.call_args_list,
                         [unittest.mock.call('arguments[0].click();', self.opener),
                          unittest.mock.call('return document.hidden === true;')])
        self.final.click.assert_called_once()
        self.assertEqual(self.app._article_ready_to_publish.call_count, 3)
        self.app._prepare_article_in_writer.assert_called_once()

    def test_late_panel_after_timeout_is_used_without_fallback_click(self):
        self.failed_native_opener()
        def recover(_driver):
            if self.app._handle_writer_recovery_prompt.call_count == 2:
                self.publish_panel_open = True
        self.app._handle_writer_recovery_prompt.side_effect = recover
        result = self.app.publish_naver_article('testblog', self.article)
        self.assertTrue(result['published'])
        self.opener.click.assert_called_once()
        self.driver.execute_script.assert_called_once_with(
            'return document.hidden === true;'
        )

    def test_panel_arriving_during_revalidation_is_not_toggled_closed(self):
        self.failed_native_opener()
        def validated(*_args, **_kwargs):
            if self.app._article_ready_to_publish.call_count == 2:
                self.publish_panel_open = True
            return True
        self.app._article_ready_to_publish.side_effect = validated
        self.assertTrue(self.app.publish_naver_article('testblog', self.article)['published'])
        self.driver.execute_script.assert_called_once_with(
            'return document.hidden === true;'
        )
        self.opener.click.assert_called_once()

    def test_fallback_is_not_repeated_when_settings_still_do_not_open(self):
        self.failed_native_opener()
        with self.assertRaisesRegex(RuntimeError, '1회 보완하고 추가 대기한 뒤에도'):
            self.app.publish_naver_article('testblog', self.article)
        self.opener.click.assert_called_once()
        self.driver.execute_script.assert_called_once_with(
            'arguments[0].click();', self.opener
        )
        self.assert_not_submitted()

    def test_panel_that_opens_during_final_grace_period_is_published_without_another_toggle(self):
        self.failed_native_opener()

        class LatePanelWait:
            def __init__(inner, driver, timeout, *_args, **_kwargs):
                inner.driver = driver
                inner.timeout = timeout

            def until(inner, callback):
                if inner.timeout == 60:
                    self.publish_panel_open = True
                result = callback(inner.driver)
                if not result:
                    from selenium.common.exceptions import TimeoutException
                    raise TimeoutException('simulated wait')
                return result

        with unittest.mock.patch('naver_automation.WebDriverWait', LatePanelWait):
            result = self.app.publish_naver_article('testblog', self.article)

        self.assertTrue(result['published'])
        self.opener.click.assert_called_once()
        self.assertEqual(
            self.driver.execute_script.call_args_list,
            [unittest.mock.call('arguments[0].click();', self.opener),
             unittest.mock.call('return document.hidden === true;')],
        )
        self.final.click.assert_called_once()

    def test_replaced_or_missing_header_is_not_clicked_with_javascript(self):
        for replacement in (None, MagicMock()):
            with self.subTest(replacement=replacement):
                self.failed_native_opener()
                calls = []
                def control(_driver, final=False):
                    if final:
                        return None
                    calls.append(True)
                    return self.opener if len(calls) == 1 else replacement
                self.app._find_publish_control.side_effect = control
                with self.assertRaisesRegex(RuntimeError, '동일하게 확인하지 못했습니다'):
                    self.app.publish_naver_article('testblog', self.article)
                self.driver.execute_script.assert_not_called()
                self.assert_not_submitted()

    def test_unknown_dialog_blocks_timeout_fallback(self):
        self.failed_native_opener()
        self.app._handle_writer_recovery_prompt.side_effect = [None, RuntimeError('알 수 없는 글쓰기 안내창')]
        with self.assertRaisesRegex(RuntimeError, '알 수 없는'):
            self.app.publish_naver_article('testblog', self.article)
        self.driver.execute_script.assert_not_called()
        self.assert_not_submitted()

    def test_changed_content_prevents_fallback_and_receipt(self):
        self.failed_native_opener()
        self.app._article_ready_to_publish.side_effect = [True, False]
        with self.assertRaisesRegex(RuntimeError, '열기 재확인 중 내용 검증'):
            self.app.publish_naver_article('testblog', self.article)
        self.driver.execute_script.assert_not_called()
        self.assert_not_submitted()

    def test_cancel_during_revalidation_prevents_javascript_click(self):
        self.failed_native_opener()
        def validated(*_args, **_kwargs):
            if self.app._article_ready_to_publish.call_count == 2:
                self.app.stop_event.set()
            return True
        self.app._article_ready_to_publish.side_effect = validated
        with self.assertRaisesRegex(RuntimeError, '중지'):
            self.app.publish_naver_article('testblog', self.article)
        self.driver.execute_script.assert_not_called()
        self.assert_not_submitted()

    def test_cancel_after_native_opener_prevents_fallback(self):
        self.failed_native_opener()
        self.opener.click.side_effect = self.app.stop_event.set
        with self.assertRaisesRegex(RuntimeError, '중지'):
            self.app.publish_naver_article('testblog', self.article)
        self.driver.execute_script.assert_not_called()
        self.assert_not_submitted()

    def test_final_click_failure_after_fallback_is_never_retried(self):
        self.failed_native_opener()
        self.driver.execute_script.side_effect = self.javascript_opens_panel
        self.final.click.side_effect = ElementClickInterceptedException('final overlay')
        first = self.app.publish_naver_article('testblog', self.article)
        second = self.app.publish_naver_article('testblog', self.article)
        self.assertEqual(first['status'], 'uncertain')
        self.assertEqual(second['status'], 'uncertain')
        self.assertTrue(second['reused_receipt'])
        self.final.click.assert_called_once()
        self.assertEqual(self.driver.execute_script.call_args_list,
                         [unittest.mock.call('arguments[0].click();', self.opener),
                          unittest.mock.call('return document.hidden === true;')])


if __name__ == '__main__':
    unittest.main()
