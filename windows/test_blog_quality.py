import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from blog_quality import inspect_article, apply_patches, local_cleanup
from blog_workflow import BlogWorkflow, WorkflowFormatError, rank_topics
from blog_stage_roles import check_role_change
from test_blog_workflow import valid_article, KEYWORDS, TOPIC
import test_blog_controls as ui_tests


class EditorialTests(unittest.TestCase):
    def test_checks_report_structural_style_and_metadata_problems(self):
        article = valid_article()
        article['paragraphs'][0] += '\n있습니다. 있습니다. 있습니다.\nAntigravity가 검수했습니다.\n출처: https://example.com\n또한 확인해요.'
        article['paragraphs'][7] = '짧습니다.'
        codes = {i['code'] for i in inspect_article(article, KEYWORDS, TOPIC)}
        self.assertTrue({'section_length', 'ending', 'tool_attribution', 'public_source', 'forbidden',
            'bridge', 'heading_keyword', 'density', 'duplication'} <= codes)
        article['paragraphs'] = ['짧아요.'] * 8
        codes = {i['code'] for i in inspect_article(article, KEYWORDS, TOPIC)}
        self.assertTrue({'total_length', 'specificity'} <= codes)
        article['paragraphs'].pop()
        self.assertEqual(inspect_article(article, KEYWORDS, TOPIC)[0]['code'], 'sections')

    def test_metadata_must_be_in_actual_heading_and_copy(self):
        article = valid_article()
        keys = [TOPIC + f' 조건{i}' for i in range(8)]
        article['subheading_keywords'] = list(keys)
        article['bridge_sentences'] = []
        for i in range(8):
            article['paragraphs'][i] = article['paragraphs'][i].replace(f'배터리의 숨은 변화 {i + 1}', keys[i])
            line = f'앞서 살펴본 기준에서 {i + 1}번째 확인으로 이어갈 수 있어요.'
            article['paragraphs'][i] += '\n' + line
            article['bridge_sentences'].append(line)
        codes = {i['code'] for i in inspect_article(article, keys, TOPIC)}
        self.assertFalse({'bridge', 'heading_keyword', 'heading_duplicate'} & codes)
        article['subheading_keywords'][1] = keys[0]
        article['paragraphs'][1] = article['paragraphs'][1].replace(keys[1], keys[0])
        self.assertIn('heading_duplicate', {i['code'] for i in inspect_article(article, keys, TOPIC)})

    def test_patch_cannot_rewrite_unreported_section_or_approval(self):
        article = valid_article()
        issues = [{'index': 0, 'code': 'ending'}]
        with self.assertRaises(ValueError):
            apply_patches(article, {'paragraph_patches': [{'index': 1, 'old': article['paragraphs'][1], 'new': '변경'}]}, issues)
        result = apply_patches(article, {'paragraph_patches': [], 'review': {'approved': False}}, issues)
        self.assertEqual(result['review'], article['review'])

    def test_local_cleanup_removes_sources_and_varies_safe_endings(self):
        article = valid_article()
        article['paragraphs'][0] += '\nAntigravity가 확인했습니다.\n참고 자료: http://example.com\n가능합니다.'
        issues = [{'index': 0, 'code': code, 'text': text} for code, text in [
            ('tool_attribution', 'Antigravity가 확인했습니다.'),
            ('public_source', '참고 자료: http://example.com'), ('ending', '가능합니다.')]]
        result, changes = local_cleanup(article, issues)
        self.assertEqual(len(changes), 3)
        self.assertNotIn('http', result['paragraphs'][0])
        self.assertIn('가능해요.', result['paragraphs'][0])
        self.assertEqual(result['review'], article['review'])

    def test_two_cli_repairs_then_code_cleanup_no_third_request(self):
        with tempfile.TemporaryDirectory() as folder:
            workflow = BlogWorkflow(Mock(), Path(folder), Mock())
            workflow._text_call = Mock(return_value={'paragraph_patches': []})
            issue = {'index': 0, 'code': 'section_length', 'text': '', 'detail': 'short'}
            with patch('blog_workflow.inspect_article', return_value=[issue]), \
                 patch('blog_workflow.local_cleanup', return_value=(valid_article(), [])) as cleanup:
                manifest = {'final_reviews': []}
                result = workflow._repair_editorial(Path(folder), valid_article(), KEYWORDS, TOPIC, '지침', ['chatgpt'], {}, None, manifest)
            self.assertEqual(workflow._text_call.call_count, 2)
            cleanup.assert_called_once()
            self.assertEqual(result['title'], valid_article()['title'])
            self.assertEqual(manifest['editorial_quality']['status'], 'editorial_followup')
            self.assertTrue((Path(folder) / 'editorial-quality.json').exists())

    def test_repaired_copy_is_audited_before_continuing(self):
        with tempfile.TemporaryDirectory() as folder:
            workflow = BlogWorkflow(Mock(), Path(folder), Mock())
            article = valid_article()
            old = '가능합니다.'
            article['paragraphs'][0] += '\n' + old
            issue = {'index': 0, 'code': 'ending', 'text': old, 'detail': '어미 반복'}
            workflow._text_call = Mock(return_value={'paragraph_patches': [{'index': 0, 'old': old, 'new': '가능해요.'}]})
            workflow._audit_final_article = Mock(return_value={'provider': 'chatgpt', 'review': article['review']})
            manifest = {'final_reviews': []}
            with patch('blog_workflow.inspect_article', side_effect=[[issue], [], [], [], []]):
                result = workflow._repair_editorial(Path(folder), article, KEYWORDS, TOPIC, '지침', ['chatgpt'], {}, None, manifest)
            self.assertIn('가능해요.', result['paragraphs'][0])
            workflow._text_call.assert_called_once()
            workflow._audit_final_article.assert_called_once()
            self.assertEqual(len(manifest['final_reviews']), 1)

    def test_code_expansion_uses_only_verified_unused_supporting_claims(self):
        article = valid_article()
        article['paragraphs'][0] = '──────────────\n❝ 배터리 조건\n배터리를 살펴봐요.'
        article['subheading_keywords'] = ['배터리 조건'] * 8
        verified = '배터리 사용 조건은 해당 기기의 제조사 안내에서 확인할 수 있습니다.'
        unverified = '배터리의 가격은 100만원으로 확정됐습니다.'
        article['sources'][0]['supports'] = [verified]
        article['sources'].append({'verified': False, 'is_primary': True, 'supports': [unverified]})
        result, changes = local_cleanup(article, [{'code': 'section_length', 'index': 0, 'text': ''}])
        self.assertIn(verified, result['paragraphs'][0])
        self.assertNotIn(unverified, result['paragraphs'][0])
        self.assertEqual(changes[0]['code'], 'verified_expansion')

    def test_more_unique_related_keywords_rank_first_even_at_score_cap(self):
        groups = {'a': ['배터리 설정', '충전 설정'], 'b': ['배터리 설정', '충전 설정'], 'c': ['배터리 설정', '충전 설정']}
        related = {'배터리 설정': [f'배터리 설정 방법{i}' for i in range(12)],
                   '충전 설정': [f'충전 설정 방법{i}' for i in range(20)]}
        result = rank_topics(groups, related)
        self.assertEqual(result[0]['topic'], '충전 설정')
        self.assertEqual(result[0]['related_bonus'], 80)
        related['배터리 설정'] *= 10
        self.assertEqual(rank_topics(groups, related)[0]['topic'], '충전 설정')

    def test_facts_stage_can_replace_documented_unverified_number_only(self):
        old = valid_article()
        old['paragraphs'][0] += '\n교체 비용은 100만원입니다.'
        new = copy.deepcopy(old)
        new['paragraphs'][0] = new['paragraphs'][0].replace('교체 비용은 100만원입니다.', '교체 비용은 제품별 조건을 확인해야 합니다.')
        new['fact_corrections'] = [{'index': 0, 'old': '교체 비용은 100만원입니다.',
            'new': '교체 비용은 제품별 조건을 확인해야 합니다.', 'reason': '확인되지 않은 금액 삭제',
            'source_urls': [new['sources'][0]['url']]}]
        check_role_change('팩트·최신 정보 보강', old, new)
        new['fact_corrections'][0]['source_urls'] = ['https://unverified.example']
        with self.assertRaises(ValueError):
            check_role_change('팩트·최신 정보 보강', old, new)


class UiCleanupTests(unittest.TestCase):
    def test_buttons_removed_and_single_completion_control(self):
        with tempfile.TemporaryDirectory() as folder:
            app = ui_tests.BlogUiTests.make_app(self, folder, {})
            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)
            all_widgets = list(widgets(app.root))
            labels = [str(w.cget('text')) for w in all_widgets if 'text' in w.keys()]
            for label in ('작업 중지', '관심 주제 자동 선정', '글·이미지 준비', '준비된 글 네이버에 입력·실행', '선택 검색어 → Google 이미지 검색'):
                self.assertNotIn(label, labels)
            modes = [w for w in all_widgets if 'textvariable' in w.keys() and str(w.cget('textvariable')) == str(app.cli_publication)]
            self.assertEqual(len(modes), 1)
            app.naver_bot.stop = Mock()
            app.stop_full_automation()
            app.naver_bot.stop.assert_called_once()
            self.assertTrue(app.full_auto_stop.is_set())


if __name__ == '__main__':
    unittest.main()
