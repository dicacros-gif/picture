import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blog_account_history import AccountTopicHistory, TopicReservationConflict


class AccountHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'published-topic-history.json'
        self.now = datetime(2026, 9, 13, tzinfo=timezone.utc)
        self.a = AccountTopicHistory(self.path, 'primary', clock=lambda: self.now)
        self.b = AccountTopicHistory(self.path, 'secondary', clock=lambda: self.now)

    def test_atomic_reservation_allows_only_one_writer(self):
        barrier, winners = threading.Barrier(2), []
        def run(history):
            barrier.wait()
            try:
                history.reserve('휴일', ['대체 공휴일'])
                winners.append(history.owner)
            except TopicReservationConflict:
                pass
        threads = [threading.Thread(target=run, args=(h,)) for h in (self.a, self.b)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(winners), 1)

    def test_related_keyword_reserved_across_browsers_but_not_own_resume(self):
        self.a.reserve('휴일', ['대체 공휴일'])
        self.a.reserve('휴일', ['대체 공휴일'], run_dir='saved-run')
        self.assertEqual(self.b.filter_keywords(['대체공휴일', '노트북'], include_pending=True), ['노트북'])
        self.assertTrue(self.b.is_duplicate('다른 제목', ['대체공휴일']))
        self.assertFalse(self.a.is_duplicate('휴일', ['대체 공휴일']))
        self.a.release()
        self.b.reserve('휴일', ['대체 공휴일'])

    def test_confirmed_publication_blocks_both_accounts_for_thirty_days(self):
        self.a.reserve('휴일', ['대체공휴일'])
        receipt = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/account1/12345',
                   'submitted_at': self.now.isoformat()}
        self.a.record_publication('휴일', receipt, keywords=['대체공휴일'], title='언제 쉴까?')
        for history in (self.a, self.b):
            self.assertTrue(history.is_duplicate('대체 공휴일'))
        self.now += timedelta(days=29, hours=23)
        self.assertTrue(self.b.is_duplicate('휴일'))
        self.now += timedelta(hours=1)
        self.assertFalse(self.b.is_duplicate('휴일'))
        self.assertIn('휴일', self.path.read_text(encoding='utf-8'))
        self.assertEqual(json.loads(self.a.reservation_path.read_text(encoding='utf-8')), {})

    def test_new_publication_after_window_archives_old_receipt(self):
        old = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/account1/12345',
               'submitted_at': (self.now - timedelta(days=31)).isoformat()}
        self.a.record_publication('휴일', old)
        self.b.record_publication('휴일', {**old, 'url': 'https://blog.naver.com/account2/98765',
                                         'submitted_at': self.now.isoformat()})
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(data['archive'][0]['url'], old['url'])
        self.assertTrue(self.a.is_duplicate('휴일'))

    def test_draft_does_not_consume_or_release_active_topic(self):
        self.a.reserve('휴일')
        self.assertFalse(self.a.record_publication('휴일', {'saved': True, 'status': 'saved'}))
        with self.assertRaises(TopicReservationConflict): self.b.reserve('휴일')
