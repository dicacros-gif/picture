import copy
import queue
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from blog_account_history import AccountTopicHistory
from blog_topic_fallback import parse_snapshot, SharedFallbackSnapshot, rank_fallback, SITE_URL
from blog_workflow import rank_topics
from picture_cleaner_pc import PictureCleanerApp, is_ephemeral_keyword


def snapshot():
    return parse_snapshot({'updatedAt': '2026-09-13T06:02:39.935Z', 'portals': [
        {'id': 'daum', 'items': [{'id': 1, 'keyword': '충전기'}, {'id': 2, 'keyword': '고구마'},
                               {'id': 3, 'keyword': '축구'}, {'id': 4, 'keyword': '별세'}]},
        {'id': 'google', 'items': [{'id': 1, 'keyword': '충전기'}]}],
        'related': {'1': {'fullItems': [{'keyword': '충전기 ' + suffix} for suffix in
            ('선택 방법', '고장 원인', '가격 비교', '사용 조건', '보관 방법', '오류 해결')], 'prefixItems': []},
                    '2': {'fullItems': [{'keyword': '고구마 ' + suffix} for suffix in ('보관법', '삶는 방법')], 'prefixItems': []},
                    '3': {'fullItems': [{'keyword': '축구 방법'}]}, '4': {'fullItems': [{'keyword': '별세 뜻'}]}}})


class FallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = datetime(2026, 9, 13, tzinfo=timezone.utc)
        path = Path(temporary.name) / 'history.json'
        self.primary = AccountTopicHistory(path, 'primary', clock=lambda: self.now)
        self.secondary = AccountTopicHistory(path, 'secondary', clock=lambda: self.now)

    def rank(self, history=None):
        return rank_fallback(snapshot(), history or self.secondary, {}, rank_topics, is_ephemeral_keyword)

    def test_static_schema_keeps_source_provenance_and_embedded_related_words(self):
        data = snapshot()
        self.assertEqual(data['groups']['RT · Google'], ['충전기'])
        self.assertEqual(len(data['related']['충전기']['RT 전체 연관어']), 6)
        ranked, related, groups = self.rank()
        self.assertEqual(ranked[0]['topic'], '충전기')
        self.assertEqual(ranked[0]['source_count'], 2)
        self.assertEqual(ranked[0]['fallback_source'], SITE_URL)
        self.assertNotIn('축구', related)
        self.assertNotIn('별세', related)

    def test_two_accounts_share_single_fetch_and_receive_detached_copies(self):
        fetch = Mock(return_value=snapshot())
        shared = SharedFallbackSnapshot(fetch=fetch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: shared.get(), range(2)))
        fetch.assert_called_once()
        first['groups']['RT · 다음'].clear()
        self.assertTrue(second['groups']['RT · 다음'])
        self.assertTrue(shared.get()['groups']['RT · 다음'])

    def test_cache_refresh_is_hourly_and_failures_do_not_hammer_site(self):
        now = [0.0]
        fetch = Mock(return_value=snapshot())
        shared = SharedFallbackSnapshot(fetch=fetch, clock=lambda: now[0])
        shared.get(); now[0] = 3599; shared.get()
        fetch.assert_called_once()
        now[0] = 3600; shared.get()
        self.assertEqual(fetch.call_count, 2)
        fetch.side_effect = RuntimeError('site unavailable')
        now[0] = 7200
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'site unavailable'): shared.get()
        self.assertEqual(fetch.call_count, 3)

    def test_shared_publication_and_active_reservation_exclude_fallback(self):
        self.primary.record_publication('충전기', {'published': True, 'status': 'published',
            'url': 'https://blog.naver.com/account1/12345', 'submitted_at': self.now.isoformat()}, keywords=['충전기 선택 방법'])
        self.primary.reserve('고구마', ['고구마 보관법'])
        self.assertEqual(self.rank()[0], [])
        self.primary.release()
        self.now += timedelta(days=30)
        self.assertIn('충전기', [item['topic'] for item in self.rank()[0]])

    def test_confirmed_draft_is_excluded_across_accounts(self):
        receipt = {'saved': True, 'published': False, 'status': 'draft_saved', 'draft_confirmation_verified': True,
                   'saved_at': self.now.isoformat(), 'article_key': 'a' * 64,
                   'url': 'https://blog.naver.com/account1/postwrite', 'blog_id': 'account1', 'paragraph_count': 8}
        # Match the real receipt-backed consumed draft predicate.
        from blog_topic_history import confirmed_draft
        if not confirmed_draft(receipt):
            self.fail('Fixture must satisfy confirmed_draft rather than forge an unverified history entry')
        self.primary.record_consumed_draft('충전기', receipt, keywords=['충전기 선택 방법'])
        self.assertNotIn('충전기', [item['topic'] for item in self.rank()[0]])

    def app(self):
        return SimpleNamespace(cli_preferences={}, topic_history=self.secondary, keyword_db=[],
            full_auto_stop=threading.Event(), events=queue.Queue(), _naver_log=Mock())

    def test_existing_unused_topics_prevent_site_fetch(self):
        app = self.app()
        with patch('blog_topic_fallback.shared_snapshot') as fallback, patch('picture_cleaner_pc.fetch_autocomplete',
            return_value={'a': ['노트북 충전 방법', '노트북 선택 조건']}):
            ranked, _ = PictureCleanerApp._rank_longtail_topics(app, {'normal': ['노트북']})
        fallback.assert_not_called()
        self.assertEqual(ranked[0]['topic'], '노트북')

    def test_exhausted_normal_queue_uses_static_related_without_autocomplete(self):
        app = self.app()
        self.primary.reserve('노트북')
        with patch('blog_topic_fallback.shared_snapshot', return_value=snapshot()) as fallback, \
             patch('picture_cleaner_pc.fetch_autocomplete') as autocomplete:
            ranked, _ = PictureCleanerApp._rank_longtail_topics(app, {'normal': ['노트북']})
        fallback.assert_called_once()
        autocomplete.assert_not_called()
        self.assertEqual(ranked[0]['topic'], '충전기')

    def test_durable_diverse_search_intents_outrank_one_off_news(self):
        groups = {'a': ['신제품 속보', '충전기']}
        related = {'신제품 속보': ['신제품 속보 ' + suffix for suffix in ('사진', '영상', '발표', '현장', '내용', '소식')],
                   '충전기': snapshot()['related']['충전기']}
        ranked = rank_topics(groups, related)
        self.assertEqual(ranked[0]['topic'], '충전기')
        self.assertGreater(ranked[0]['durability_bonus'], 0)
        self.assertGreater(len(ranked[0]['intent_families']), 2)
        self.assertEqual(ranked[1]['one_off_penalty'], 22)


if __name__ == '__main__':
    unittest.main()
