import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from blog_controls import BlogWorkflowControls, _GoogleSearchJob
from blog_deadline import CycleBudget, CycleDeadlineExceeded
from blog_preferences import atomic_json_write
from blog_workflow import WorkflowError


class GoogleParallelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'blog-runs' / 'article'
        self.app = object.__new__(BlogWorkflowControls)
        self.app.cli_app_dir = self.root
        self.app.cli_bridge = Mock()
        self.app.full_auto_stop = threading.Event()
        self.app.events = Mock()
        self.app._naver_log = Mock()
        self.app._preflight_cli_accounts = Mock()
        self.app._ensure_topic_allowed = Mock()
        self.app.naver_bot = Mock()
        self.original_stop = self.app.naver_bot.stop_event = threading.Event()
        self.app.naver_bot.capture_google_reference_candidates.return_value = []
        self.config = {'steps': ['chatgpt'], 'models': {'chatgpt': 'saved'},
            'stage_configs': [{'provider': 'chatgpt', 'role': '작성', 'model': 'stage-model'}],
            'include_google': True, 'base_prompt': '사용자 지침', 'review_mode': 'final', 'blog_id': 'owner'}
        self.writer, self.planner = Mock(), Mock()
        self.planner.plan_google_image_search.return_value = {
            'query': 'Korean train station', 'queries': ['Korean train station', 'railway countryside photograph', 'third search query']}
        self.planner._text_call.side_effect = self.review_images
        self.writer.prepare.side_effect = self.prepare
        factory = patch('blog_controls.BlogWorkflow', side_effect=[self.writer, self.planner])
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    def review_images(self, *args, images, **kwargs):
        flags = ('image_observed', 'text_free', 'logo_free', 'watermark_free', 'photorealistic')
        return {'images': [{'index': index, **{flag: True for flag in flags}} for index in range(len(images))]}

    def prepare(self, topic, keywords, *args, **kwargs):
        atomic_json_write(self.run / 'request.json', {'google_candidates': kwargs['google_candidates']})
        atomic_json_write(self.run / 'manifest.json', {'status': 'preparing'})
        kwargs['on_run_created'](str(self.run))
        self.resolved = kwargs['resolve_google_candidates']()
        atomic_json_write(self.run / 'request.json', {'google_candidates': self.resolved})
        return {'topic': topic, 'run_dir': str(self.run)}

    def run_prepare(self, **kwargs):
        return self.app._prepare_cli_worker('기차 이용', ['기차 예매'], self.config, **kwargs)

    def photo(self, index):
        path = self.root / 'google-reference-candidates' / 'photos' / f'{index}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f'actual test photo {index}'.encode())
        return {'path': str(path), 'capture_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'image_url': f'https://upload.wikimedia.org/{index}.png', 'english_source_verified': True}

    def test_capture_overlaps_writer_after_resume_pointer_is_persisted(self):
        capture_started, writing_started = threading.Event(), threading.Event()
        atomic_json_write(self.root / 'pending-blog-topic.json', {'choice': {'topic': '기차 이용'}, 'phase': 'preparing'})
        def capture(*args, **kwargs):
            pending = json.loads((self.root / 'pending-blog-topic.json').read_text(encoding='utf-8'))
            self.assertEqual(pending['resume_run_dir'], str(self.run))
            capture_started.set()
            self.assertTrue(writing_started.wait(2))
            return []
        def prepare(topic, keywords, *args, **kwargs):
            atomic_json_write(self.run / 'request.json', {'google_candidates': []})
            atomic_json_write(self.run / 'manifest.json', {'status': 'preparing'})
            kwargs['on_run_created'](str(self.run))
            self.assertTrue(capture_started.wait(2))
            writing_started.set()
            kwargs['resolve_google_candidates']()
            return {'topic': topic, 'run_dir': str(self.run)}
        self.app.naver_bot.capture_google_reference_candidates.side_effect = capture
        self.writer.prepare.side_effect = prepare
        self.run_prepare()
        self.assertEqual(self.factory.call_count, 2)
        self.assertIsNot(self.factory.call_args_list[0].args[-1], self.factory.call_args_list[1].args[-1])
        self.assertFalse(self.app._browser_task_busy())
        self.assertIs(self.app.naver_bot.stop_event, self.original_stop)

    def test_early_writer_failure_stops_and_joins_capture_without_stopping_next_run(self):
        capture_started = threading.Event()
        def capture(*args, **kwargs):
            capture_started.set()
            self.assertTrue(self.app.naver_bot.stop_event.wait(2))
            return []
        def fail(*args, **kwargs):
            atomic_json_write(self.run / 'request.json', {'google_candidates': []})
            atomic_json_write(self.run / 'manifest.json', {})
            kwargs['on_run_created'](str(self.run))
            self.assertTrue(capture_started.wait(2))
            raise WorkflowError('writer failed', self.run)
        self.app.naver_bot.capture_google_reference_candidates.side_effect = capture
        self.writer.prepare.side_effect = fail
        with self.assertRaisesRegex(WorkflowError, 'writer failed'):
            self.run_prepare()
        self.assertIsNone(self.app._google_search_job)
        self.assertIs(self.app.naver_bot.stop_event, self.original_stop)
        self.assertFalse(self.original_stop.is_set())
        self.assertFalse(self.app.full_auto_stop.is_set())

    def test_budget_expiration_cancels_only_optional_capture_and_reserves_publication_time(self):
        now = [0.0]
        budget = CycleBudget(3000, clock=lambda: now[0])
        started = threading.Event()
        def capture(*args, **kwargs):
            started.set()
            self.assertTrue(self.app.naver_bot.stop_event.wait(2))
            return []
        def prepare(topic, keywords, *args, **kwargs):
            self.assertIs(kwargs['budget'], budget)
            atomic_json_write(self.run / 'request.json', {'google_candidates': []})
            atomic_json_write(self.run / 'manifest.json', {})
            kwargs['on_run_created'](str(self.run))
            self.assertTrue(started.wait(2))
            now[0] = 2401
            self.assertEqual(kwargs['resolve_google_candidates'](), [])
            return {'topic': topic, 'run_dir': str(self.run)}
        self.app.naver_bot.capture_google_reference_candidates.side_effect = capture
        self.writer.prepare.side_effect = prepare
        self.run_prepare(budget=budget)
        self.assertIs(self.planner.budget, budget)
        self.assertFalse(self.app.full_auto_stop.is_set())
        self.assertFalse(self.original_stop.is_set())
        self.assertNotIn('budget', (self.run / 'request.json').read_text(encoding='utf-8'))
        self.assertNotIn('budget', self.config)

    def test_slow_browser_exit_blocks_publication_and_next_cycle_until_joined(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def capture(*args, **kwargs):
            started.set()
            release.wait(3)
            return []
        def fail(*args, **kwargs):
            atomic_json_write(self.run / 'request.json', {'google_candidates': []})
            atomic_json_write(self.run / 'manifest.json', {})
            kwargs['on_run_created'](str(self.run))
            self.assertTrue(started.wait(2))
            raise WorkflowError('writer failed', self.run)
        self.app.naver_bot.capture_google_reference_candidates.side_effect = capture
        self.writer.prepare.side_effect = fail
        with patch.object(_GoogleSearchJob, 'DRAIN_SECONDS', .01):
            with self.assertRaises(WorkflowError) as caught:
                self.run_prepare()
        self.assertIs(caught.exception.retryable, False)
        job = self.app._google_search_job
        self.assertTrue(job.alive())
        self.assertTrue(self.app._browser_task_busy())
        for call in (lambda: self.app._publish_cli_worker({}, {}), lambda: self.run_prepare(),
                     lambda: self.app._cli_automation_cycle({})):
            with self.assertRaisesRegex(WorkflowError, '이전 Google'):
                call()
        release.set()
        job.thread.join(2)
        self.assertFalse(job.alive())
        self.app._ensure_google_browser_idle()
        self.assertFalse(self.app._browser_task_busy())
        self.assertIs(self.app.naver_bot.stop_event, self.original_stop)

    def test_first_query_pool_keeps_result_order_and_allows_scene_text_and_logos(self):
        photos = [self.photo(index) for index in range(8)]
        self.app.naver_bot.capture_google_reference_candidates.return_value = photos
        calls = [0]
        def review(*args, images, **kwargs):
            result = self.review_images(*args, images=images, **kwargs)
            calls[0] += 1
            if calls[0] == 1:
                for item in result['images']:
                    item['text_free'] = False
                    item['logo_free'] = False
            return result
        self.planner._text_call.side_effect = review
        self.run_prepare()
        self.app.naver_bot.capture_google_reference_candidates.assert_called_once()
        self.assertEqual(self.app.naver_bot.capture_google_reference_candidates.call_args.kwargs['count'], 8)
        self.assertEqual([item['path'] for item in self.resolved], [item['path'] for item in photos[:4]])
        self.assertEqual(self.planner._text_call.call_args_list[0].kwargs['images'], [item['path'] for item in photos[:4]])
        self.assertEqual(self.planner._text_call.call_args.args[4]['chatgpt'], 'stage-model')
        self.assertTrue(all('approved' not in item for item in self.resolved))
        sidecar = json.loads((self.run / 'google-search-checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(len(sidecar['prechecks']), 4)
        self.assertEqual(sidecar['candidate_count'], 4)

    def test_max_twelve_photos_three_prechecks_with_alternative_query(self):
        photos = [self.photo(index) for index in range(12)]
        self.app.naver_bot.capture_google_reference_candidates.side_effect = [photos[:8], photos[8:]]
        def reject(*args, images, **kwargs):
            result = self.review_images(*args, images=images, **kwargs)
            for item in result['images']:
                item['photorealistic'] = False
            return result
        self.planner._text_call.side_effect = reject
        self.run_prepare()
        self.assertEqual(self.resolved, [])
        self.assertEqual(self.planner._text_call.call_count, 3)
        self.assertEqual([call.kwargs['count'] for call in self.app.naver_bot.capture_google_reference_candidates.call_args_list], [8, 4])
        sidecar = json.loads((self.run / 'google-search-checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(sidecar['status'], 'completed')
        self.assertEqual(len(sidecar['prechecks']), 12)

    def test_precheck_requires_exact_indices_observation_and_all_boolean_flags(self):
        photo = self.photo(0)
        job = _GoogleSearchJob(self.app, '주제', ['연관어'], self.config)
        job.run_dir = self.run
        for change in ({'index': True}, {'index': 1}, {'watermark_free': 'true'}, {'image_observed': None}):
            with self.subTest(change=change):
                result = self.review_images(images=[photo['path']])
                result['images'][0].update(change)
                self.planner._text_call.side_effect = None
                self.planner._text_call.return_value = result
                with self.assertRaises(WorkflowError):
                    job._precheck(self.planner, [photo], 1)
        result = self.review_images(images=[photo['path']])
        result['images'][0]['image_observed'] = False
        self.planner._text_call.return_value = result
        self.assertEqual(job._precheck(self.planner, [photo], 1), [])
        Path(photo['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(WorkflowError, '지문'):
            job._precheck(self.planner, [photo], 1)


class CycleBudgetPropagationTests(unittest.TestCase):
    def test_all_three_preparation_attempts_share_one_budget_without_persisting_it(self):
        from test_process_resume import ProcessResumeTests
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = ProcessResumeTests.cycle(self, folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            budget = CycleBudget()
            app._prepare_cli_worker.side_effect = [WorkflowError('one'), WorkflowError('two'),
                {'topic': 'A', 'run_dir': str(Path(folder) / 'blog-runs' / 'ready')}]
            app._cli_automation_cycle(config, budget=budget)
            self.assertEqual(app._prepare_cli_worker.call_count, 3)
            self.assertTrue(all(call.kwargs['budget'] is budget for call in app._prepare_cli_worker.call_args_list))
            self.assertTrue(all('budget' not in call.args[2] for call in app._prepare_cli_worker.call_args_list))
            self.assertNotIn('budget', (Path(folder) / 'automation-history.json').read_text(encoding='utf-8'))

    def test_deadline_exception_never_consumes_three_immediate_retries(self):
        from test_process_resume import ProcessResumeTests
        with tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
            app, config = ProcessResumeTests.cycle(self, folder)
            workflow.return_value.select_topic.return_value = {'topic': 'A', 'keywords': ['A 방법']}
            app._prepare_cli_worker.side_effect = CycleDeadlineExceeded()
            with self.assertRaises(CycleDeadlineExceeded):
                app._cli_automation_cycle(config, budget=CycleBudget())
            app._prepare_cli_worker.assert_called_once()
            app._publish_cli_worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
