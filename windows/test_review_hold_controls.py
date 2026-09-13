import copy
import json
import queue
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import picture_cleaner_pc as app_module
import test_process_resume as resume_support
from blog_controls import _review_hold_signature
from blog_preferences import atomic_json_write
from blog_workflow import WorkflowError, WorkflowReviewRequired


class ReviewHoldControlsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app, self.config = resume_support.ProcessResumeTests().cycle(temporary.name)
        self.run = self.root / 'blog-runs' / 'existing-run'
        self.path = self.root / 'pending-blog-topic.json'
        atomic_json_write(self.run / 'manifest.json', {'ready_to_publish': False})
        atomic_json_write(self.run / 'request.json', {'topic': 'A', 'base_prompt': '기존 지침'})
        atomic_json_write(self.run / 'editorial.pending.json', {
            'status': 'rejected', 'repair_attempts': [{'number': 1}, {'number': 2}],
            'last_audit': {'review': {'approved': False, 'issues': ['원고의 수치 설명 충돌']}}})
        self.pending = {'choice': {'topic': 'A', 'keywords': ['A 방법']}, 'groups': {'a': ['A']},
                        'related': {'A': {}}, 'config': copy.deepcopy(self.config),
                        'phase': 'preparing', 'resume_run_dir': str(self.run)}
        atomic_json_write(self.path, self.pending)

    def error(self, deadline=None):
        error = WorkflowReviewRequired('같은 원고의 수치 설명 보완이 필요합니다.', self.run)
        error.retry_after = (deadline or datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        return error

    def create_hold(self):
        self.app._prepare_cli_worker.side_effect = self.error()
        with self.assertRaises(WorkflowReviewRequired):
            self.app._cli_automation_cycle(self.config)
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_review_required_ends_immediate_retry_after_one_attempt_and_preserves_evidence(self):
        before = {p.name: p.read_bytes() for p in self.run.iterdir()}
        pending = self.create_hold()
        self.app._prepare_cli_worker.assert_called_once()
        self.app._publish_cli_worker.assert_not_called()
        self.assertEqual(pending['phase'], 'review_required')
        self.assertIsNotNone(pending['review_hold']['signature'])
        self.assertIsNotNone(pending['review_hold']['retry_after'])
        self.assertEqual(pending['config'], self.config)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.run.iterdir()})
        attempts = json.loads((self.root / 'last-cycle-attempts.json').read_text(encoding='utf-8'))['attempts']
        self.assertEqual([item['stage'] for item in attempts], ['review_required'])

    def test_unchanged_hold_skips_accounts_and_workers_and_does_not_write_again(self):
        pending = self.create_hold()
        snapshots = {p: p.read_bytes() for p in self.root.rglob('*.json')}
        self.app._preflight_cli_accounts.reset_mock()
        self.app._prepare_cli_worker.reset_mock()
        for _ in range(2):
            with self.assertRaises(WorkflowReviewRequired) as caught:
                self.app._cli_automation_cycle(self.config)
            self.assertEqual(caught.exception.retry_after, pending['review_hold']['retry_after'])
        self.app._preflight_cli_accounts.assert_not_called()
        self.app._prepare_cli_worker.assert_not_called()
        self.app._rank_longtail_topics.assert_not_called()
        self.assertEqual(snapshots, {p: p.read_bytes() for p in self.root.rglob('*.json')})

    def test_live_settings_changes_do_not_replace_frozen_run_configuration(self):
        self.create_hold()
        self.app._prepare_cli_worker.reset_mock()
        next_article_config = {**self.config, 'base_prompt': '다음 글 지침', 'models': {'chatgpt': 'next-model'}}
        with self.assertRaises(WorkflowReviewRequired):
            self.app._cli_automation_cycle(next_article_config)
        self.app._prepare_cli_worker.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['config'], self.config)

    def test_expired_hold_resumes_same_topic_through_normal_worker(self):
        pending = self.create_hold()
        pending['review_hold']['retry_after'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        atomic_json_write(self.path, pending)
        self.app._prepare_cli_worker.reset_mock()
        self.app._prepare_cli_worker.side_effect = None
        self.app._prepare_cli_worker.return_value = {'topic': 'A', 'run_dir': str(self.run)}
        self.app._cli_automation_cycle(self.config)
        self.app._prepare_cli_worker.assert_called_once()
        call = self.app._prepare_cli_worker.call_args
        self.assertEqual(call.args[:2], ('A', ['A 방법']))
        self.assertEqual(call.args[2]['resume_run_dir'], str(self.run))
        self.app._rank_longtail_topics.assert_not_called()
        self.app._publish_cli_worker.assert_called_once()

    def test_changed_evidence_invalidates_hold_but_worker_still_controls_acceptance(self):
        for name in ('request.json', 'editorial.pending.json'):
            with self.subTest(name=name):
                atomic_json_write(self.path, self.pending)
                self.create_hold()
                original = (self.run / name).read_bytes()
                (self.run / name).write_bytes(original + b'\n')
                self.app._prepare_cli_worker.reset_mock()
                self.app._prepare_cli_worker.side_effect = WorkflowError('원래 검증기에서 거절', self.run)
                with self.assertRaises(WorkflowError):
                    self.app._cli_automation_cycle(self.config)
                self.assertEqual(self.app._prepare_cli_worker.call_count, 3)
                self.app._publish_cli_worker.assert_not_called()
                pending = json.loads(self.path.read_text(encoding='utf-8'))
                self.assertNotIn('review_hold', pending)
                self.assertEqual(pending['phase'], 'preparing')

    def test_changed_frozen_configuration_invalidates_hold(self):
        pending = self.create_hold()
        pending['config']['base_prompt'] = '명시적으로 바뀐 회차 지침'
        atomic_json_write(self.path, pending)
        self.app._prepare_cli_worker.reset_mock()
        self.app._prepare_cli_worker.side_effect = self.error()
        with self.assertRaises(WorkflowReviewRequired):
            self.app._cli_automation_cycle(self.config)
        self.app._prepare_cli_worker.assert_called_once()
        self.assertEqual(self.app._prepare_cli_worker.call_args.args[2]['base_prompt'], '명시적으로 바뀐 회차 지침')

    def test_unknown_or_naive_retry_deadline_does_not_create_permanent_hold(self):
        for deadline in (None, 'not-a-date', '2026-09-13T10:00:00'):
            with self.subTest(deadline=deadline):
                atomic_json_write(self.path, self.pending)
                pending = self.create_hold()
                pending['review_hold']['retry_after'] = deadline
                atomic_json_write(self.path, pending)
                self.app._prepare_cli_worker.reset_mock()
                with self.assertRaises(WorkflowReviewRequired):
                    self.app._cli_automation_cycle(self.config)
                self.app._prepare_cli_worker.assert_called_once()

    def test_prepared_article_takes_priority_over_stale_review_hold(self):
        pending = self.create_hold()
        pending['prepared_article'] = {'topic': 'A', 'run_dir': str(self.run)}
        atomic_json_write(self.path, pending)
        self.app._prepare_cli_worker.reset_mock()
        self.app._cli_automation_cycle(self.config)
        self.app._prepare_cli_worker.assert_not_called()
        self.app._publish_cli_worker.assert_called_once()

    def test_uncertain_submission_takes_priority_and_never_republishes(self):
        pending = self.create_hold()
        pending.update(publication_started=True, prepared_article={'topic': 'A', 'run_dir': str(self.run)})
        atomic_json_write(self.path, pending)
        self.app.naver_bot.publication_receipt_for.return_value = {'published': False, 'status': 'uncertain'}
        self.app._prepare_cli_worker.reset_mock()
        with self.assertRaisesRegex(WorkflowError, '이전 발행 결과'):
            self.app._cli_automation_cycle(self.config)
        self.app._prepare_cli_worker.assert_not_called()
        self.app._publish_cli_worker.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['phase'], 'submitted_uncertain')

    def test_confirmed_submission_finishes_bookkeeping_before_stale_hold(self):
        pending = self.create_hold()
        pending.update(publication_started=True, prepared_article={'topic': 'A', 'run_dir': str(self.run)})
        atomic_json_write(self.path, pending)
        receipt = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/testblog/100'}
        self.app.naver_bot.publication_receipt_for.return_value = receipt
        self.app._record_cli_publication = Mock(return_value=receipt)
        self.app._prepare_cli_worker.reset_mock()
        self.app._cli_automation_cycle(self.config)
        self.app._record_cli_publication.assert_called_once()
        self.app._prepare_cli_worker.assert_not_called()
        self.app._publish_cli_worker.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_signature_never_reads_a_run_outside_owned_root(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected file read')):
            result = _review_hold_signature(self.root, {**self.pending, 'resume_run_dir': str(self.root.parent)}, self.config)
        self.assertIsNone(result)


class ReviewHoldScheduleTests(unittest.TestCase):
    def test_repeated_hold_is_quiet_and_retains_hourly_repair_schedule(self):
        app = object.__new__(app_module.PictureCleanerApp)
        app.settings, app.events, app.naver_bot, app._naver_log = {}, queue.Queue(), Mock(), Mock()
        app.full_auto_active = app.naver_task_active = True
        clock, cycles = [0.0], []
        deadline = datetime.now(timezone.utc) + timedelta(hours=1, seconds=2)

        class Stop:
            stopped = False
            def is_set(self): return self.stopped
            def wait(self, duration):
                self.stopped = len(cycles) >= 2
                clock[0] = 7200
                return self.stopped

        app.full_auto_stop = Stop()
        def cycle(config, budget=None):
            cycles.append(config)
            self.assertTrue(app.full_auto_active)
            error = WorkflowReviewRequired('같은 원고 보완 대기', Path('held-run'))
            error.retry_after = deadline.isoformat()
            raise error
        app._run_full_automation_cycle = cycle
        config = {'interval_hours': 1, 'interval_seconds': 3600, 'completion_label': '자동 발행'}
        with patch.object(app_module.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(app_module, 'automation_config_snapshot', side_effect=lambda settings, current: current):
            app._full_automation_loop(config)
        self.assertEqual(len(cycles), 2)
        app._naver_log.assert_called_once()
        events = list(app.events.queue)
        self.assertEqual(len([e for e in events if e[0] == 'auto_error']), 1)
        scheduled = [e[1] for e in events if e[0] == 'status' and '다음' in e[1]]
        self.assertTrue(scheduled)
        self.assertTrue(all('원고 보완 대기 · 다음 보완 예정' in text and '1시간마다' in text for text in scheduled))
        self.assertTrue(all('다음 회차' not in text for text in scheduled))
        self.assertEqual(config['interval_hours'], 1)


if __name__ == '__main__':
    unittest.main()
