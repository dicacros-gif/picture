"""An invalid citation record never justifies regenerating the whole article."""
import copy
import json
import unittest
from unittest.mock import Mock

import test_blog_workflow as support
from blog_workflow import WorkflowReviewRequired


class SourceMetadataRepairTests(unittest.TestCase):
    setUp = support.BlogWorkflowTests.setUp

    def broken(self):
        article = support.valid_article()
        article['sources'][0]['is_primary'] = False
        return article

    def answer(self, article):
        source = copy.deepcopy(article['sources'][0])
        source.update(is_primary=True, url='https://manufacturer.example.com/battery-guide', title='Original manufacturer guide')
        return {'replacements': [{'index': 0, 'source': source}], 'unverifiable': []}

    def test_one_small_request_changes_only_verified_source_records(self):
        article = self.broken()
        self.workflow._text_call = Mock(return_value={**self.answer(article), 'title': 'ignored', 'paragraphs': ['ignored']})
        result = self.workflow._repair_source_metadata(self.root, 'draft', 'chatgpt', {}, article)
        call = self.workflow._text_call.call_args
        self.assertLessEqual(len(call.args[3]), 5000)
        self.assertNotIn(article['paragraphs'][0], call.args[3])
        self.assertEqual(call.kwargs['timeout'], 120)
        self.assertFalse(call.kwargs['retry_transient'])
        self.assertEqual(result['paragraphs'], article['paragraphs'])
        self.assertEqual(result['title'], article['title'])
        self.assertEqual(result['image_prompts'], article['image_prompts'])
        self.assertEqual(result['review'], article['review'])
        self.assertTrue(result['sources'][0]['is_primary'])
        self.assertFalse(article['sources'][0]['is_primary'])

    def test_false_flags_and_missing_claims_are_not_accepted(self):
        for field in ('is_primary', 'verified', 'supports'):
            with self.subTest(field=field):
                article = self.broken()
                answer = self.answer(article)
                answer['replacements'][0]['source'][field] = ['a different claim'] if field == 'supports' else False
                self.workflow._text_call = Mock(return_value=answer)
                with self.assertRaises(WorkflowReviewRequired):
                    self.workflow._repair_source_metadata(self.root, 'draft', 'chatgpt', {}, article)
                self.workflow._text_call.assert_called_once()
                self.assertFalse(article['sources'][0]['is_primary'])

    def test_unverifiable_response_stops_with_evidence_not_full_rewrite(self):
        article = self.broken()
        self.workflow._text_call = Mock(return_value={'replacements': [], 'unverifiable': [{'index': 0, 'reason': 'no original record'}]})
        with self.assertRaises(WorkflowReviewRequired):
            self.workflow._repair_source_metadata(self.root, 'draft', 'chatgpt', {}, article)
        self.workflow._text_call.assert_called_once()

    def test_oversized_claim_request_does_not_start_cli(self):
        article = self.broken()
        article['sources'][0]['supports'] = ['기존 주장 원문' * 1000]
        self.workflow._text_call = Mock()
        with self.assertRaises(WorkflowReviewRequired):
            self.workflow._repair_source_metadata(self.root, 'draft', 'chatgpt', {}, article)
        self.workflow._text_call.assert_not_called()

    def test_actual_workflow_recovers_source_without_rewriting_or_provider_fallback(self):
        self.bridge.article = self.broken()
        original = self.bridge.run_text
        prompts = []
        def text(provider, prompt, **kwargs):
            prompts.append(prompt)
            if prompt.startswith('SOURCE_METADATA_REPAIR'):
                return json.dumps(self.answer(self.bridge.article))
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = text
        result = self.workflow.prepare(support.TOPIC, support.KEYWORDS, '지침', ['chatgpt'],
                                       '마지막 CLI 집중 검수', early_image_finish=True, essential_review=True)
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(result['paragraphs'], self.bridge.article['paragraphs'])
        self.assertEqual(sum(p.startswith('SOURCE_METADATA_REPAIR') for p in prompts), 1)
        self.assertFalse(any('동일 주제 복구 단계' in p for p in prompts))
        self.assertEqual(result['sources'][0]['url'], 'https://manufacturer.example.com/battery-guide')

    def test_failed_source_repair_does_not_enter_whole_article_fallback(self):
        self.bridge.article = self.broken()
        original = self.bridge.run_text
        prompts = []
        def text(provider, prompt, **kwargs):
            prompts.append(prompt)
            if prompt.startswith('SOURCE_METADATA_REPAIR'):
                return json.dumps({'replacements': [], 'unverifiable': [{'index': 0}]})
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = text
        stages = [{'provider': 'chatgpt', 'role': '작성', 'model': ''},
                  {'provider': 'antigravity', 'role': '팩트·최신 정보 보강', 'model': ''}]
        with self.assertRaises(WorkflowReviewRequired):
            self.workflow.prepare(support.TOPIC, support.KEYWORDS, '지침', ['chatgpt', 'antigravity'],
                                  '단계별 교차 검수', stage_configs=stages, essential_review=True)
        self.assertEqual(sum(p.startswith('SOURCE_METADATA_REPAIR') for p in prompts), 1)
        self.assertFalse(any('동일 주제 복구 단계' in p for p in prompts))
        self.assertFalse(self.bridge.generations)

    def test_resume_saved_source_error_uses_original_draft_before_any_new_writing(self):
        self.bridge.article = self.broken()
        original = self.bridge.run_text
        prompts = []
        fixed = [False]
        def text(provider, prompt, **kwargs):
            prompts.append(prompt)
            if prompt.startswith('SOURCE_METADATA_REPAIR'):
                response = self.answer(self.bridge.article) if fixed[0] else {'replacements': [], 'unverifiable': [{'index': 0}]}
                return json.dumps(response)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = text
        with self.assertRaises(WorkflowReviewRequired) as failed:
            self.workflow.prepare(support.TOPIC, support.KEYWORDS, '지침', ['chatgpt'],
                                  '단계별 교차 검수', early_image_finish=True, essential_review=True)
        fixed[0] = True
        prompts.clear()
        result = self.workflow.resume(failed.exception.run_dir)
        self.assertTrue(result['ready_to_publish'])
        self.assertTrue(prompts[0].startswith('SOURCE_METADATA_REPAIR'))
        self.assertFalse(any('사용자 글쓰기 지침(' in p for p in prompts))
        self.assertEqual(result['paragraphs'], self.bridge.article['paragraphs'])

    def test_first_writer_brief_is_included_once_without_truncation(self):
        brief = '사용자가 저장한 고유한 지침을 그대로 유지합니다.' * 150
        prompt = self.workflow._article_prompt(support.TOPIC, support.KEYWORDS, brief)
        self.assertEqual(prompt.count(json.dumps({'writing_brief': brief}, ensure_ascii=False)), 1)


if __name__ == '__main__':
    unittest.main()
