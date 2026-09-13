"""New CLI cover questions and unchanged legacy checkpoints have distinct rules."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import blog_workflow as workflow
import test_blog_workflow as support


class OverlayQuestionValidationTests(unittest.TestCase):
    def test_fresh_question_requires_spacing_and_final_question_mark(self):
        for value in ('배터리수명왜짧을까요?', '배터리 수명 왜 짧을까요', '쉬는날판도 ?',
                      '배터리\n수명 왜 짧을까?', {'문구': '왜 그럴까?'}, '가' * 29 + '?'):
            with self.subTest(value=value):
                article = support.valid_article()
                article['cover_headline'] = value
                with self.assertRaisesRegex(workflow.WorkflowFormatError, 'cover_headline:'):
                    workflow._validate_article(article, support.KEYWORDS, require_visual_style=True,
                                               require_overlay_question=True)

    def test_fresh_valid_question_preserves_words_and_does_not_rewrite_article(self):
        article = support.valid_article()
        article['cover_headline'] = '배터리는 왜 빨리 닳을까요?'
        before = copy.deepcopy(article)
        workflow._validate_article(article, support.KEYWORDS, require_visual_style=True, require_overlay_question=True)
        self.assertEqual(article, before)

    def test_legacy_validation_does_not_add_question_mark_or_change_hash(self):
        article = support.valid_article()
        article['cover_headline'] = '쉬는날판도'
        before = workflow._json_hash(article)
        workflow._validate_article(article, support.KEYWORDS, require_visual_style=True)
        self.assertEqual(workflow._json_hash(article), before)
        self.assertEqual(article['cover_headline'], '쉬는날판도')


class OverlayQuestionWorkflowTests(unittest.TestCase):
    setUp = support.BlogWorkflowTests.setUp
    prepare = support.BlogWorkflowTests.prepare
    latest_manifest = support.BlogWorkflowTests.latest_manifest

    def test_format_retry_repairs_only_cover_before_generation(self):
        self.bridge.article['cover_headline'] = '배터리수명왜짧을까'
        old_body = copy.deepcopy(self.bridge.article['paragraphs'])
        def repair(result, call_number):
            if call_number == 2:
                result['cover_headline'] = '배터리 수명 왜 짧을까?'
        self.bridge.article_callback = repair
        result = self.prepare(steps=['chatgpt'])
        calls = [call for call in self.bridge.calls if not call['images']]
        self.assertEqual(len(calls), 2)
        self.assertIn('cover_headline:', calls[1]['prompt'])
        self.assertIn('본문을 다시 쓰지 말고', calls[1]['prompt'])
        self.assertEqual(result['paragraphs'], old_body)
        self.assertEqual(result['cover_headline'], '배터리 수명 왜 짧을까?')
        checkpoint = json.loads((Path(result['run_dir']) / 'stage-1-chatgpt.checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(checkpoint['response_name'], 'stage-1-chatgpt-format-retry')
        self.assertEqual(len(self.bridge.generations), 8)

    def test_cover_only_retry_cannot_change_body_or_start_images(self):
        self.bridge.article['cover_headline'] = '배터리수명왜짧을까'
        def rewrite(result, call_number):
            if call_number == 2:
                result['cover_headline'] = '배터리 수명 왜 짧을까?'
                result['paragraphs'][0] += '\n\n새로 바꾼 본문입니다.'
        self.bridge.article_callback = rewrite
        with self.assertRaisesRegex(workflow.WorkflowError, '본문·출처·메타데이터를 변경할 수 없습니다'):
            self.prepare(steps=['chatgpt'])
        self.assertEqual(len(self.bridge.calls), 2)
        self.assertFalse(self.bridge.generations)

    def test_invalid_question_after_one_format_retry_is_not_automatically_approved(self):
        self.bridge.article['cover_headline'] = '배터리 수명 왜 짧을까'
        with self.assertRaisesRegex(workflow.WorkflowError, 'cover_headline:'):
            self.prepare(steps=['chatgpt'])
        self.assertEqual(len(self.bridge.calls), 2)
        self.assertFalse(self.bridge.generations)

    def test_legacy_approved_stage_is_reused_without_new_question_validation(self):
        # Build a historical checkpoint with the previously valid statement.
        self.bridge.article['cover_headline'] = '쉬는날판도'
        self.bridge.missing_image_index = 2
        validate = workflow._validate_article
        def legacy(*args, **kwargs):
            kwargs.pop('require_overlay_question', None)
            return validate(*args, **kwargs)
        with patch.object(workflow, '_validate_article', side_effect=legacy):
            with self.assertRaises(workflow.WorkflowError):
                self.prepare(steps=['chatgpt'])
        failed = self.latest_manifest()
        checkpoint = Path(failed['run_dir']) / 'stage-1-chatgpt.checkpoint.json'
        before = checkpoint.read_bytes()
        original_calls = len([call for call in self.bridge.calls if not call['images']])
        self.bridge.missing_image_index = None
        result = self.workflow.resume(failed['run_dir'])
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(result['cover_headline'], '쉬는날판도')
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual(len([call for call in self.bridge.calls if not call['images']]), original_calls)
        image_prompts = [call['prompt'] for call in self.bridge.generations]
        self.assertFalse(any('쉬는날판도' in prompt for prompt in image_prompts))

    def test_app_validated_question_does_not_wait_for_cli_image_review(self):
        def reject(review, index):
            if index == 0:
                review.update(approved=False, issues=['문구의 한국어 문법이 자연스럽지 않습니다.'])
        self.bridge.image_callback = reject
        result = self.prepare(steps=['chatgpt'])
        self.assertTrue(result['images'][0]['local_file_validated'])
        self.assertEqual([call for call in self.bridge.calls if call['images']], [])


if __name__ == '__main__':
    unittest.main()
