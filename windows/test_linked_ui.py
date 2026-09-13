import json
import tempfile
import threading
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock
from blog_accounts_ui import CommentTaskGroup
from blog_ui_helpers import model_choices
import test_blog_controls as support

class LinkedUiTests(unittest.TestCase):
    make_app = support.BlogUiTests.make_app

    def test_comment_ids_browser_and_selection_persist_and_share_live_variables(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder, {'blog_id': 'first', 'writer_accounts': [
                {'id': 'secondary', 'browser': 'edge', 'blog_id': 'second', 'enabled': True}]})
            comments = app.comment_accounts_ui
            self.assertEqual(comments.rows[1][3].get(), 'second')
            self.assertIs(comments.rows[1][3], app.writer_accounts_ui.rows[1][3])
            self.assertIs(comments.rows[1][2], app.writer_accounts_ui.rows[1][2])
            comments.rows[1][3].set('newsecond')
            comments.rows[1][2].set('크롬')
            comments.rows[0][1].set(False)
            comments.rows[1][1].set(True)
            comments.save()
            saved = json.loads((Path(folder) / 'settings.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['writer_accounts'][1]['blog_id'], 'newsecond')
            self.assertEqual(saved['writer_accounts'][1]['browser'], 'chrome')
            self.assertEqual(saved['comment_accounts'], {'primary': False, 'secondary': True})
            restored = self.make_app(folder, saved)
            self.assertEqual(restored.comment_accounts_ui.snapshot()[1]['blog_id'], 'newsecond')
            self.assertFalse(restored.comment_accounts_ui.snapshot()[0]['enabled'])
            self.assertTrue(restored.comment_accounts_ui.snapshot()[1]['enabled'])

    def test_model_dropdowns_theme_and_failure_log_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder, {})
            app.cli_step_count.set('4')
            app._save_cli_selection()
            for box in app.cli_stage_model_boxes:
                self.assertIsInstance(box, ttk.Combobox)
            app.cli_order[1].set('Antigravity CLI')
            app._save_cli_selection()
            self.assertIn('gemini-3.8-flash-high', app.cli_stage_model_boxes[1]['values'])
            app.cli_stage_models[1].set('custom-model')
            app._save_cli_selection()
            self.assertIn('custom-model', app.cli_stage_model_boxes[1]['values'])
            app.dark_mode.set(True)
            app._apply_theme()
            box = app.cli_stage_model_boxes[1]
            pop = app.root.tk.call('ttk::combobox::PopdownWindow', str(box))
            self.assertEqual(app.root.tk.call(str(pop)+'.f.l', 'cget', '-background'), '#243b53')
            app.progress_panel.append('계정 회차 failed', account='secondary')
            app.progress_panel.append('정상 처리 완료', account='secondary')
            log = app.progress_panel.account_texts['secondary']
            self.assertIn('failure', log.tag_names('1.0'))
            self.assertNotIn('failure', log.tag_names('2.0'))
            self.assertEqual(log.tag_cget('failure', 'foreground'), '#ff646c')

    def test_overlap_tooltips_exist_and_show_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder, {})
            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)
            helpers = [w._hover_help for w in widgets(app.blog_tab) if hasattr(w, '_hover_help')]
            self.assertEqual(len(helpers), 4)
            for helper in helpers:
                helper.show()
                self.assertIsNotNone(helper.window)
                self.assertIn('30일', helper.text)
                helper.hide()

    def test_selected_comment_accounts_run_concurrently_and_stop_all(self):
        barrier = threading.Barrier(2)
        one, two = Mock(), Mock()
        one.run_own_posts.side_effect = lambda *_: barrier.wait(timeout=2)
        two.run_own_posts.side_effect = lambda *_: barrier.wait(timeout=2)
        group = CommentTaskGroup([(one, 'first'), (two, 'second')])
        group.reset_stop()
        log = Mock()
        group.run('run_own_posts', (30, 15, True), log)
        log.assert_not_called()
        one.run_own_posts.assert_called_once_with('first', 30, 15, True)
        two.run_own_posts.assert_called_once_with('second', 30, 15, True)
        group.stop()
        self.assertTrue(group.stop_event.is_set())
        one.stop.assert_called_once()
        two.stop.assert_called_once()

if __name__ == '__main__':
    unittest.main()
