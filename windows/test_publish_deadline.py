import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from blog_controls import BlogWorkflowControls
from blog_deadline import CycleBudget, CycleDeadlineExceeded
from blog_preferences import atomic_json_write


class PublicationDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.app = object.__new__(BlogWorkflowControls)
        self.app.full_auto_stop = threading.Event()
        self.app.naver_bot = Mock()
        self.original_stop = self.app.naver_bot.stop_event = threading.Event()
        self.app.naver_bot.publication_receipt_for.return_value = None
        self.app._ensure_topic_allowed = Mock()
        self.app.events = Mock()
        self.app._record_cli_publication = Mock(side_effect=lambda article, config, result: result)
        self.now = [0.0]
        self.budget = CycleBudget(3000, clock=lambda: self.now[0])
        self.config = {'blog_id': 'owner', 'publish': True}
        self.article = {'title': '제목', 'topic': '주제'}

    def publish(self):
        return self.app._publish_cli_worker(self.article, self.config, budget=self.budget)

    def test_insufficient_entry_time_preserves_article_without_opening_browser(self):
        self.now[0] = 2881
        with self.assertRaises(CycleDeadlineExceeded):
            self.publish()
        self.app.naver_bot.publish_naver_article.assert_not_called()
        self.assertIs(self.app.naver_bot.stop_event, self.original_stop)
        self.assertNotIn('publication', self.article)

    def test_deadline_cancels_browser_before_final_click_and_restores_events(self):
        final_clicks = []
        def browser(*args, **kwargs):
            self.assertFalse(self.app.naver_bot.stop_event.is_set())
            self.now[0] = 3001
            if self.app.naver_bot.stop_event.is_set():
                raise RuntimeError('before final click stopped')
            final_clicks.append(True)
        self.app.naver_bot.publish_naver_article.side_effect = browser
        with self.assertRaises(CycleDeadlineExceeded):
            self.publish()
        self.assertEqual(final_clicks, [])
        self.assertIs(self.app.naver_bot.stop_event, self.original_stop)
        self.assertFalse(self.original_stop.is_set())
        self.assertFalse(self.app.full_auto_stop.is_set())

    def test_confirmed_result_after_deadline_is_recorded_and_not_turned_into_retry(self):
        result = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/owner/123'}
        def browser(*args, **kwargs):
            self.now[0] = 3001
            return result
        self.app.naver_bot.publish_naver_article.side_effect = browser
        self.assertEqual(self.publish(), result)
        self.app._record_cli_publication.assert_called_once_with(self.article, self.config, result)
        self.assertIs(self.app.naver_bot.stop_event, self.original_stop)

    def test_exception_after_final_click_recovers_success_receipt_before_deadline_check(self):
        receipt = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/owner/123'}
        self.app.naver_bot.publication_receipt_for.side_effect = [None, receipt]
        def browser(*args, **kwargs):
            self.now[0] = 3001
            raise RuntimeError('inspection ended after confirmed click')
        self.app.naver_bot.publish_naver_article.side_effect = browser
        self.assertEqual(self.publish(), receipt)
        self.app.naver_bot.publish_naver_article.assert_called_once()
        self.app._record_cli_publication.assert_called_once()
        self.assertIs(self.app.naver_bot.stop_event, self.original_stop)

    def test_uncertain_receipt_is_recorded_even_when_deadline_and_exception_follow_click(self):
        receipt = {'published': False, 'status': 'uncertain', 'message': '발행 확인 필요'}
        self.app.naver_bot.publication_receipt_for.side_effect = [None, receipt]
        def browser(*args, **kwargs):
            self.now[0] = 3001
            raise RuntimeError('URL response missing after click')
        self.app.naver_bot.publish_naver_article.side_effect = browser
        del self.app._record_cli_publication
        with self.assertRaisesRegex(RuntimeError, '발행 확인 필요'):
            self.publish()
        self.assertEqual(self.article['publication'], receipt)
        self.assertIs(self.app.naver_bot.stop_event, self.original_stop)
        self.app.naver_bot.publish_naver_article.assert_called_once()

    def test_existing_success_receipt_is_recorded_with_expired_budget_and_user_stop(self):
        self.now[0] = 3100
        self.app.full_auto_stop.set()
        receipt = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/owner/123'}
        self.app.naver_bot.publication_receipt_for.return_value = receipt
        result = self.publish()
        self.assertIs(result['published'], True)
        self.app.naver_bot.publish_naver_article.assert_not_called()
        self.assertTrue(self.app.full_auto_stop.is_set())

    def test_top_stop_and_original_browser_stop_are_retained_after_restoration(self):
        for kind in ('top', 'browser'):
            with self.subTest(kind=kind):
                self.app.full_auto_stop.clear()
                self.original_stop.clear()
                def browser(*args, **kwargs):
                    if kind == 'top':
                        self.app.full_auto_stop.set()
                    else:
                        self.app.naver_bot.stop_event.set()
                    self.assertTrue(self.app.naver_bot.stop_event.is_set())
                    raise RuntimeError('user stopped')
                self.app.naver_bot.publish_naver_article.side_effect = browser
                with self.assertRaisesRegex(RuntimeError, 'user stopped'):
                    self.publish()
                self.assertIs(self.app.naver_bot.stop_event, self.original_stop)
                self.assertTrue(self.app.full_auto_stop.is_set() if kind == 'top' else self.original_stop.is_set())


