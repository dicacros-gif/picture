import unittest
from unittest.mock import Mock

from selenium.common.exceptions import WebDriverException
from naver_automation import NaverAutomation


class GoogleCaptureRenderingTests(unittest.TestCase):
    def setUp(self):
        self.bot = object.__new__(NaverAutomation)
        self.bot.log = Mock()
        self.driver = Mock()
        self.bot._driver = Mock(return_value=self.driver)
        self.bot._capture_google_reference_candidates = Mock(return_value=[{'review_pending': True}])

    def test_capture_target_renders_and_focus_override_is_cleared(self):
        result = self.bot.capture_google_reference_candidates('gas station photo', 'unused', reuse_only=True, english_only=True)
        self.assertEqual(result, [{'review_pending': True}])
        self.assertEqual(self.driver.execute_cdp_cmd.call_args_list[0].args,
                         ('Emulation.setFocusEmulationEnabled', {'enabled': True}))
        self.assertEqual(self.driver.execute_cdp_cmd.call_args_list[-1].args,
                         ('Emulation.setFocusEmulationEnabled', {'enabled': False}))
        self.bot._capture_google_reference_candidates.assert_called_once_with(
            self.driver, 'gas station photo', 'unused', 2, reuse_only=True, english_only=True,
            allow_attribution=False)

    def test_failure_restores_capture_focus_without_hiding_original_error(self):
        self.bot._capture_google_reference_candidates.side_effect = RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.bot.capture_google_reference_candidates('gas station', 'unused')
        self.assertEqual(self.driver.execute_cdp_cmd.call_args.args,
                         ('Emulation.setFocusEmulationEnabled', {'enabled': False}))

    def test_unsupported_cdp_retains_existing_capture_checks(self):
        self.driver.execute_cdp_cmd.side_effect = WebDriverException('unsupported')
        self.bot.capture_google_reference_candidates('gas station', 'unused')
        self.bot._capture_google_reference_candidates.assert_called_once()
        self.driver.execute_cdp_cmd.assert_called_once()

    def test_invalid_english_query_never_opens_browser(self):
        with self.assertRaises(ValueError):
            self.bot.capture_google_reference_candidates('한글', 'unused', english_only=True)
        self.bot._driver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
