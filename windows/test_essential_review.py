import copy
import json
import unittest
from unittest.mock import Mock, patch
import test_blog_workflow as support
from blog_fact_enrichment import requests_for
from blog_workflow import _validate_text_review, WorkflowError

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
        draft['paragraphs'][0] += '\n추가 설명입니다.'
        self.workflow._audit_with_routes(folder, draft, route, [route], {}, 'changed', manifest)
        self.assertEqual(len(self.bridge.calls), 2)

    def test_recent_request_explicitly_uses_google_for_today_yesterday(self):
        _, prompts = requests_for(support.valid_article())
        self.assertIn('Google 검색', prompts['additions'])
        self.assertIn('오늘', prompts['additions'])
        self.assertIn('어제', prompts['additions'])
        self.assertIn('전부 재조사하지 않는다', prompts['facts'])

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
