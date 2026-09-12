import copy
import unittest

from blog_workflow import _apply_humanize_response, WorkflowFormatError
from test_blog_workflow import valid_article
import test_workflow_live_hardening as live_support


class HumanizePartialTests(unittest.TestCase):
    def article_and_response(self, invalid_new):
        article = valid_article()
        old = '예정된 휴일은 70일입니다.'
        style_old = '기준을 차분하게 살펴보면 준비할 순서를 찾을 수 있습니다.'
        style_new = '기준부터 차분히 살펴보면 무엇을 준비할지 감이 잡히지요.'
        article['paragraphs'][0] += '\n\n' + old
        article['paragraphs'][1] += '\n\n' + style_old
        bridges = ['배터리의 숨은 변화 ' + str(i + 1) for i in range(8)]
        bridges[0], bridges[1] = old, style_old
        article['bridge_sentences'] = bridges
        article['highlight_phrases'] = [old, style_old]
        response = {'paragraph_patches': [{'index': 0, 'old': old, 'new': invalid_new},
                                          {'index': 1, 'old': style_old, 'new': style_new}],
                    'bridge_sentences': [invalid_new, style_new, *bridges[2:]],
                    'highlight_phrases': [invalid_new, style_new], 'bold_phrases': [invalid_new, style_new]}
        return article, response

    def test_numeric_change_or_repetition_rejects_only_that_patch(self):
        for invalid in ('예정된 휴일은 71일입니다.', '예정된 휴일은 70일입니다. 이 70일 중에서 살펴볼까요?'):
            with self.subTest(invalid=invalid):
                article, response = self.article_and_response(invalid)
                original = copy.deepcopy(article)
                result, details = _apply_humanize_response(article, response)
                self.assertEqual(article, original)
                self.assertEqual(result['paragraphs'][0], article['paragraphs'][0])
                self.assertIn(response['paragraph_patches'][1]['new'], result['paragraphs'][1])
                self.assertEqual(details['patch_count'], 1)
                self.assertEqual(details['accepted_patch_numbers'], [1])
                self.assertEqual(details['rejected_patches'][0]['patch_number'], 0)
                self.assertIn('수치·날짜·단위', details['rejected_patches'][0]['reason'])
                self.assertEqual(result['sources'], article['sources'])
                self.assertEqual(result['review'], article['review'])

    def test_metadata_describes_only_accepted_text_and_preserves_rejected_original(self):
        article, response = self.article_and_response('예정된 휴일은 71일입니다.')
        result, details = _apply_humanize_response(article, response)
        self.assertEqual(result['bridge_sentences'][0], article['bridge_sentences'][0])
        self.assertEqual(result['bridge_sentences'][1], response['bridge_sentences'][1])
        for index, bridge in enumerate(result['bridge_sentences']):
            self.assertIn(bridge, result['paragraphs'][index])
        for field in ('highlight_phrases', 'bold_phrases'):
            self.assertNotIn(response['paragraph_patches'][0]['new'], result[field])
            self.assertTrue(all(any(value in paragraph for paragraph in result['paragraphs']) for value in result[field]))
        self.assertIn(article['highlight_phrases'][0], result['highlight_phrases'])
        self.assertTrue(details['metadata_filtered'])

    def test_omitted_metadata_follows_valid_replacement(self):
        article, response = self.article_and_response('예정된 휴일은 71일입니다.')
        result, _ = _apply_humanize_response(article, {'paragraph_patches': response['paragraph_patches'][1:]})
        self.assertEqual(result['bridge_sentences'][1], response['paragraph_patches'][1]['new'])
        self.assertIn(response['paragraph_patches'][1]['new'], result['highlight_phrases'])

    def test_limits_and_ambiguous_originals_remain_structural_errors(self):
        article, response = self.article_and_response('예정된 휴일은 71일입니다.')
        invalids = [{'paragraph_patches': response['paragraph_patches'] * 5},
                    {'paragraph_patches': [{'index': 0, 'old': '없는 원문', 'new': '새 문장'}]},
                    {'paragraph_patches': [{'index': 0, 'old': '짧음', 'new': '가' * 751}]},
                    {'paragraph_patches': [], 'paragraphs': article['paragraphs']}]
        for response in invalids:
            with self.subTest(response=response), self.assertRaises(WorkflowFormatError):
                _apply_humanize_response(article, response)


class HumanizePartialAuditGateTests(unittest.TestCase):
    setUp = live_support.LiveWorkflowHardeningTests.setUp
    prepare = live_support.LiveWorkflowHardeningTests.prepare
    latest_manifest = live_support.LiveWorkflowHardeningTests.latest_manifest
    assert_blocked = live_support.LiveWorkflowHardeningTests.assert_blocked
    natural_bridge = live_support.LiveWorkflowHardeningTests.natural_bridge
    natural_options = live_support.LiveWorkflowHardeningTests.natural_options

    def test_partial_style_recovery_cannot_skip_final_fact_rejection(self):
        self.bridge.article['paragraphs'][0] += '\n교체 비용은 100만원입니다.'
        calls = self.natural_bridge({'paragraph_patches': [
            {'index': 0, 'old': '100만원입니다.', 'new': '100원입니다.'}]}, reject_audit=True)
        failed = self.assert_blocked('근거 부족', **self.natural_options())
        self.assertEqual(len(failed['editorial_quality']['humanization']['rejected_patches']), 1)
        self.assertEqual(failed['final_reviews'], [])
        self.assertFalse(self.bridge.generations)
        self.assertEqual(sum(prompt.startswith('FINAL_ARTICLE_REVIEW') for _, prompt, _ in calls), 1)


if __name__ == '__main__':
    unittest.main()
