import json
import tempfile
import unittest
from pathlib import Path

import test_blog_controls as support
from blog_accounts_ui import normalized_writer_accounts, validate_writer_accounts, writer_data_dir


class AccountsUiTests(unittest.TestCase):
    make_app = support.BlogUiTests.make_app

    def test_two_account_browser_settings_and_split_logs_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, {'blog_id': 'firstblog'})
            _, enabled, browser, identifier = app.writer_accounts_ui.rows[1]
            enabled.set(True)
            browser.set('에지')
            identifier.set('secondblog')
            app.writer_accounts_ui.save()
            app.progress_panel.append('첫 계정 작업', account='primary')
            app.progress_panel.append('두 번째 계정 작업', account='secondary')
            self.assertEqual(len(app.progress_panel.account_split.panes()), 2)
            self.assertNotIn('두 번째', app.progress_panel.text.get('1.0', 'end'))
            self.assertIn('두 번째', app.progress_panel.account_texts['secondary'].get('1.0', 'end'))
            saved = json.loads((Path(directory) / 'settings.json').read_text(encoding='utf-8'))
            restored = self.make_app(directory, saved)
            accounts = restored._cli_configuration()['writer_accounts']
            self.assertEqual(accounts[1], {'id': 'secondary', 'browser': 'edge', 'blog_id': 'secondblog', 'enabled': True})
            self.assertEqual(len(restored.progress_panel.account_split.panes()), 2)

    def test_disabled_account_collapses_only_second_log_without_losing_its_text(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, {'blog_id': 'firstblog'})
            app.writer_accounts_ui.rows[1][1].set(True)
            app.writer_accounts_ui.save()
            app.progress_panel.append('기존 실행 기록', account='secondary')
            app.writer_accounts_ui.rows[1][1].set(False)
            app.writer_accounts_ui.save()
            self.assertEqual(len(app.progress_panel.account_split.panes()), 1)
            self.assertIn('기존 실행 기록', app.progress_panel.account_texts['secondary'].get('1.0', 'end'))

    def test_same_blog_id_or_missing_second_id_prevents_double_submission_configuration(self):
        settings = {'writer_accounts': [{'id': 'secondary', 'enabled': True, 'blog_id': 'firstblog'}]}
        for value in ('firstblog', '', 'invalid/id'):
            settings['writer_accounts'][0]['blog_id'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_writer_accounts(normalized_writer_accounts(settings, 'firstblog'))

    def test_secondary_profile_cannot_share_primary_artifact_root(self):
        root = Path('app-data').resolve()
        accounts = normalized_writer_accounts({}, 'firstblog')
        self.assertEqual(writer_data_dir(root, accounts[0]), root)
        self.assertEqual(writer_data_dir(root, accounts[1]), root / 'writer-accounts' / 'secondary' / 'edge')
