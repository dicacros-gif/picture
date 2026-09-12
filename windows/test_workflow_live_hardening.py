import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_blog_workflow as support
from blog_cli_bridge import BlogCliError
from blog_workflow import BlogWorkflow, WorkflowError, _canonical_fact_spacing


class LiveWorkflowHardeningTests(unittest.TestCase):
    setUp = support.BlogWorkflowTests.setUp
    prepare = support.BlogWorkflowTests.prepare
    latest_manifest = support.BlogWorkflowTests.latest_manifest
    assert_blocked = support.BlogWorkflowTests.assert_blocked

    def natural_bridge(self, response=None, error=None, reject_audit=False):
        original = self.bridge.run_text
        calls = []
        def run(provider, prompt, **kwargs):
            calls.append((provider, prompt, kwargs))
            if prompt.startswith('EDITORIAL_NATURAL_FINISH'):
                if error:
                    raise error
                return json.dumps(response if response is not None else {'paragraph_patches': []})
            if reject_audit and prompt.startswith('FINAL_ARTICLE_REVIEW'):
                review = copy.deepcopy(self.bridge.article['review'])
                review.update(approved=False, facts_verified=False, issues=['수정된 문장 근거 부족'])
                return json.dumps(review)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        self.inspection = patch('blog_workflow.inspect_article', return_value=[])
        self.inspection.start()
        self.addCleanup(self.inspection.stop)
        return calls

    def natural_options(self):
        return {'steps': ['chatgpt', 'antigravity'], 'editorial_mode': 'natural', 'quality_checks': True,
                'stage_configs': [{'provider': 'chatgpt', 'role': '작성', 'model': 'writer'},
                                  {'provider': 'antigravity', 'role': '팩트·최신 정보 보강', 'model': 'facts'}]}

    def test_user_brief_occurs_once_without_changing_instruction_priority(self):
        brief = '고유한 사용자 문체와 줄바꿈 지침'
        prompt = BlogWorkflow._article_prompt(support.TOPIC, support.KEYWORDS, brief, editorial_mode='natural')
        self.assertEqual(prompt.count(json.dumps({'writing_brief': brief}, ensure_ascii=False)), 1)
        self.assertLess(prompt.index(brief), prompt.index('출력 스키마:'))
        self.assertIn('기본 문체·편집 형식을 유지하면서', prompt)
        self.assertIn('사실 확인·기본 문체·형식 조건 안에서 추가 적용', prompt)

    def test_clean_mechanical_copy_still_gets_one_bounded_finish_and_independent_audit(self):
        calls = self.natural_bridge()
        result = self.prepare(**self.natural_options())
        finishes = [call for call in calls if call[1].startswith('EDITORIAL_NATURAL_FINISH')]
        audits = [call for call in calls if call[1].startswith('FINAL_ARTICLE_REVIEW')]
        self.assertEqual(len(finishes), 1)
        self.assertEqual(finishes[0][0], 'antigravity')
        self.assertEqual(finishes[0][2]['model'], 'facts')
        self.assertEqual(finishes[0][2]['timeout'], 180)
        self.assertEqual([call[0] for call in audits], ['chatgpt'])
        self.assertEqual(result['editorial_quality']['humanization']['patch_count'], 0)
        self.assertTrue(result['ready_to_publish'])

    def test_natural_mode_also_finishes_when_mechanical_option_is_disabled(self):
        calls = self.natural_bridge()
        options = self.natural_options()
        options['quality_checks'] = False
        self.prepare(**options)
        self.assertEqual(sum(call[1].startswith('EDITORIAL_NATURAL_FINISH') for call in calls), 1)

    def test_successful_last_style_stage_is_not_repeated(self):
        calls = self.natural_bridge()
        options = self.natural_options()
        options['stage_configs'][-1]['role'] = '문체 다듬기'
        self.prepare(**options)
        self.assertFalse(any(call[1].startswith('EDITORIAL_NATURAL_FINISH') for call in calls))

    def test_short_sentence_patch_preserves_fact_metadata_and_gets_fresh_audit(self):
        old = '충전 설정을 살피는 순서를 차분하게 익혀두면 좋습니다.'
        new = '충전 설정을 살피는 순서부터 차분하게 익혀두면 좋아요.'
        self.bridge.article['paragraphs'][0] += '\n\n' + old
        self.natural_bridge({'paragraph_patches': [{'index': 0, 'old': old, 'new': new}]})
        result = self.prepare(**self.natural_options())
        self.assertIn(new, result['paragraphs'][0])
        self.assertEqual(result['sources'], self.bridge.article['sources'])
        self.assertEqual(result['editorial_quality']['humanization']['patch_count'], 1)
        self.assertEqual(result['final_reviews'][0]['provider'], 'chatgpt')

    def test_style_amount_change_keeps_original_and_still_requires_independent_audit(self):
        self.bridge.article['paragraphs'][0] += '\n교체 비용은 100만원입니다.'
        self.natural_bridge({'paragraph_patches': [{'index': 0, 'old': '100만원입니다.', 'new': '100원입니다.'}]})
        result = self.prepare(**self.natural_options())
        self.assertIn('100만원입니다.', result['paragraphs'][0])
        self.assertEqual(result['editorial_quality']['humanization']['patch_count'], 0)
        self.assertEqual(len(result['editorial_quality']['humanization']['rejected_patches']), 1)
        self.assertTrue(result['final_reviews'])

    def test_style_full_rewrite_is_rejected_before_image_generation(self):
        self.natural_bridge({'paragraph_patches': [], 'paragraphs': self.bridge.article['paragraphs']})
        self.assert_blocked('전체 재작성', **self.natural_options())
        self.assertFalse(self.bridge.generations)

    def test_style_timeout_has_one_request_and_no_implicit_approval(self):
        calls = self.natural_bridge(error=BlogCliError('timeout', 'bounded timeout'))
        failed = self.assert_blocked('bounded timeout', **self.natural_options())
        self.assertEqual(sum(call[1].startswith('EDITORIAL_NATURAL_FINISH') for call in calls), 1)
        self.assertEqual(failed['final_reviews'], [])
        self.assertFalse(self.bridge.generations)

    def test_style_audit_rejection_is_not_promoted_or_sent_to_images(self):
        self.natural_bridge(reject_audit=True)
        failed = self.assert_blocked('근거 부족', **self.natural_options())
        self.assertFalse(self.bridge.generations)
        self.assertEqual(failed['final_reviews'], [])
        self.assertFalse(failed['final_review_attempts'][0]['review']['approved'])

    def test_approved_finish_checkpoint_repairs_from_intact_pending_approval(self):
        calls = self.natural_bridge()
        self.bridge.bad_image_indices = {0}
        failed = self.assert_blocked('첫 사진', image_retry_limit=2, **self.natural_options())
        with self.assertRaises(WorkflowError):
            self.workflow.resume(failed['run_dir'])
        self.assertEqual(sum(call[1].startswith('EDITORIAL_NATURAL_FINISH') for call in calls), 1)
        self.assertEqual(len(self.bridge.generations), 10)
        checkpoint = Path(failed['run_dir']) / 'editorial.checkpoint.json'
        saved = json.loads(checkpoint.read_text(encoding='utf-8'))
        saved['editorial_quality']['humanization']['article_sha256'] = '0' * 64
        checkpoint.write_text(json.dumps(saved), encoding='utf-8')
        with self.assertRaises(WorkflowError):
            self.workflow.resume(failed['run_dir'])
        # The separate pending-review record still holds the exact approved
        # style copy and its successful audit, so no fresh writing is needed.
        self.assertEqual(sum(call[1].startswith('EDITORIAL_NATURAL_FINISH') for call in calls), 1)
        restored = json.loads(checkpoint.read_text(encoding='utf-8'))
        self.assertNotEqual(restored['editorial_quality']['humanization']['article_sha256'], '0' * 64)
        self.assertEqual(len(self.bridge.generations), 10)

    def test_run_created_callback_is_durable_and_precedes_every_cli_call(self):
        observed = []
        def callback(path):
            folder = Path(path)
            observed.append(path)
            self.assertTrue((folder / 'request.json').is_file())
            self.assertEqual(json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))['status'], 'preparing')
            self.assertFalse(self.bridge.calls)
        result = self.prepare(steps=['chatgpt'], on_run_created=callback)
        self.assertEqual(observed, [result['run_dir']])

    def test_failed_run_pointer_persistence_prevents_untracked_cli_request(self):
        def callback(_path):
            raise OSError('cannot save pending run')
        self.assert_blocked('cannot save pending run', steps=['chatgpt'], on_run_created=callback)
        self.assertFalse(self.bridge.calls)

    def test_google_queries_keep_primary_deduplicate_and_cap_without_extra_cli(self):
        primary = 'laptop battery charging home desk photo'
        self.workflow._text_call = Mock(return_value={'query': primary, 'queries': [primary.upper(),
            'site:example.com laptop battery photo', 'power adapter wooden desk photo',
            'charging cable laptop detail photo', 'laptop keyboard close up photo']})
        result = self.workflow.plan_google_image_search(support.TOPIC, support.KEYWORDS, ['chatgpt'], {})
        self.assertEqual(result['query'], primary)
        self.assertEqual(result['queries'], [primary, 'power adapter wooden desk photo', 'charging cable laptop detail photo'])
        self.workflow._text_call.assert_called_once()
        self.assertIn('실제 사물·장소 명사', self.workflow._text_call.call_args.args[3])

    def test_legacy_single_google_query_returns_compatible_array(self):
        self.workflow._text_call = Mock(return_value={'query': 'laptop battery wooden desk photo'})
        result = self.workflow.plan_google_image_search(support.TOPIC, support.KEYWORDS, ['chatgpt'], {})
        self.assertEqual(result['queries'], [result['query']])

    def fact_response_with_extra_blank_line(self):
        before = copy.deepcopy(self.bridge.article)
        after = copy.deepcopy(before)
        fact = '제조사 지원 안내에서 해당 제품의 충전 제한 기능을 확인할 수 있습니다.'
        after['fact_corrections'] = []
        after['fact_additions'] = [{'index': 0, 'text': fact, 'source_urls': [after['sources'][0]['url']]}]
        after['paragraphs'][0] += '\n\n\n' + fact
        return before, after, fact

    def test_fact_addition_blank_line_only_mismatch_restores_exact_ledger_copy(self):
        before, after, fact = self.fact_response_with_extra_blank_line()
        self.assertTrue(_canonical_fact_spacing('팩트·최신 정보 보강', before, after))
        self.assertEqual(after['paragraphs'][0], before['paragraphs'][0] + '\n\n' + fact)

    def test_fact_spacing_repair_never_hides_changed_words_or_unverified_additions(self):
        for change in ('word', 'space', 'source'):
            with self.subTest(change=change):
                before, after, _ = self.fact_response_with_extra_blank_line()
                if change == 'source':
                    after['fact_additions'][0]['source_urls'] = ['https://unverified.example/document']
                elif change == 'space':
                    after['paragraphs'][0] = after['paragraphs'][0].replace('눈에 보이는', '눈에보이는', 1)
                else:
                    after['paragraphs'][0] = after['paragraphs'][0].replace('눈에 보이는', '새롭게 보이는', 1)
                untouched = copy.deepcopy(after)
                self.assertFalse(_canonical_fact_spacing('팩트·최신 정보 보강', before, after))
                self.assertEqual(after, untouched)

    def test_fact_blank_line_repair_is_audited_and_raw_checkpoint_is_resumable(self):
        _, response, _ = self.fact_response_with_extra_blank_line()
        original = self.bridge.run_text
        calls = []
        def run(provider, prompt, **kwargs):
            calls.append(prompt)
            if provider == 'antigravity' and not kwargs.get('images') and not prompt.startswith('FINAL_ARTICLE_REVIEW'):
                return json.dumps(response)
            return original(provider, prompt, **kwargs)
        self.bridge.run_text = run
        self.bridge.bad_image_indices = {0}
        options = self.natural_options()
        options.update(editorial_mode='strict', quality_checks=False)
        with patch('blog_workflow.inspect_article', return_value=[]):
            failed = self.assert_blocked('첫 사진', image_retry_limit=2, **options)
            with self.assertRaises(WorkflowError):
                self.workflow.resume(failed['run_dir'])
        self.assertEqual(sum(prompt.startswith('FINAL_ARTICLE_REVIEW') for prompt in calls), 1)
        self.assertFalse(any('이전 응답의 구조 오류' in prompt for prompt in calls))
        self.assertEqual(len(self.bridge.generations), 10)
        self.assertEqual(failed['fact_spacing_repairs'][0]['change'], 'extra_blank_lines_only')


if __name__ == '__main__':
    unittest.main()
