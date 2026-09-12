import copy
import unittest
from tkinter import Tk, ttk
from types import SimpleNamespace
from unittest.mock import Mock

from blog_preferences import normalize_preferences
from blog_stage_roles import check_role_change, role_prompt
from blog_workflow import _canonical_title_intent, _validate_article, WorkflowFormatError
from progress_panel import ProgressPanel
from test_blog_workflow import valid_article, TOPIC, KEYWORDS


class IntentAndRoleTests(unittest.TestCase):
    def test_reviewer_adding_root_topic_does_not_reject_valid_intent(self):
        article = valid_article()
        article['title_intent']['related_keywords'] = [TOPIC, *KEYWORDS]
        _canonical_title_intent(article, TOPIC, KEYWORDS)
        self.assertEqual(article['title_intent']['related_keywords'], KEYWORDS)
        _validate_article(article, KEYWORDS)

    def test_invented_keywords_require_same_article_repair(self):
        article = valid_article()
        article['title_intent']['related_keywords'] = [TOPIC, 'invented term']
        _canonical_title_intent(article, TOPIC, KEYWORDS)
        with self.assertRaises(WorkflowFormatError):
            _validate_article(article, KEYWORDS)

    def test_facts_allow_additions_but_never_rewrite(self):
        old = valid_article()
        new = copy.deepcopy(old)
        new['paragraphs'][0] += '\n확인된 새 정보입니다.'
        check_role_change('팩트·최신 정보 보강', old, new)
        new['paragraphs'][1] = '바뀐 내용'
        with self.assertRaises(ValueError):
            check_role_change('팩트·최신 정보 보강', old, new)

    def test_style_cannot_change_numbers(self):
        old = valid_article()
        new = copy.deepcopy(old)
        new['paragraphs'][0] += ' 2027년입니다.'
        with self.assertRaises(ValueError):
            check_role_change('문체 다듬기', old, new)

    def test_stage_models_and_roles_roundtrip(self):
        pref = normalize_preferences(None, '사용자 프롬프트')
        pref['stages'][0]['model'] = 'first-model'
        pref['stages'][3]['model'] = 'last-model'
        pref['stages'][1]['role'] = '문체 다듬기'
        loaded = normalize_preferences(pref, 'new default')
        self.assertEqual(loaded['stages'], pref['stages'])
        self.assertEqual(loaded['prompts'], pref['prompts'])

    def test_tool_attribution_requires_repair(self):
        article = valid_article()
        article['paragraphs'][0] += '\nAntigravity가 확인했습니다.'
        with self.assertRaises(WorkflowFormatError):
            _validate_article(article, KEYWORDS)
        self.assertIn('제공되지 않은 개인 체험', role_prompt('문체 다듬기', True))


class ProgressPanelTests(unittest.TestCase):
    def test_resize_collapse_restore_and_log_limit(self):
        root = Tk()
        root.geometry('900x700+0+0')
        self.addCleanup(root.destroy)
        app = SimpleNamespace(root=root, settings={'progress_pane_height': 180}, _persist_cli_preferences=Mock())
        panel = ProgressPanel(app, root)
        panel.notebook.add(ttk.Frame(panel.notebook), text='one')
        panel.notebook.add(ttk.Frame(panel.notebook), text='two')
        root.update()
        panel._position()
        self.assertAlmostEqual(panel.split.winfo_height() - panel.split.sashpos(0), 180, delta=5)
        panel.split.sashpos(0, panel.split.winfo_height() - 240)
        panel.save_height()
        self.assertAlmostEqual(app.settings['progress_pane_height'], 240, delta=5)
        panel.toggle()
        self.assertTrue(app.settings['progress_pane_collapsed'])
        panel.toggle()
        root.update()
        self.assertAlmostEqual(panel.split.winfo_height() - panel.split.sashpos(0), 240, delta=5)
        panel.notebook.select(1)
        panel.append('\n'.join(str(i) for i in range(5100)))
        self.assertLessEqual(int(panel.text.index('end-1c').split('.')[0]), 5000)
        self.assertIn('5099', panel.text.get('1.0', 'end'))


if __name__ == '__main__':
    unittest.main()
