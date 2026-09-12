"""Offline Mac workflow orchestration regressions; never calls real CLIs/Naver."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'windows'))
sys.path.insert(0, str(ROOT / 'mac-app/backend'))

from engine import MacRun, WorkflowError, atomic_json_write, read_json, validate_settings


def settings(mode='local', text='사용자 기본 문체를 유지합니다.'):
    return {'blogId': 'exampleblog', 'blog': {
        'stages': [{'provider': 'chatgpt', 'role': '작성', 'model': 'chosen-model'}],
        'prompts': [{'id': 'p', 'text': text}], 'selectedPromptId': 'p',
        'mode': mode, 'includeGoogle': False, 'imageRetryLimit': 2,
    }}


def payload(mode='local'):
    return {'settings': settings(mode), 'keyword': '정기예금',
            'groups': {'트렌드': ['다른 주제']},
            'relatedByTopic': {'정기예금': ['정기예금 금리 비교', '정기예금 가입 방법', '정기예금 만기 조건']}}


def article(run_dir):
    title, paragraphs = '정기예금 금리 비교와 가입 조건', [f'구역 {i}의 내용입니다.' for i in range(8)]
    digest = hashlib.sha256(json.dumps({'title': title, 'paragraphs': paragraphs},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'run_dir': str(run_dir), 'ready_to_publish': True, 'title': title, 'paragraphs': paragraphs,
            'reviewed_content_sha256': digest,
            'images': [{'path': str(run_dir / f'{i}.png'), 'sha256': str(i) * 64, 'paragraph_index': i}
                       for i in range(6)], 'google_images': []}


class FakeWorkflow:
    def __init__(self, root):
        self.root = root
        self._text_call = MagicMock(return_value={'article_topic': '정기예금 금리 비교와 가입 조건',
                                                  'intent': '금리와 가입 조건이 궁금합니다.'})
        self.select_topic = MagicMock()
        self.plan_google_image_search = MagicMock(return_value={'query': 'savings account photograph',
            'queries': ['savings account photograph', 'bank counter photograph']})
        self.prepare = MagicMock(side_effect=self._prepare)
        self.fail_once = False
        self.add_google = False

    def _prepare(self, *_args, **kwargs):
        folder = Path(kwargs.get('resume_run_dir') or self.root / 'run-1')
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json_write(folder / 'request.json', {'request': 'fixture'})
        atomic_json_write(folder / 'manifest.json', {'ready_to_publish': False})
        kwargs['on_run_created'](str(folder))
        if self.fail_once:
            self.fail_once = False
            raise WorkflowError('실제 중단을 흉내 낸 fixture', folder)
        result = article(folder)
        if self.add_google:
            result['google_images'] = [dict(result['images'][0]),
                {'path': str(folder / 'google.jpg'), 'sha256': 'a' * 64, 'paragraph_index': 7, 'provider': 'google'}]
        atomic_json_write(folder / 'manifest.json', result)
        return result


class MacEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()
        self.bridge = MagicMock()
        self.bridge.check_accounts.return_value = {provider: {'installed': True, 'auth_status': 'available'}
            for provider in ['chatgpt', 'claude', 'antigravity']}
        self.bot = MagicMock()
        self.bot.check_login.return_value = {'authenticated': True}
        self.bot.publication_receipt_for.return_value = None
        self.bot.publish_naver_article.return_value = {'status': 'published', 'published': True,
            'url': 'https://blog.naver.com/exampleblog/1234567890'}
        self.workflow = FakeWorkflow(self.root / 'blog-runs')
        self.log = MagicMock()
        self.run = self.make_run()

    def make_run(self):
        return MacRun(self.root, self.log, self.cancel, bridge=self.bridge, bot=self.bot,
                      workflow_type=lambda *_args: self.workflow)

    def pending(self, mode='local', phase='preparing', ready=False):
        data = {'config': validate_settings(settings(mode)), 'requested_keyword': '정기예금', 'phase': phase,
                'choice': {'topic': '정기예금 금리 비교와 가입 조건', 'source_topic': '정기예금',
                           'keywords': ['정기예금 금리 비교', '정기예금 가입 방법'], 'intent': '가입 조건이 궁금합니다.'}}
        if ready:
            folder = self.root / 'blog-runs/run-existing'
            folder.mkdir(parents=True)
            data['run_dir'] = str(folder)
            atomic_json_write(folder / 'manifest.json', article(folder))
        atomic_json_write(self.run.pending_path, data)
        return data

    def test_manual_keyword_asks_only_its_intent_and_local_never_opens_writer(self):
        result = self.run.run(payload())
        self.assertEqual(result['status'], 'local')
        self.assertFalse(result['published'])
        self.bot.check_login.assert_not_called()
        self.bot.publish_naver_article.assert_not_called()
        self.workflow.select_topic.assert_not_called()
        prompt = self.workflow._text_call.call_args.args[3]
        self.assertIn('MANUAL_TOPIC_INTENT', prompt)
        self.assertIn('정기예금 가입 방법', prompt)
        self.assertNotIn('다른 주제', prompt)
        self.assertFalse(self.run.pending_path.exists())

    def test_unrelated_manual_model_answer_uses_actual_related_keyword(self):
        self.workflow._text_call.return_value = {'article_topic': '제주도 호텔 추천', 'intent': '여행을 준비합니다.'}
        self.run.run(payload())
        args = self.workflow.prepare.call_args.args
        self.assertIn('정기예금', args[0])
        self.assertNotIn('제주도', args[0])
        self.assertIn('정기예금 금리 비교', args[0])
        self.assertNotIn('여행을 준비', args[2])

    def test_expired_naver_cookie_result_stops_before_paid_generation(self):
        self.bot.check_login.return_value = {'authenticated': False}
        with self.assertRaisesRegex(WorkflowError, '로그인'):
            self.run.run(payload('publish'))
        self.workflow._text_call.assert_not_called()
        self.workflow.prepare.assert_not_called()

    def test_required_image_cli_is_checked_even_if_not_a_writing_stage(self):
        self.bridge.check_accounts.return_value['antigravity'] = {'installed': False}
        with self.assertRaisesRegex(WorkflowError, 'antigravity'):
            self.run.run(payload())
        self.workflow._text_call.assert_not_called()

    def test_draft_flags_do_not_publish_or_consume_keyword_history(self):
        self.bot.publish_naver_article.return_value = {'status': 'draft_saved', 'saved': True, 'published': False}
        result = self.run.run(payload('draft'))
        self.assertEqual(self.bot.publish_naver_article.call_args.kwargs, {'publish': False, 'save_draft': True})
        self.assertNotIn('consumedKeywords', result)
        self.assertEqual(self.run.history.filter_keywords(['정기예금']), ['정기예금'])

    def test_publication_merges_google_images_and_consumes_topic_and_related(self):
        self.workflow.add_google = True
        result = self.run.run(payload('publish'))
        passed = self.bot.publish_naver_article.call_args.args[1]
        self.assertEqual(len(passed['images']), 7)
        self.assertEqual(self.bot.publish_naver_article.call_args.kwargs, {'publish': True, 'save_draft': False})
        self.assertIn('정기예금', result['consumedKeywords'])
        self.assertEqual(self.run.history.filter_keywords(['정기예금', '정기예금 금리 비교', '새 검색어']), ['새 검색어'])

    def test_stopped_preparing_run_reuses_frozen_prompt_models_and_run_directory(self):
        self.workflow.fail_once = True
        with self.assertRaises(WorkflowError):
            self.run.run(payload())
        saved = read_json(self.run.pending_path)
        first_dir = saved['run_dir']
        self.assertEqual(saved['phase'], 'preparing')
        changed = payload('publish')
        changed['settings'] = {}  # Incomplete edits only apply to the next new article.
        self.make_run().run(changed)
        call = self.workflow.prepare.call_args
        self.assertEqual(call.kwargs['resume_run_dir'], first_dir)
        self.assertEqual(call.kwargs['stage_configs'][0]['model'], 'chosen-model')
        self.assertIn('사용자 기본 문체', call.args[2])
        self.assertEqual(self.workflow._text_call.call_count, 1)
        self.assertEqual(self.bridge.check_accounts.call_count, 2)
        self.bot.publish_naver_article.assert_not_called()

    def test_preparing_resume_without_run_directory_rechecks_accounts(self):
        self.pending()
        self.bridge.check_accounts.return_value['chatgpt']['auth_status'] = 'authentication_required'
        with self.assertRaisesRegex(WorkflowError, '로그인'):
            self.run.run(payload())
        self.workflow.prepare.assert_not_called()

    def test_ready_manifest_is_reused_without_another_cli_or_image_call(self):
        self.pending(ready=True)
        result = self.run.run(payload())
        self.assertEqual(result['status'], 'local')
        self.workflow.prepare.assert_not_called()
        self.workflow._text_call.assert_not_called()
        self.bridge.check_accounts.assert_not_called()

    def test_ready_after_crash_before_pending_phase_update_is_also_reused(self):
        self.pending(phase='preparing', ready=True)
        self.run.run(payload())
        self.workflow.prepare.assert_not_called()

    def test_confirmed_receipt_finishes_bookkeeping_without_login_or_republication(self):
        self.pending(mode='publish', phase='delivery', ready=True)
        self.bot.publication_receipt_for.return_value = self.bot.publish_naver_article.return_value.copy()
        self.bot.check_login.return_value = {'authenticated': False}
        result = self.run.run(payload('publish'))
        self.assertTrue(result['published'])
        self.bot.check_login.assert_not_called()
        self.bot.publish_naver_article.assert_not_called()
        self.workflow.prepare.assert_not_called()
        self.assertFalse(self.run.pending_path.exists())

    def test_uncertain_submission_never_calls_publish_again_on_resume(self):
        uncertain = {'status': 'uncertain', 'published': False, 'article_key': 'a' * 64,
                     'submitted_at': '2026-09-13T12:00:00', 'url': ''}
        self.bot.publish_naver_article.return_value = uncertain
        with self.assertRaisesRegex(WorkflowError, '불확실'):
            self.run.run(payload('publish'))
        self.assertEqual(read_json(self.run.pending_path)['phase'], 'submitted_uncertain')
        self.bot.publication_receipt_for.return_value = uncertain
        with self.assertRaisesRegex(WorkflowError, '다시 발행하지'):
            self.make_run().run(payload('publish'))
        self.assertEqual(self.bot.publish_naver_article.call_count, 1)
        self.assertEqual(self.workflow.prepare.call_count, 1)

    def test_missing_receipt_after_uncertain_submission_stays_blocked(self):
        self.pending(mode='publish', phase='submitted_uncertain', ready=True)
        with self.assertRaisesRegex(WorkflowError, '다시 발행하지'):
            self.run.run(payload('publish'))
        self.bot.publish_naver_article.assert_not_called()
        self.workflow.prepare.assert_not_called()

    def test_delivery_without_final_submission_receipt_reuses_same_article(self):
        pending = self.pending(mode='publish', phase='delivery', ready=True)
        self.run.run(payload('publish'))
        self.workflow.prepare.assert_not_called()
        self.assertEqual(self.bot.publish_naver_article.call_count, 1)
        self.assertEqual(self.bot.publish_naver_article.call_args.args[1]['run_dir'], pending['run_dir'])

    def test_uncertain_draft_is_not_silently_saved_twice(self):
        self.pending(mode='draft', phase='delivery', ready=True)
        with self.assertRaisesRegex(WorkflowError, '중복 저장'):
            self.run.run(payload('draft'))
        self.bot.publish_naver_article.assert_not_called()

    def test_confirmed_draft_result_finishes_without_saving_again(self):
        data = self.pending(mode='draft', phase='delivery', ready=True)
        data['delivery_result'] = {'status': 'draft_saved', 'saved': True, 'published': False}
        atomic_json_write(self.run.pending_path, data)
        self.assertTrue(self.run.run(payload('draft'))['saved'])
        self.bot.publish_naver_article.assert_not_called()

    def test_completed_empty_google_search_is_reused_without_planning_again(self):
        config = validate_settings(settings())
        config['includeGoogle'] = True
        self.assertEqual(self.run.google_candidates({}, config, {'google_search_complete': True, 'google_candidates': []}), [])
        self.workflow.plan_google_image_search.assert_not_called()
        self.bot.capture_google_reference_candidates.assert_not_called()

    def test_google_cache_accepts_unchanged_owned_files_only(self):
        folder = self.root / 'google-reference-candidates/query-1'
        folder.mkdir(parents=True)
        inside, changed, outside = folder / 'good.jpg', folder / 'bad.jpg', self.root / 'outside.jpg'
        for file in [inside, changed, outside]:
            file.write_bytes(b'original')
        sha = hashlib.sha256(b'original').hexdigest()
        entries = [{'path': str(file), 'capture_sha256': sha} for file in [inside, changed, outside]]
        changed.write_bytes(b'changed')
        result = self.run.google_candidates({}, {}, {'google_search_complete': True, 'google_candidates': entries})
        self.assertEqual(result, [entries[0]])

    def test_google_queries_receive_only_remaining_slots_and_preserve_duplicates(self):
        config = validate_settings(settings())
        config.update(includeGoogle=True, googleReferenceCount=3)
        first = {'capture_sha256': 'first', 'path': 'first.jpg'}
        second = {'capture_sha256': 'second', 'path': 'second.jpg'}
        third = {'capture_sha256': 'third', 'path': 'third.jpg'}
        self.bot.capture_google_reference_candidates.side_effect = [[first], [first, second, third]]
        pending = {}
        result = self.run.google_candidates({'topic': '정기예금', 'keywords': ['정기예금 금리']}, config, pending)
        self.assertEqual(result, [first, second, third])
        self.assertEqual([c.args[2] for c in self.bot.capture_google_reference_candidates.call_args_list], [3, 2])
        self.assertTrue(read_json(self.run.pending_path)['google_search_complete'])

    def test_cancelled_before_start_makes_no_cli_or_browser_calls(self):
        self.cancel.set()
        with self.assertRaisesRegex(WorkflowError, '중지'):
            self.run.run(payload())
        self.bridge.check_accounts.assert_not_called()
        self.workflow.prepare.assert_not_called()

    def test_cancelled_after_preparation_preserves_ready_article_without_delivery(self):
        real_prepare = self.workflow._prepare
        def finish_then_cancel(*args, **kwargs):
            result = real_prepare(*args, **kwargs)
            self.cancel.set()
            return result
        self.workflow.prepare.side_effect = finish_then_cancel
        with self.assertRaisesRegex(WorkflowError, '중지'):
            self.run.run(payload('publish'))
        self.assertEqual(read_json(self.run.pending_path)['phase'], 'ready')
        self.bot.publish_naver_article.assert_not_called()

    def test_changed_manual_keyword_cannot_overwrite_pending_article(self):
        original = self.pending()
        incoming = payload()
        incoming['keyword'] = '청약 통장'
        with self.assertRaisesRegex(WorkflowError, '같은 키워드'):
            self.run.run(incoming)
        self.assertEqual(read_json(self.run.pending_path), original)

    def test_modified_ready_article_is_not_republished(self):
        data = self.pending(mode='publish', phase='delivery', ready=True)
        manifest = Path(data['run_dir']) / 'manifest.json'
        altered = read_json(manifest)
        altered['paragraphs'][0] = '다른 원고로 바꿈'
        atomic_json_write(manifest, altered)
        with self.assertRaisesRegex(WorkflowError, '검수 이후 변경'):
            self.run.run(payload('publish'))
        self.bot.publish_naver_article.assert_not_called()

    def test_outside_resume_path_is_rejected_before_reading_it(self):
        data = self.pending()
        data['run_dir'] = str(self.root / 'outside')
        atomic_json_write(self.run.pending_path, data)
        with self.assertRaisesRegex(ValueError, '폴더 밖'):
            self.run.run(payload())
        self.workflow.prepare.assert_not_called()

    def test_empty_blog_id_fails_before_spending_any_cli_requests(self):
        request = payload('publish')
        request['settings']['blogId'] = ''
        with self.assertRaisesRegex(ValueError, '아이디'):
            self.run.run(request)
        self.bridge.check_accounts.assert_not_called()


if __name__ == '__main__':
    unittest.main()
