import copy
import json
import unittest
from unittest.mock import Mock, patch
import test_blog_workflow as support
from blog_fact_enrichment import requests_for
from blog_workflow import _validate_text_review, _validate_article, _normalize_visual_emphasis, WorkflowError

class EssentialReviewTests(unittest.TestCase):
    setUp = support.BlogWorkflowTests.setUp

    def test_minor_notes_approve_but_material_error_does_not(self):
        review = support.valid_article()['review']
        review.update(minor_notes=['비유를 더 넣으면 좋습니다.'], review_scope='critical_only')
        _validate_text_review(review)
        review.update(approved=False, facts_verified=False, issues=['서로 같은 대상의 금액이 다릅니다.'])
        with self.assertRaises(WorkflowError):
            _validate_text_review(review)

    def test_critical_prompt_and_unchanged_source_copy_reuse_one_final_audit(self):
        self.workflow.essential_review = True
        draft = support.valid_article()
        route = {'provider': 'chatgpt', 'model': 'writer'}
        manifest = {'final_reviews': []}
        folder = self.root / 'final'; folder.mkdir()
        one = self.workflow._audit_with_routes(folder, draft, route, [route], {}, 'one', manifest)
        two = self.workflow._audit_with_routes(folder, draft, route, [route], {}, 'two', manifest)
        self.assertEqual(one, two)
        self.assertEqual(len(self.bridge.calls), 1)
        self.assertIn('중대한 문제', self.bridge.calls[0]['prompt'])
        self.assertIn('minor_notes', self.bridge.calls[0]['prompt'])
        self.assertIn('검색 의도의 핵심 질문', self.bridge.calls[0]['prompt'])
        self.assertIn('조건·이유·절차', self.bridge.calls[0]['prompt'])
        self.assertIn('핵심 답이 빠진 원고', self.bridge.calls[0]['prompt'])
        self.assertIn('모든 구역을 다시 쓰라고 요구하지 않는다', self.bridge.calls[0]['prompt'])
        draft['paragraphs'][0] += '\n추가 설명입니다.'
        self.workflow._audit_with_routes(folder, draft, route, [route], {}, 'changed', manifest)
        self.assertEqual(len(self.bridge.calls), 2)

    def test_changed_source_and_selected_model_invalidate_final_audit_reuse(self):
        self.workflow.essential_review = True
        draft = support.valid_article()
        route = {'provider': 'chatgpt', 'model': 'writer'}
        manifest = {'final_reviews': []}
        folder = self.root / 'changed-evidence'; folder.mkdir()
        self.workflow._audit_with_routes(folder, draft, route, [route], {}, 'first', manifest)
        draft['sources'][0]['supports'].append('실제로 확인한 추가 조건입니다.')
        self.workflow._audit_with_routes(folder, draft, route, [route], {}, 'sources', manifest)
        route = {**route, 'model': 'reviewer'}
        self.workflow._audit_with_routes(folder, draft, route, [route], {}, 'model', manifest)
        self.assertEqual(len(self.bridge.calls), 3)

    def test_recent_request_explicitly_uses_google_for_today_yesterday(self):
        _, prompts = requests_for(support.valid_article())
        self.assertIn('Google 검색', prompts['additions'])
        self.assertIn('오늘', prompts['additions'])
        self.assertIn('어제', prompts['additions'])
        self.assertIn('전부 재조사하지 않는다', prompts['facts'])

    def test_bad_emphasis_metadata_is_dropped_without_changing_article_or_approval(self):
        draft = support.valid_article()
        before = copy.deepcopy(draft)
        draft['highlight_phrases'] = ['단어', '존재하지 않는 강조 문장을 넣었어요.', None, {'invalid': True}]
        draft['bold_phrases'] = 'not an array'
        _validate_article(draft, support.KEYWORDS, require_visual_style=True)
        self.assertEqual(draft['highlight_phrases'], [])
        self.assertEqual(draft['bold_phrases'], [])
        for field in ('paragraphs', 'title', 'review', 'sources'):
            self.assertEqual(draft[field], before[field])

    def test_grouped_full_sentence_can_still_be_highlighted(self):
        draft = support.valid_article()
        phrase = draft['highlight_phrases'][0]
        for index, paragraph in enumerate(draft['paragraphs']):
            if phrase in paragraph:
                draft['paragraphs'][index] = paragraph.replace(phrase, phrase + ' 다음에는 이용 조건을 따로 살펴보지요.')
                break
        _normalize_visual_emphasis(draft)
        self.assertEqual(draft['highlight_phrases'], [phrase])

    def test_emphasis_cleanup_never_overrides_explicit_fact_failure(self):
        draft = support.valid_article()
        draft['highlight_phrases'] = ['잘못된 조각']
        draft['review'].update(approved=False, facts_verified=False, issues=['수치 충돌이 남아 있습니다.'])
        with self.assertRaises(WorkflowError):
            _validate_article(draft, support.KEYWORDS, require_visual_style=True)
        self.assertFalse(draft['review']['facts_verified'])

    def test_writer_prompt_preserves_sentence_lines_and_excludes_secondary_references(self):
        prompt = self.workflow._article_prompt(support.TOPIC, support.KEYWORDS, '사용자 지침', editorial_mode='natural')
        self.assertIn('묶음 내부 문장 사이는 \\n', prompt)
        self.assertIn('단순 재게시', prompt)
        self.assertIn('true로 꾸며 통과시키지 말고', prompt)
        self.assertIn('처음 6개만으로도 서로 다른 핵심 설명', prompt)

    def test_essential_flow_skips_repeated_targeted_and_extra_humanization_calls(self):
        stages = [{'provider': 'chatgpt', 'model': 'writer', 'role': '작성'},
                  {'provider': 'chatgpt', 'model': 'finish', 'role': '문체 다듬기'}]
        with patch('blog_workflow.inspect_article', return_value=[]):
            result = self.workflow.prepare(support.TOPIC, support.KEYWORDS, '사용자 지침',
                ['chatgpt', 'chatgpt'], '마지막 CLI 집중 검수', stage_configs=stages,
                quality_checks=True, editorial_mode='natural', essential_review=True)
        self.assertTrue(result['ready_to_publish'])
        prompts = [call['prompt'] for call in self.bridge.calls]
        self.assertFalse(any(value.startswith(('EDITORIAL_TARGETED_REPAIR', 'EDITORIAL_NATURAL_FINISH')) for value in prompts))
        request = json.loads((self.root / 'runs' / Path(result['run_dir']).name / 'request.json').read_text(encoding='utf-8'))
        self.assertTrue(request['essential_review'])

from pathlib import Path
if __name__ == '__main__':
    unittest.main()