class EarlierStageDeadlineTests(unittest.TestCase):
    def test_collection_and_ranking_expiry_never_start_cli_topic_selection(self):
        from test_process_resume import ProcessResumeTests
        for stage in ('collect', 'rank'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
                app, config = ProcessResumeTests.cycle(self, folder)
                now = [0.0]
                budget = CycleBudget(3000, clock=lambda: now[0])
                def collect():
                    if stage == 'collect':
                        now[0] = 2401
                    return {'source': ['A']}
                def rank(*args, **kwargs):
                    now[0] = 2401
                    return ([{'topic': 'A', 'keywords': ['A 방법']}], {})
                app._cli_realtime_groups.side_effect = collect
                app._rank_longtail_topics.side_effect = rank
                with self.assertRaises(CycleDeadlineExceeded):
                    app._cli_automation_cycle(config, budget=budget)
                workflow.return_value.select_topic.assert_not_called()
                app._prepare_cli_worker.assert_not_called()
                if stage == 'collect':
                    app._rank_longtail_topics.assert_not_called()

    def test_account_check_deadline_is_rechecked_and_bridge_stop_restored(self):
        app = object.__new__(BlogWorkflowControls)
        app.cli_bridge = Mock()
        original = app.cli_bridge.cancel_event = threading.Event()
        app.full_auto_stop = threading.Event()
        app.events, app._naver_log = Mock(), Mock()
        now = [0.0]
        budget = CycleBudget(3000, clock=lambda: now[0])
        def check_accounts():
            self.assertIsNot(app.cli_bridge.cancel_event, original)
            now[0] = 3001
            self.assertTrue(app.cli_bridge.cancel_event.is_set())
            return {}
        app.cli_bridge.check_accounts.side_effect = check_accounts
        with self.assertRaises(CycleDeadlineExceeded):
            app._preflight_cli_accounts({'steps': []}, budget=budget)
        self.assertIs(app.cli_bridge.cancel_event, original)
        self.assertFalse(original.is_set())

    def test_prepared_and_fresh_article_publish_receive_same_budget(self):
        from test_process_resume import ProcessResumeTests
        for prepared in (False, True):
            with self.subTest(prepared=prepared), tempfile.TemporaryDirectory() as folder, patch('blog_controls.BlogWorkflow') as workflow:
                app, config = ProcessResumeTests.cycle(self, folder)
                app._prepare_cli_worker = Mock(return_value={'topic': 'A',
                    'run_dir': str(Path(folder) / 'blog-runs' / 'ready')})
                choice = {'topic': 'A', 'keywords': ['A 방법']}
                workflow.return_value.select_topic.return_value = choice
                if prepared:
                    atomic_json_write(Path(folder) / 'pending-blog-topic.json', {'choice': choice, 'groups': {},
                        'related': {}, 'config': config, 'phase': 'prepared', 'prepared_article':
                        {'topic': 'A', 'run_dir': str(Path(folder) / 'blog-runs' / 'ready')}})
                budget = CycleBudget()
                app._cli_automation_cycle(config, budget=budget)
                self.assertIs(app._publish_cli_worker.call_args.kwargs['budget'], budget)

    def test_expired_budget_still_records_previously_confirmed_cycle(self):
        from test_process_resume import ProcessResumeTests
        with tempfile.TemporaryDirectory() as folder:
            app, config = ProcessResumeTests.cycle(self, folder)
            result = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/owner/123'}
            app._record_cli_publication = Mock(return_value=result)
            atomic_json_write(Path(folder) / 'pending-blog-topic.json', {'choice': {'topic': 'A', 'keywords': ['A 방법']},
                'groups': {}, 'related': {}, 'config': config, 'phase': 'prepared', 'confirmed_receipt': result,
                'prepared_article': {'topic': 'A', 'run_dir': str(Path(folder) / 'blog-runs' / 'ready')}})
            now = [0.0]
            budget = CycleBudget(3000, clock=lambda: now[0])
            now[0] = 3100
            app._cli_automation_cycle(config, budget=budget)
            app._publish_cli_worker.assert_not_called()
            app._record_cli_publication.assert_called_once()
            self.assertFalse((Path(folder) / 'pending-blog-topic.json').exists())


if __name__ == '__main__':
    unittest.main()
