import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from blog_image_review import image_review_classification_schema
from blog_workflow import (_apply_humanize_response, _apply_fact_recovery_response, BlogWorkflow,
                           WorkflowFormatError, WorkflowError)
from naver_automation import NaverAutomation
import test_blog_workflow as support


class WorkflowNumericImageIntegrationTests(unittest.TestCase):
    setUp = support.BlogWorkflowTests.setUp
    prepare = support.BlogWorkflowTests.prepare

    def test_actual_cover_dimensions_and_classified_minor_quality_reach_publisher(self):
        def classified(review, index):
            review.update(image_review_classification_schema())
            review.update(quality_score=70, issues=['약간의 구도 차이'],
                quality_notes=[{'code': 'composition', 'severity': 'minor', 'detail': '약간의 구도 차이'}])
            if index == 0:
                review['square_1_to_1'] = False
        self.bridge.image_callback = classified
        result = self.prepare()
        cover = result['images'][0]
        review = cover['reviews'][0]
        self.assertTrue(review['approved'])
        self.assertTrue(review['square_1_to_1'])
        self.assertFalse(review['eligibility']['raw_review']['square_1_to_1'])
        self.assertTrue(review['eligibility']['relaxed_quality'])
        call = next(call for call in self.bridge.calls if call['images'])
        context = json.loads(call['prompt'].split('BEGIN_UNTRUSTED_IMAGE_CONTEXT_JSON\n')[1]
                             .split('\nEND_UNTRUSTED_IMAGE_CONTEXT_JSON')[0])
        self.assertEqual(context['decoded_image_size'], {'width': 800, 'height': 800, 'square_1_to_1': True})
        NaverAutomation._validate_publish_article(result)

    def test_changed_review_policy_reuses_exhausted_cover_without_new_generation(self):
        def reject_cover(review, index):
            if index == 0:
                review.update(approved=False, square_1_to_1=False, issues=['미리보기 비율 오판'])
        self.bridge.image_callback = reject_cover
        with patch.object(self.workflow, '_vision_plan_hash', return_value='old-review-policy'):
            with self.assertRaisesRegex(WorkflowError, '첫 사진'):
                self.prepare(image_retry_limit=2)
        manifest_path = next((self.root / 'runs').glob('*/manifest.json'))
        failed = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertEqual(failed['image_generation_attempts']['0'], 3)
        original_generations = len(self.bridge.generations)
        original_file = Path(failed['image_candidates'][0]['path']).read_bytes()
        self.bridge.image_callback = None
        resumed = self.workflow.resume(manifest_path.parent)
        self.assertTrue(resumed['ready_to_publish'])
        self.assertEqual(len(self.bridge.generations), original_generations)
        self.assertEqual(resumed['image_generation_attempts']['0'], 3)
        self.assertEqual(Path(resumed['images'][0]['path']).read_bytes(), original_file)
        self.assertTrue(resumed['images'][0]['previous_vision_reviews'])

    def test_verified_cc_by_photo_keeps_credit_after_korean_caption(self):
        source = self.root / 'google.png'
        support.make_image(source, seed=400)
        page = 'https://commons.wikimedia.org/wiki/File:Example.jpg'
        image = 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Example.jpg/800px-Example.jpg'
        license_url = 'https://creativecommons.org/licenses/by/4.0/'
        candidate = {**NaverAutomation._commons_license_evidence(page, image,
            {'original_file_present': True, 'author': 'Example photographer', 'license_links': [license_url]}),
            'source_url': page, 'image_url': image, 'path': str(source),
            'allow_attribution': True, 'english_source_verified': True}
        result = self.prepare(google_candidates=[candidate])
        selected = result['google_images'][0]
        self.assertTrue(selected['original_text_free'])
        self.assertTrue(selected['caption_applied'])
        self.assertEqual(selected['attribution'], NaverAutomation.reference_attribution_text(selected))
        self.assertIn('한글 설명띠 추가', selected['attribution'])
        self.assertEqual(result['attributions'], [selected['attribution']])
        self.assertNotIn(license_url, result['text'])
        self.assertEqual(len(result['paragraphs']), 8)
        NaverAutomation._validate_publish_article(result)

    def test_numeric_record_is_refreshed_only_against_accepted_style_patch(self):
        article = support.valid_article()
        old = '사용 기간은 30일입니다.'
        new = '사용 기간은 30일이지요.'
        article['paragraphs'][0] += '\n\n' + old
        record = {'subject': '사용 기간', 'value': '30', 'unit': '일', 'section_index': 0, 'quote': old}
        article['numeric_claims'] = [record]
        response = {'paragraph_patches': [{'index': 0, 'old': old, 'new': new}],
                    'numeric_claims': [{**record, 'quote': new}]}
        result, _ = _apply_humanize_response(article, response)
        self.assertEqual(result['numeric_claims'][0]['quote'], new)
        self.assertEqual(result['numeric_claims'][0]['value'], '30')
        self.assertEqual(result['sources'], article['sources'])
        self.assertEqual(article['numeric_claims'], [record])

    def test_writer_prompt_has_one_section_numeric_rule_and_current_camera_layout(self):
        prompt = BlogWorkflow._article_prompt('휴일', ['휴일 확인'], '사용자 문체', editorial_mode='natural')
        self.assertIn('같은 대상·기간의 구체적인 수치는 한 구역에서만', prompt)
        self.assertIn('numeric_claims', prompt)
        self.assertIn('중앙에는 앱이 굵은 고딕체의 흰색과 형광 녹색', prompt)
        self.assertNotIn('아주 약한 미세 필름', prompt)

    def test_unverified_claim_can_be_removed_as_a_complete_sentence_without_new_facts(self):
        article = support.valid_article()
        sentence = '추가 지원 기간은 30일입니다.'
        article['paragraphs'][0] += '\n\n' + sentence
        response = {'fact_corrections': [{'index': 0, 'old': sentence, 'new': '',
            'reason': '지원 기간을 확인할 수 없어 해당 주장 제거', 'source_urls': [], 'issue_index': 0}],
            'fact_additions': [], 'sources': copy.deepcopy(article['sources']), 'changes': ['불확실한 주장 제거']}
        result, _ = _apply_fact_recovery_response(article, response, {'issues': ['지원 기간 근거 없음']}, support.KEYWORDS)
        self.assertNotIn(sentence, result['paragraphs'][0])
        self.assertEqual(result['paragraphs'][1:], article['paragraphs'][1:])
        self.assertEqual(result['review'], article['review'])

    def test_deleting_only_a_condition_or_negative_cannot_turn_it_into_a_different_claim(self):
        article = support.valid_article()
        article['paragraphs'][0] += '\n\n대상에 해당하지 않는 경우에는 지원을 받을 수 없습니다.'
        response = {'fact_corrections': [{'index': 0, 'old': '해당하지 않는 경우에는', 'new': '',
            'reason': '제거', 'source_urls': [], 'issue_index': 0}],
            'fact_additions': [], 'sources': copy.deepcopy(article['sources']), 'changes': []}
        with self.assertRaisesRegex(WorkflowFormatError, '완전한 문장'):
            _apply_fact_recovery_response(article, response, {'issues': ['조건 확인']}, support.KEYWORDS)


if __name__ == '__main__':
    unittest.main()
