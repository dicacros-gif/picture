import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from blog_controls import BlogWorkflowControls

class CyclePreflightTests(unittest.TestCase):
    def make(self):
        app=object.__new__(BlogWorkflowControls)
        app.cli_bridge=SimpleNamespace(cancel_event=None,check_accounts=Mock(return_value={
          'chatgpt':{'installed':True,'auth_status':'available'},
          'antigravity':{'installed':True,'text_status':'not_checked'}}))
        app.events=Mock();app._naver_log=Mock();app.full_auto_stop=Mock()
        return app

    def test_same_cycle_only_reuses_success_and_changed_config_rechecks(self):
        app=self.make();budget=Mock();config={'steps':['chatgpt'],'models':{'chatgpt':'model-a'}}
        first=app._preflight_cli_accounts(config,budget)
        first['chatgpt']['installed']=False
        result=app._preflight_cli_accounts(config,budget)
        self.assertTrue(result['chatgpt']['installed'])
        self.assertEqual(app.cli_bridge.check_accounts.call_count,1)
        app._preflight_cli_accounts({**config,'models':{'chatgpt':'model-b'}},budget)
        app._preflight_cli_accounts(config,Mock())
        self.assertEqual(app.cli_bridge.check_accounts.call_count,3)

    def test_failed_check_and_unbudgeted_manual_run_are_not_cached(self):
        app=self.make();budget=Mock();config={'steps':['chatgpt']}
        app.cli_bridge.check_accounts.side_effect=RuntimeError('login')
        for _ in range(2):
            with self.assertRaises(RuntimeError):app._preflight_cli_accounts(config,budget)
        self.assertEqual(app.cli_bridge.check_accounts.call_count,2)
        app.cli_bridge.check_accounts.side_effect=None
        app._preflight_cli_accounts(config)
        app._preflight_cli_accounts(config)
        self.assertEqual(app.cli_bridge.check_accounts.call_count,4)

if __name__=='__main__':unittest.main()
