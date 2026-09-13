import copy
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from blog_accounts_runtime import (AccountConfigurationError, AccountsRuntime, AccountStop, AccountWorker, SharedRealtime)
from blog_controls import BlogWorkflowControls
from blog_runtime import CliAccessRequired
from blog_preferences import atomic_json_write
from blog_topic_history import TopicHistory


ACCOUNTS = [{'id': 'primary', 'browser': 'whale', 'blog_id': 'primary_blog', 'enabled': True},
            {'id': 'secondary', 'browser': 'edge', 'blog_id': 'secondary_blog', 'enabled': True}]
CONFIG = {'steps': ['chatgpt'], 'models': {}, 'review_mode': '단계별 교차 검수',
          'base_prompt': '사용자 지침', 'blog_id': 'primary_blog', 'publish': True,
          'save_draft': False, 'completion_label': '자동 발행', 'include_google': False}


class NoTk:
    def get(self):
        raise AssertionError('A worker read a Tk variable')


class Bot:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir) if data_dir else None
        self.stop_event = threading.Event()
        self.closed = False
        self.receipt = None
    def stop(self):
        self.stop_event.set()
    def reset_stop(self):
        self.stop_event.clear()
    def close(self):
        self.closed = True
    def publication_receipt_for(self, *_args):
        return self.receipt


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = SimpleNamespace(cli_app_dir=self.root, events=queue.Queue(), full_auto_stop=threading.Event(),
            settings={'writer_accounts': copy.deepcopy(ACCOUNTS)}, naver_bot=Bot(self.root),
            full_auto_active=True, naver_task_active=True, blog_id=NoTk(), status=NoTk(), auto_interval_hours=NoTk())

    def runtime(self, **options):
        browser_factory = options.pop('browser_factory', lambda path, *_args, **_kw: Bot(path))
        return AccountsRuntime(self.app, CONFIG, browser_factory=browser_factory,
                               bridge_factory=lambda *_args: SimpleNamespace(), fetch_groups=lambda: {'공통': ['검색어']}, **options)

    def run_bounded(self, runtime, seconds=4):
        worker = threading.Thread(target=runtime.run)
        worker.start()
        worker.join(seconds)
        if worker.is_alive():
            self.app.full_auto_stop.set()
            worker.join(seconds)
            self.fail('Runtime did not join all workers')

    def events(self):
        result = []
        while not self.app.events.empty():
            result.append(self.app.events.get_nowait())
        return result

    def test_account_stop_is_local_and_global_stop_reaches_both(self):
        first, second = AccountStop(self.app.full_auto_stop), AccountStop(self.app.full_auto_stop)
        first.set()
        self.assertTrue(first.is_set())
        self.assertFalse(second.is_set())
        self.assertFalse(self.app.full_auto_stop.is_set())
        self.app.full_auto_stop.set()
        self.assertTrue(second.wait(.01))

    def test_real_headless_workers_use_separate_paths_and_one_shared_database(self):
        runtime = self.runtime()
        first, second = [AccountWorker(runtime, value) for value in ACCOUNTS]
        self.assertEqual(first.cli_app_dir, self.root)
        self.assertEqual(second.cli_app_dir, self.root / 'writer-accounts' / 'secondary' / 'edge')
        self.assertIsNot(first.naver_bot, self.app.naver_bot)
        self.assertTrue(first.owns_browser)
        self.assertIsNot(first.cli_bridge, second.cli_bridge)
        self.assertEqual(first.keyword_database_path, second.keyword_database_path)
        self.assertEqual(first.topic_history.path, second.topic_history.path)
        self.assertNotEqual(first.topic_history.owner, second.topic_history.owner)
        self.assertEqual(second._next_config()['blog_id'], 'secondary_blog')
        self.assertEqual(first._next_config()['interval_seconds'], 3600)
        self.assertFalse((self.root / 'settings.json').exists())

    def test_primary_other_browser_uses_ui_path_and_cached_login_driver(self):
        self.app.settings['writer_accounts'][0]['browser'] = 'chrome'
        cached = Bot()
        self.app._account_login_bots = {('primary', 'chrome', 'primary_blog'): cached}
        runtime = self.runtime()
        first = AccountWorker(runtime, runtime.accounts[0])
        self.assertIs(first.naver_bot, cached)
        self.assertEqual(first.cli_app_dir, self.root / 'writer-accounts' / 'primary' / 'chrome')

    def test_single_primary_whale_preserves_original_logged_in_browser(self):
        self.app.settings['writer_accounts'][1]['enabled'] = False
        worker = AccountWorker(self.runtime(), ACCOUNTS[0])
        self.assertIs(worker.naver_bot, self.app.naver_bot)
        self.assertFalse(worker.owns_browser)

    def test_dual_primary_whale_must_verify_bound_account_before_writing(self):
        created, written = [], []
        def browser(path, logger, name, *, blog_id):
            bot = Bot(path)
            created.append((name, blog_id, path))
            def verify(target):
                self.assertEqual(target, blog_id)
                if name == 'whale':
                    raise RuntimeError('현재 로그인 블로그 ID 불일치')
            bot.verify_account = verify
            return bot
        class Worker(AccountWorker):
            def _cli_automation_cycle(self, config, budget=None):
                written.append(self.account_id)
                self.full_auto_stop.set()
        self.run_bounded(self.runtime(browser_factory=browser, worker_factory=Worker))
        self.assertEqual(written, ['secondary'])
        self.assertIn(('whale', 'primary_blog', self.root), created)
        self.assertFalse(self.app.naver_bot.closed)

    def test_accounts_prepare_concurrently_and_only_manager_emits_global_idle(self):
        entered = threading.Barrier(2)
        seen = []
        class Worker(AccountWorker):
            def _cli_automation_cycle(self, config, budget=None):
                seen.append((self.account_id, budget, config['blog_id']))
                entered.wait(2)
                self.full_auto_stop.set()
        runtime = self.runtime(worker_factory=Worker)
        self.run_bounded(runtime)
        self.assertEqual({row[0] for row in seen}, {'primary', 'secondary'})
        self.assertIsNot(seen[0][1], seen[1][1])
        self.assertFalse(self.app.full_auto_stop.is_set())
        self.assertEqual(set(self.app._account_bots), {'primary', 'secondary'})
        events = self.events()
        self.assertEqual(sum(event == ('cli_idle',) for event in events), 1)
        stopped = [i for i, event in enumerate(events) if event[0] == 'account_event' and event[3][0] == 'account_stopped']
        self.assertEqual(len(stopped), 2)
        self.assertGreater(events.index(('cli_idle',)), max(stopped))
        self.assertTrue(all((worker.cli_app_dir / 'cycle-results.jsonl').is_file() for worker in runtime.workers))

    def test_access_failure_stops_only_affected_account(self):
        other_completed = threading.Event()
        class Worker(AccountWorker):
            def _cli_automation_cycle(self, config, budget=None):
                if self.account_id == 'primary':
                    raise CliAccessRequired({'claude': 'login needed'})
                other_completed.set()
                self.full_auto_stop.set()
        runtime = self.runtime(worker_factory=Worker)
        self.run_bounded(runtime)
        self.assertTrue(other_completed.is_set())
        self.assertFalse(self.app.full_auto_stop.is_set())
        primary = json.loads((self.root / 'cycle-results.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(primary['status'], 'access_required')
        self.assertTrue(any(event[0] == 'account_event' and event[1] == 'primary' and event[3][0] == 'cli_access_required'
                            for event in self.events()))

    def test_publication_is_serialized_while_budgets_remain_account_local(self):
        runtime = self.runtime()
        workers = [AccountWorker(runtime, row) for row in ACCOUNTS]
        entered = threading.Barrier(2)
        state = {'active': 0, 'peak': 0}
        lock = threading.Lock()
        def publish(_worker, article, config, budget=None):
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
            time.sleep(.03)
            with lock:
                state['active'] -= 1
            return {'published': True}
        results = []
        def invoke(worker):
            entered.wait(2)
            results.append(worker._publish_cli_worker({}, CONFIG))
        with patch.object(BlogWorkflowControls, '_publish_cli_worker', new=publish):
            threads = [threading.Thread(target=invoke, args=(worker,)) for worker in workers]
            for thread in threads: thread.start()
            for thread in threads: thread.join(2)
        self.assertEqual(len(results), 2)
        self.assertEqual(state['peak'], 1)

    def test_receipt_completion_bypasses_expired_budget_and_held_publish_lock(self):
        runtime = self.runtime()
        worker = AccountWorker(runtime, ACCOUNTS[0])
        worker.full_auto_stop.set()
        runtime.publish_lock.acquire()
        self.addCleanup(runtime.publish_lock.release)
        def forbidden(*_args, **_kwargs):
            raise AssertionError('Receipt bookkeeping must not consume budget or submit')
        worker.naver_bot.publish_naver_article = forbidden
        budget = SimpleNamespace(check=forbidden)
        for receipt in ({'status': 'published', 'published': True, 'url': 'https://blog.naver.com/primary_blog/123'},
                        {'status': 'uncertain', 'published': False}):
            worker.naver_bot.receipt = receipt
            with patch.object(worker, '_record_cli_publication', return_value='local completion') as record:
                self.assertEqual(worker._publish_cli_worker({}, CONFIG, budget), 'local completion')
                self.assertEqual(record.call_args.args[2], {**receipt, 'reused_receipt': True})
        self.assertTrue(runtime.publish_lock.locked())
        self.assertFalse(self.app.full_auto_stop.is_set())

    def test_shared_realtime_collects_once_even_when_primary_only_resumes_pending(self):
        fetched = []
        copied = []
        def fetch():
            fetched.append(threading.current_thread().name)
            return {'공통': ['배터리 관리']}
        class Worker(AccountWorker):
            def _cli_automation_cycle(self, config, budget=None):
                if self.account_id == 'secondary':
                    copied.append(self._cli_realtime_groups())
                    self.runtime.realtime.get(self.full_auto_stop, budget)['공통'].append('private mutation')
                self.full_auto_stop.set()
        runtime = self.runtime(worker_factory=Worker)
        runtime.realtime.fetch = fetch
        self.run_bounded(runtime)
        self.assertEqual(fetched, ['blog-shared-realtime'])
        self.assertEqual(copied, [{'공통': ['배터리 관리']}])
        self.assertEqual(runtime.realtime.groups, {'공통': ['배터리 관리']})

    def pending_worker(self):
        worker = AccountWorker(self.runtime(), ACCOUNTS[0])
        run = worker.cli_app_dir / 'blog-runs' / 'example'
        run.mkdir(parents=True)
        atomic_json_write(run / 'manifest.json', {'image_candidates': [], 'ready_to_publish': False})
        pending = {'phase': 'preparing', 'choice': {'topic': '노트북 배터리 관리', 'keywords': ['배터리 수명']},
                   'config': copy.deepcopy(CONFIG), 'resume_run_dir': str(run)}
        worker._save_pending_topic(pending)
        return worker, pending, run

    def test_two_unchanged_unsubmitted_cycles_archive_pointer_but_preserve_run(self):
        worker, pending, run = self.pending_worker()
        self.assertFalse(worker._observe_progress(pending, copy.deepcopy(pending), CONFIG))
        self.assertTrue(worker._observe_progress(pending, copy.deepcopy(pending), CONFIG))
        self.assertFalse((worker.cli_app_dir / 'pending-blog-topic.json').exists())
        self.assertTrue((run / 'manifest.json').is_file())
        archived = list((worker.cli_app_dir / 'abandoned-pending').glob('*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text(encoding='utf-8'))['phase'], 'abandoned')
        self.assertFalse(worker.topic_history._reservations())

    def test_new_checkpoint_counts_as_progress_using_captured_before_state(self):
        worker, pending, run = self.pending_worker()
        before = worker._progress(pending)
        atomic_json_write(run / 'stage-1-chatgpt.checkpoint.json', {'article_sha256': 'new-approved-copy'})
        self.assertFalse(worker._observe_progress(pending, copy.deepcopy(pending), CONFIG, before_progress=before))
        state = json.loads((worker.cli_app_dir / 'account-progress.json').read_text(encoding='utf-8'))
        self.assertEqual(state['stalled_cycles'], 0)

    def test_uncertain_submission_and_existing_receipt_are_never_abandoned(self):
        worker, pending, _run = self.pending_worker()
        for phase in ('submitted_uncertain', 'publishing', 'completed'):
            with self.subTest(phase=phase):
                value = {**pending, 'phase': phase}
                for _ in range(3):
                    self.assertFalse(worker._observe_progress(value, copy.deepcopy(value), CONFIG))
        self.assertTrue((worker.cli_app_dir / 'pending-blog-topic.json').exists())
        pending['phase'] = 'prepared'
        pending['prepared_article'] = {'title': 'prepared'}
        worker.naver_bot.receipt = {'status': 'uncertain', 'published': False}
        with patch.object(worker, '_publication_payload', return_value={}):
            self.assertFalse(worker._safe_to_abandon(pending, CONFIG))
        worker.naver_bot.receipt = None
        with patch.object(worker, '_publication_payload', return_value={}):
            self.assertTrue(worker._safe_to_abandon(pending, CONFIG))

    def test_existing_pending_reserves_before_any_new_selection_and_account_changes_stop(self):
        worker, pending, _run = self.pending_worker()
        worker.topic_history.release()
        self.assertFalse(worker.topic_history._reservations())
        worker._next_config()
        self.assertIn('primary', worker.topic_history._reservations())
        self.app.settings['writer_accounts'][0]['blog_id'] = 'other_blog'
        with self.assertRaises(AccountConfigurationError):
            worker._next_config()
        self.assertTrue((worker.cli_app_dir / 'pending-blog-topic.json').exists())

    def test_uncertain_pending_does_not_gain_new_reservation_on_start(self):
        worker, pending, _run = self.pending_worker()
        worker.topic_history.release()
        pending['phase'] = 'submitted_uncertain'
        atomic_json_write(worker.cli_app_dir / 'pending-blog-topic.json', pending)
        worker._next_config()
        self.assertFalse(worker.topic_history._reservations())

    def test_prepared_legacy_reservation_restored_before_secondary_selection_or_collection(self):
        # Reversed configuration order must not let secondary beat legacy primary.
        self.app.settings['writer_accounts'].reverse()
        pending = {'phase': 'prepared', 'choice': {'topic': '배터리 수명 관리', 'keywords': ['배터리 오래 쓰기']},
                   'config': copy.deepcopy(CONFIG), 'prepared_article': {'title': '배터리 수명 관리'}}
        atomic_json_write(self.root / 'pending-blog-topic.json', pending)
        original = (self.root / 'pending-blog-topic.json').read_bytes()
        reservations_seen = []
        class Worker(AccountWorker):
            def _cli_automation_cycle(self, config, budget=None):
                reservations_seen.append((self.account_id, copy.deepcopy(self.topic_history._reservations())))
                if self.account_id == 'secondary':
                    self_test.assertTrue(self.topic_history.is_duplicate('배터리 수명 관리'))
                self.full_auto_stop.set()
        self_test = self
        runtime = self.runtime(worker_factory=Worker)
        def fetch():
            self.assertIn('primary', runtime.workers[0].topic_history._reservations())
            return {'공통': ['다른 키워드']}
        runtime.realtime.fetch = fetch
        self.run_bounded(runtime)
        self.assertEqual({row[0] for row in reservations_seen}, {'primary', 'secondary'})
        self.assertTrue(all('primary' in row[1] for row in reservations_seen))
        self.assertEqual((self.root / 'pending-blog-topic.json').read_bytes(), original)

    def test_prepared_receipt_or_receipt_failure_never_creates_new_reservation(self):
        worker = AccountWorker(self.runtime(), ACCOUNTS[0])
        pending = {'phase': 'prepared', 'choice': {'topic': '배터리 관리'}, 'prepared_article': {'title': '배터리 관리'}}
        for receipt in ({'status': 'uncertain'}, {'status': 'published', 'published': True}):
            worker.naver_bot.receipt = receipt
            worker._restore_pending_reservation(pending, CONFIG)
            self.assertFalse(worker.topic_history._reservations())
        def unreadable(*_args):
            raise OSError('영수증 파일 읽기 실패')
        worker.naver_bot.publication_receipt_for = unreadable
        with self.assertRaises(AccountConfigurationError):
            worker._restore_pending_reservation(pending, CONFIG)
        self.assertFalse(worker.topic_history._reservations())

    def test_completed_local_bookkeeping_does_not_require_browser_login(self):
        self.app.settings['writer_accounts'][1]['enabled'] = False
        atomic_json_write(self.root / 'pending-blog-topic.json',
                          {'phase': 'completed', 'completion_result': {'saved': True}, 'config': copy.deepcopy(CONFIG)})
        def forbidden(_blog_id):
            raise AssertionError('Completed bookkeeping should not reopen the login')
        self.app.naver_bot.verify_account = forbidden
        calls = []
        class Worker(AccountWorker):
            def _cli_automation_cycle(self, config, budget=None):
                calls.append(self.account_id)
                self.full_auto_stop.set()
        self.run_bounded(self.runtime(worker_factory=Worker))
        self.assertEqual(calls, ['primary'])

    def test_interval_setting_changes_the_pending_schedule_without_tk_reads(self):
        now = [10.0]
        worker = AccountWorker(self.runtime(clock=lambda: now[0]), ACCOUNTS[0])
        self.app.settings['auto_interval_hours'] = 1
        waits = []
        def advance(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                self.app.settings['auto_interval_hours'] = 2
                now[0] = 7199
            else:
                now[0] += seconds
            return False
        worker.full_auto_stop.wait = advance
        self.assertEqual(worker._wait_for_next_cycle(0, 10, CONFIG), 7200)
        self.assertEqual(waits, [1, 1])
        self.assertEqual(worker._next_config()['interval_hours'], 2)

    def test_daily_metrics_count_published_drafts_and_failures_separately(self):
        worker = AccountWorker(self.runtime(), ACCOUNTS[0])
        from datetime import datetime, timezone
        for status in ('published', 'draft', 'failed'):
            worker._record_cycle({'status': status, 'started_at': datetime.now(timezone.utc).isoformat()})
        logs = [event[-1] for event in self.events() if event[0] == 'account_log']
        self.assertIn('예약 3회', logs[-1])
        self.assertIn('발행 1회', logs[-1])
        self.assertIn('발행률 33%', logs[-1])
        self.assertIn('임시저장 1회', logs[-1])

    def test_actual_completion_receipt_determines_published_status_and_url(self):
        self.app.settings['writer_accounts'][1]['enabled'] = False
        publication = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/primary_blog/123'}
        class Worker(AccountWorker):
            def _cli_automation_cycle(self, config, budget=None):
                self.auto_history.append({'topic': '주제', 'publication': copy.deepcopy(publication)})
                self.full_auto_stop.set()
        runtime = self.runtime(worker_factory=Worker)
        self.run_bounded(runtime)
        record = json.loads((self.root / 'cycle-results.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(record['status'], 'published')
        self.assertEqual(record['url'], publication['url'])

    def test_browser_login_failure_prevents_only_that_accounts_writing(self):
        calls = []
        class Worker(AccountWorker):
            def __init__(self, runtime, account):
                super().__init__(runtime, account)
                if self.account_id == 'secondary':
                    def reject(_blog_id):
                        raise RuntimeError('다른 블로그 계정으로 로그인되어 있습니다.')
                    self.naver_bot.verify_account = reject
            def _cli_automation_cycle(self, config, budget=None):
                calls.append(self.account_id)
                self.full_auto_stop.set()
        self.run_bounded(self.runtime(worker_factory=Worker))
        self.assertEqual(calls, ['primary'])
        self.assertFalse(self.app.full_auto_stop.is_set())


if __name__ == '__main__':
    unittest.main()
