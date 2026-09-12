"""Exercise the engine login boundary without accessing a real account."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'windows'))
sys.path.insert(0, str(ROOT / 'mac-app/backend'))

from engine import open_cli_login


class MacLoginRequestTests(unittest.TestCase):
    def test_ordinary_login_remains_available_for_every_cli(self):
        for provider in ('chatgpt', 'claude', 'antigravity'):
            with self.subTest(provider=provider):
                bridge = MagicMock()
                result = open_cli_login(bridge, {'provider': provider})
                bridge.open_login.assert_called_once_with(provider, device_auth=False)
                self.assertEqual(result['status'], 'login_opened')
                self.assertFalse(result['deviceAuth'])
                bridge.check_accounts.assert_not_called()

    def test_explicit_device_choice_reaches_bridge_and_remains_unverified(self):
        bridge = MagicMock()
        result = open_cli_login(bridge, {'provider': 'chatgpt', 'deviceAuth': True})
        bridge.open_login.assert_called_once_with('chatgpt', device_auth=True)
        self.assertEqual(result['status'], 'login_opened')
        self.assertTrue(result['deviceAuth'])
        self.assertIn('CLI 로그인 재확인', result['message'])
        self.assertNotIn('authenticated', result)
        bridge.check_accounts.assert_not_called()

    def test_invalid_modes_and_other_providers_never_open_a_console(self):
        for payload in (None, [], {}, {'provider': []}, {'provider': 'unknown'},
                        {'provider': 'claude', 'deviceAuth': True},
                        {'provider': 'antigravity', 'deviceAuth': True},
                        {'provider': 'chatgpt', 'deviceAuth': 'true'},
                        {'provider': 'chatgpt', 'deviceAuth': 1}):
            with self.subTest(payload=payload):
                bridge = MagicMock()
                with self.assertRaises(ValueError):
                    open_cli_login(bridge, payload)
                bridge.open_login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
