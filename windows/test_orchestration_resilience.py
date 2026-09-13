import json
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_blog_controls as ui_support
import test_process_resume as resume_support
from blog_cli_bridge import BlogCliError
from blog_controls import BlogWorkflowControls
from blog_preferences import atomic_json_write
from blog_runtime import CliAccessRequired
from blog_workflow import WorkflowError


class InterruptedPreparationTests(unittest.TestCase):
    cycle = resume_support.ProcessResumeTests.cycle

    def test_stop_and_auth_failure_keep_run_pointer_before_exit(self):
        for failure in ('stop', 'auth'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
                app, config = self.cycle(folder)
                workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
                run_dir = Path(folder) / 'blog-runs' / 'in-progress'
                run_dir.mkdir(parents=True)
                atomic_json_write(run_dir / 'manifest.json', {'ready_to_publish': False})
                def fail(*_):
                    error = WorkflowError('일시 중단', run_dir)
                    if failure == 'stop':
                        app.full_auto_stop.set()
                    else:
                        error.__cause__ = BlogCliError('authentication_required', '로그인 필요', provider='antigravity')
                    raise error
                app._prepare_cli_worker.side_effect = fail
                with self.assertRaises((WorkflowError, CliAccessRequired)):
                    app._cli_automation_cycle(config)
                pending = json.loads((Path(folder) / 'pending-blog-topic.json').read_text(encoding='utf-8'))
                self.assertEqual(pending['resume_run_dir'], str(run_dir))
                app.full_auto_stop.clear()
                app._prepare_cli_worker.side_effect = None
                app._prepare_cli_worker.return_value = {'topic': 'A', 'run_dir': str(run_dir)}
                app._cli_automation_cycle(config)
                self.assertEqual(app._prepare_cli_worker.call_args.args[2]['resume_run_dir'], str(run_dir))
                workflow.return_value.select_topic.assert_called_once()

    def test_completion_history_write_failure_does_not_repeat_saved_draft(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            config.update(publish=False, save_draft=True)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            app._publish_cli_worker.return_value = {'status': 'draft', 'saved': True, 'published': False}
            def failing_write(path, value):
                if Path(path).name == 'automation-history.json':
                    raise OSError('이력 파일 일시 잠금')
                atomic_json_write(path, value)
            with patch('blog_controls.atomic_json_write', side_effect=failing_write), self.assertRaises(OSError):
                app._cli_automation_cycle(config)
            pending = json.loads((Path(folder) / 'pending-blog-topic.json').read_text(encoding='utf-8'))
            self.assertEqual(pending['phase'], 'completed')
            self.assertTrue(pending['completion_result']['saved'])
            app._cli_automation_cycle(config)
            app._publish_cli_worker.assert_called_once()
            app._prepare_cli_worker.assert_called_once()
            self.assertEqual(len(app.auto_history), 1)
            self.assertFalse((Path(folder) / 'pending-blog-topic.json').exists())

    def test_final_approved_flag_does_not_hide_failed_fact_gate(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            run_dir = Path(folder) / 'blog-runs' / 'facts-rejected'
            atomic_json_write(run_dir / 'manifest.json', {'final_review_attempts': [{'review': {
                'approved': True, 'facts_verified': False, 'sources_verified': True,
                'search_intent_satisfied': True, 'natural_korean': True, 'issues': []}}]})
            app._prepare_cli_worker.side_effect = [WorkflowError('사실 확인 거절', run_dir),
                {'topic': 'A', 'run_dir': str(run_dir)}]
            with self.assertRaises(WorkflowError):
                app._cli_automation_cycle(config)
            app._publish_cli_worker.assert_not_called()
            app._cli_automation_cycle(config)
            self.assertIn('facts_verified', app._prepare_cli_worker.call_args.args[2]['revision_feedback'])
            self.assertEqual(app._prepare_cli_worker.call_count, 2)
            self.assertEqual(len(app.auto_history), 1)
            self.assertFalse((Path(folder) / 'pending-blog-topic.json').exists())

    def test_few_real_related_keywords_are_comparable_candidates(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            config['quality_checks'] = True
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            app._cli_automation_cycle(config)
            self.assertEqual(len(workflow.return_value.select_topic.call_args.args[0]), 4)
            app._publish_cli_worker.assert_called_once()

    def test_failed_final_audit_is_returned_as_bounded_same_topic_revision(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            run_dir = Path(folder) / 'blog-runs' / 'final-rejected'
            atomic_json_write(run_dir / 'manifest.json', {'title': 'A 제목',
                'final_review_attempts': [{'review': {'approved': False, 'facts_verified': False,
                    'sources_verified': False, 'search_intent_satisfied': True, 'natural_korean': True,
                    'issues': ['확인되지 않은 금액 ' * 600]}}]})
            app._prepare_cli_worker.side_effect = [WorkflowError('최종 사실 거절', run_dir),
                {'topic': 'A', 'run_dir': str(run_dir)}]
            with self.assertRaises(WorkflowError):
                app._cli_automation_cycle(config)
            app._publish_cli_worker.assert_not_called()
            app._cli_automation_cycle(config)
            second = app._prepare_cli_worker.call_args.args[2]
            self.assertEqual(second['resume_run_dir'], str(run_dir))
            self.assertIn('확인되지 않은 금액', second['revision_feedback'])
            self.assertLessEqual(len(second['revision_feedback']), 4000)
            self.assertEqual([call.args[0] for call in app._prepare_cli_worker.call_args_list], ['A', 'A'])
            app._publish_cli_worker.assert_called_once()

    def test_preserved_editorial_findings_do_not_replace_legacy_writing_identity(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            config['revision_feedback'] = '이전에 저장된 작성 지침'
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            run_dir = Path(folder) / 'blog-runs' / 'editorial-rejected'
            atomic_json_write(run_dir / 'manifest.json', {'final_review_attempts': [
                {'review': {'approved': False, 'facts_verified': False, 'sources_verified': False,
                           'search_intent_satisfied': True, 'natural_korean': True, 'issues': ['최신 적용 대상 확인']}}]})
            atomic_json_write(run_dir / 'editorial.pending.json', {
                'context': {'sequence': 'editorial'}, 'status': 'rejected'})
            app._prepare_cli_worker.side_effect = [WorkflowError('최종 사실 거절', run_dir),
                {'topic': 'A', 'run_dir': str(run_dir)}]
            with self.assertRaises(WorkflowError):
                app._cli_automation_cycle(config)
            app._publish_cli_worker.assert_not_called()
            app._cli_automation_cycle(config)
            second = app._prepare_cli_worker.call_args.args[2]
            self.assertEqual(second['revision_feedback'], '이전에 저장된 작성 지침')
            self.assertIn('최신 적용 대상 확인', second['final_review_feedback'])
            app._publish_cli_worker.assert_called_once()

    def test_malformed_review_does_not_break_error_handler_or_add_fact_feedback(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.cycle(folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            run_dir = Path(folder) / 'blog-runs' / 'malformed-audit'
            atomic_json_write(run_dir / 'manifest.json', {'final_review_attempts': [
                {'review': {'approved': False, 'facts_verified': False, 'sources_verified': False,
                           'search_intent_satisfied': True, 'natural_korean': True, 'issues': None}}]})
            atomic_json_write(run_dir / 'editorial.pending.json', {
                'context': {'sequence': 'editorial'}, 'status': 'awaiting_audit'})
            app._prepare_cli_worker.side_effect = [WorkflowError('검수 JSON 형식 오류', run_dir),
                {'topic': 'A', 'run_dir': str(run_dir)}]
            with self.assertRaises(WorkflowError):
                app._cli_automation_cycle(config)
            app._publish_cli_worker.assert_not_called()
            app._cli_automation_cycle(config)
            second = app._prepare_cli_worker.call_args.args[2]
            self.assertEqual(second['resume_run_dir'], str(run_dir))
            self.assertNotIn('final_review_feedback', second)
            self.assertNotIn('revision_feedback', second)
            app._publish_cli_worker.assert_called_once()


class GoogleCandidateReuseTests(unittest.TestCase):
    def setUp(self):
        precheck = patch("blog_controls._GoogleSearchJob._precheck", side_effect=lambda planner, items, number: items)
        precheck.start()
        self.addCleanup(precheck.stop)

    def google_app(self, folder, workflow, count=3):
        app = ui_support.BlogUiTests.make_app(self, folder, {})
        app._preflight_cli_accounts = Mock()
        app.naver_bot = Mock()
        workflow.return_value.prepare.return_value = {'topic': '전기요금', 'run_dir': str(Path(folder) / 'blog-runs' / 'new')}
        workflow.return_value.plan_google_image_search.return_value = {
            'query': 'home electricity meter',
            'queries': ['home electricity meter', 'household power socket', 'domestic solar panels', 'ignored fourth query']}
        config = {'steps': ['chatgpt'], 'models': {}, 'include_google': True, 'base_prompt': '지침',
                  'review_mode': '단계별 교차 검수', 'blog_id': 'owner', 'google_reference_count': count}
        def prepare(topic, keywords, *args, **kwargs):
            run = Path(kwargs.get('resume_run_dir', Path(folder) / 'blog-runs' / 'new'))
            atomic_json_write(run / 'request.json', {'google_candidates': kwargs['google_candidates']})
            atomic_json_write(run / 'manifest.json', {'status': 'preparing'})
            kwargs['on_run_created'](str(run))
            app.resolved_google = kwargs['resolve_google_candidates']()
            return {'topic': topic, 'run_dir': str(run)}
        workflow.return_value.prepare.side_effect = prepare
        return app, config

    def test_one_search_keeps_available_candidate_without_chasing_target_count(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.google_app(folder, workflow)
            first = {'path': str(Path(folder) / 'one.jpg'), 'image_url': 'https://example.org/one.jpg', 'capture_sha256': 'one'}
            second = {'path': str(Path(folder) / 'two.jpg'), 'image_url': 'https://example.org/two.jpg', 'capture_sha256': 'two'}
            third = {'path': str(Path(folder) / 'three.jpg'), 'image_url': 'https://example.org/three.jpg', 'capture_sha256': 'three'}
            app.naver_bot.capture_google_reference_candidates.side_effect = [[first], [dict(first), second, third]]
            app._prepare_cli_worker('전기요금', ['전기요금 절약'], config)
            calls = app.naver_bot.capture_google_reference_candidates.call_args_list
            self.assertEqual(len(calls), 1)
            self.assertEqual([call.kwargs['count'] for call in calls], [2])
            self.assertTrue(all(call.kwargs['reuse_only'] and call.kwargs['english_only'] for call in calls))
            self.assertEqual(app.resolved_google, [first])

    def test_two_original_limit_applies_even_when_saved_target_is_four(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.google_app(folder, workflow, count=4)
            photos = [{'path': str(Path(folder) / f'{index}.jpg'),
                       'image_url': f'https://example.org/{index}.jpg', 'capture_sha256': str(index)}
                      for index in range(5)]
            batches = [photos[:3], [dict(photos[0]), photos[3], photos[4]]]
            def capture(_query, _folder, **kwargs):
                return batches.pop(0)[:kwargs['count']]
            app.naver_bot.capture_google_reference_candidates.side_effect = capture
            app._prepare_cli_worker('전기요금', ['전기요금 절약'], config)
            calls = app.naver_bot.capture_google_reference_candidates.call_args_list
            self.assertEqual([call.kwargs['count'] for call in calls], [2])
            self.assertEqual(app.resolved_google, photos[:2])

    def test_search_failure_does_not_visit_alternate_queries(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.google_app(folder, workflow)
            one = {'path': str(Path(folder) / 'one.jpg')}
            two = {'path': str(Path(folder) / 'two.jpg')}
            app.naver_bot.capture_google_reference_candidates.side_effect = [RuntimeError('preview timeout'), [one, two]]
            app._prepare_cli_worker('전기요금', ['전기요금 절약'], config)
            self.assertEqual(app.naver_bot.capture_google_reference_candidates.call_count, 1)
            self.assertEqual(app.resolved_google, [])

    def test_stop_in_google_capture_never_starts_more_searches_or_writing(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = self.google_app(folder, workflow)
            def cancel(*args, **kwargs):
                app.full_auto_stop.set()
                raise RuntimeError('stopped')
            app.naver_bot.capture_google_reference_candidates.side_effect = cancel
            with self.assertRaisesRegex(WorkflowError, '중지'):
                app._prepare_cli_worker('전기요금', ['전기요금 절약'], config)
            app.naver_bot.capture_google_reference_candidates.assert_called_once()
            workflow.return_value.prepare.assert_called_once()

    @staticmethod
    def google_app_prepare(app, workflow, run_dir):
        def prepare(topic, keywords, *args, **kwargs):
            atomic_json_write(run_dir / 'request.json', {'google_candidates': kwargs['google_candidates']})
            atomic_json_write(run_dir / 'manifest.json', {'status': 'preparing'})
            kwargs['on_run_created'](str(run_dir))
            app.resolved_google = kwargs['resolve_google_candidates']()
            return {'topic': topic, 'run_dir': str(run_dir)}
        workflow.return_value.prepare.side_effect = prepare

    def test_resume_reuses_english_source_candidates_without_new_search(self):
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app = ui_support.BlogUiTests.make_app(self, folder, {})
            app._preflight_cli_accounts = Mock()
            app.naver_bot = Mock()
            run_dir = Path(folder) / 'blog-runs' / 'pending'
            image_dir = Path(folder) / 'google-reference-candidates' / 'kept'
            image_dir.mkdir(parents=True)
            photo = image_dir / 'original.png'
            photo.write_bytes(b'candidate fixture')
            candidate = {'path': str(photo), 'english_source_verified': True, 'source_language': 'en',
                         'capture_sha256': hashlib.sha256(photo.read_bytes()).hexdigest()}
            atomic_json_write(run_dir / 'request.json', {'google_candidates': [candidate]})
            self.google_app_prepare(app, workflow, run_dir)
            config = {'steps': ['chatgpt'], 'models': {}, 'include_google': True, 'base_prompt': '지침',
                      'review_mode': '단계별 교차 검수', 'blog_id': 'owner', 'resume_run_dir': str(run_dir)}
            article = app._prepare_cli_worker('전기요금', ['전기요금 절약'], config)
            app.naver_bot.capture_google_reference_candidates.assert_not_called()
            workflow.return_value.plan_google_image_search.assert_not_called()
            self.assertEqual(workflow.return_value.prepare.call_args.kwargs['google_candidates'], [candidate])
            self.assertIn(str(image_dir), article['auxiliary_dirs'])
            photo.write_bytes(b'replaced content must not inherit reuse evidence')
            workflow.return_value.plan_google_image_search.return_value = {'query': 'Korean home electricity saving'}
            app.naver_bot.capture_google_reference_candidates.return_value = []
            app._prepare_cli_worker('전기요금', ['전기요금 절약'], config)
            app.naver_bot.capture_google_reference_candidates.assert_called_once()

    def test_cleanup_preserves_pending_google_original_and_auxiliary_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            app = object.__new__(BlogWorkflowControls)
            app.cli_app_dir, app._naver_log = Path(folder), Mock()
            run = Path(folder) / 'blog-runs' / 'pending'
            source = Path(folder) / 'google-reference-candidates' / 'needed'
            auxiliary = Path(folder) / 'blog-runs' / 'search-query'
            expired = Path(folder) / 'blog-runs' / 'expired'
            for directory in (run, source, auxiliary, expired):
                directory.mkdir(parents=True)
            atomic_json_write(run / 'request.json', {'google_candidates': [{'path': str(source / 'photo.png')}]})
            atomic_json_write(Path(folder) / 'pending-blog-topic.json', {'resume_run_dir': str(run),
                'prepared_article': {'auxiliary_dirs': [str(auxiliary)]}})
            old = time.time() - 9 * 86400
            for directory in (run, source, auxiliary, expired):
                os.utime(directory, (old, old))
            app._cleanup_stale_artifacts()
            for directory in (run, source, auxiliary):
                self.assertTrue(directory.exists())
            self.assertTrue(expired.exists())  # Age alone does not prove this run was published.


class EarlyResumeReceiptTests(unittest.TestCase):
    def test_pointer_written_before_work_and_preserves_fixed_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            app = object.__new__(BlogWorkflowControls)
            app.cli_app_dir, app._naver_log = Path(folder), Mock()
            run = Path(folder) / 'blog-runs' / 'current'
            atomic_json_write(run / 'request.json', {'topic': '전기요금'})
            atomic_json_write(run / 'manifest.json', {'status': 'preparing'})
            pending_path = Path(folder) / 'pending-blog-topic.json'
            original = {'phase': 'preparing', 'choice': {'topic': '전기요금'}, 'config': {'base_prompt': '지침', 'interval_hours': 1}}
            atomic_json_write(pending_path, original)
            app._remember_preparing_run('전기요금', run)
            saved = json.loads(pending_path.read_text(encoding='utf-8'))
            self.assertEqual(saved['resume_run_dir'], str(run.resolve()))
            self.assertEqual(saved['config'], original['config'])
            before = pending_path.read_bytes()
            with self.assertRaises(WorkflowError):
                app._remember_preparing_run('다른 주제', run)
            self.assertEqual(pending_path.read_bytes(), before)
            with self.assertRaises(WorkflowError):
                app._remember_preparing_run('전기요금', Path(folder))
            self.assertEqual(pending_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
