"""Offline regressions from the independent account/early-image audit."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from blog_account_history import AccountTopicHistory
from blog_deadline import CycleBudget
from blog_topic_history import TopicHistoryError
import test_blog_workflow as image_fixtures
import test_parallel_blog_workflow as parallel_fixtures


class EarlyImageDistinctCountTests(unittest.TestCase):
    setUp = parallel_fixtures.ParallelWorkflowTests.setUp
    prepare = image_fixtures.BlogWorkflowTests.prepare

    def test_six_unique_approved_files_are_not_reduced_by_counting_duplicate_slots(self):
        generate = self.bridge.generate_image

        def duplicated_pair(provider, prompt, output_dir, **kwargs):
            result = generate(provider, prompt, output_dir, **kwargs)
            index = int(Path(output_dir).name.split('-')[1]) - 1
            if index == 2:
                # The second and third generated slots contain identical pixels.
                # The seventh slot supplies the sixth *distinct* usable photo.
                image_fixtures.make_image(Path(result['path']), 32)
            return result

        self.bridge.generate_image = duplicated_pair
        result = self.prepare(early_image_finish=True, budget=CycleBudget())
        self.assertTrue(result['ready_to_publish'])
        self.assertEqual(len(result['images']), 6)
        self.assertEqual(len({item['pixel_hash'] for item in result['images']}), 6)
        self.assertTrue(any(item['paragraph_index'] >= 6 for item in result['images']))


class AccountHistoryAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'published-topic-history.json'
        self.now = datetime(2026, 9, 13, tzinfo=timezone.utc)
        self.history = AccountTopicHistory(self.path, 'secondary', clock=lambda: self.now)

    def test_expired_publication_is_not_presented_to_cli_as_a_recent_duplicate(self):
        old = {'published': True, 'status': 'published', 'url': 'https://blog.naver.com/example/12345',
               'submitted_at': (self.now - timedelta(days=31)).isoformat()}
        self.history.record_publication('사용 기간', old, keywords=['확인 방법'], title='기간을 어떻게 확인할까요?')
        self.assertFalse(self.history.is_duplicate('사용 기간'))
        self.assertEqual(self.history.recent_publications(), [])

    def test_invalid_reservation_is_not_silently_treated_as_an_available_topic(self):
        self.history.reservation_path.write_text(json.dumps({
            'primary': {'topic': None, 'keywords': [None], 'run_dir': '', 'updated_at': self.now.isoformat()}
        }), encoding='utf-8')
        before = self.history.reservation_path.read_bytes()
        with self.assertRaises(TopicHistoryError):
            self.history.reserve('검토할 주제', ['실제 연관어'])
        self.assertEqual(self.history.reservation_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
