import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import blog_google_budget as budgets
from blog_google_budget import GoogleSearchBudget


class GoogleSearchBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.whale = GoogleSearchBudget(self.root, clock=lambda: self.now)
        self.edge = GoogleSearchBudget(self.root, clock=lambda: self.now)

    def restart(self):
        with budgets._registry_lock:
            budgets._registry.pop(str(self.root.resolve()).casefold(), None)
        return GoogleSearchBudget(self.root, clock=lambda: self.now)

    def test_accounts_never_hold_simultaneous_google_searches(self):
        first = self.whale.reserve('whale-article')
        self.assertTrue(first.allowed)
        self.now += 120
        self.assertEqual(self.edge.reserve('edge-article').reason, 'busy')
        self.whale.release(first)
        self.assertEqual(self.edge.reserve('edge-article').reason, 'spacing')
        self.now += 60
        self.assertTrue(self.edge.reserve('edge-article').allowed)

    def test_spacing_starts_when_browser_session_finishes(self):
        first = self.whale.reserve('one')
        self.now += 180
        self.whale.release(first)
        self.now += 20
        skipped = self.restart().reserve('two')
        self.assertEqual(skipped.reason, 'spacing')
        self.assertEqual(skipped.retry_after, 40)

    def test_spacing_skips_without_sleeping_and_persists_restart(self):
        first = self.whale.reserve('one')
        self.whale.release(first)
        self.now += 30
        other = self.restart()
        with patch('time.sleep', side_effect=AssertionError('must not wait')):
            skipped = other.reserve('two')
        self.assertEqual(skipped.reason, 'spacing')
        self.assertEqual(skipped.retry_after, 30)
        self.now += 30
        self.assertTrue(other.reserve('two').allowed)

    def test_article_search_cannot_repeat_after_restart(self):
        first = self.whale.reserve('same-run')
        self.whale.release(first)
        self.now += 3600
        self.assertEqual(self.restart().reserve('same-run').reason, 'article_limit')

    def test_challenge_stops_both_accounts_for_one_hour_after_restart(self):
        first = self.whale.reserve('one')
        self.assertTrue(self.whale.record_challenge())
        self.whale.release(first)
        self.assertEqual(self.edge.reserve('two').reason, 'challenge_cooldown')
        other = self.restart()
        self.now += 3599
        self.assertEqual(other.reserve('three').retry_after, 1)
        self.now += 1
        self.assertTrue(other.reserve('three').allowed)

    def test_concurrent_reservations_have_one_winner(self):
        barrier = threading.Barrier(2)
        values = []
        def reserve(helper, article):
            barrier.wait(timeout=2)
            values.append(helper.reserve(article))
        threads = [threading.Thread(target=reserve, args=(self.whale, 'one')),
                   threading.Thread(target=reserve, args=(self.edge, 'two'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(sum(value.allowed for value in values), 1)

    def test_corrupt_state_skips_optional_work_without_reset(self):
        self.whale.path.write_text('{bad', encoding='utf-8')
        self.assertEqual(self.whale.reserve('one').reason, 'state_unavailable')
        self.assertEqual(self.whale.path.read_text(encoding='utf-8'), '{bad')

    def test_persistence_failure_does_not_issue_search(self):
        with patch('blog_google_budget.atomic_json_write', side_effect=OSError('disk unavailable')):
            self.assertEqual(self.whale.reserve('one').reason, 'state_unavailable')
            self.assertTrue(self.whale.record_challenge() is False)
        self.assertEqual(self.edge.reserve('two').reason, 'challenge_cooldown')

    def test_retains_utf8_json_and_no_plain_article_text(self):
        self.whale.reserve('개인 계정 주제')
        value = json.loads(self.whale.path.read_text(encoding='utf-8'))
        self.assertEqual(value['version'], 1)
        self.assertNotIn('개인 계정 주제', self.whale.path.read_text(encoding='utf-8'))

    def test_releasing_old_token_does_not_release_new_owner(self):
        first = self.whale.reserve('one')
        self.whale.release(first)
        self.now += 60
        self.edge.reserve('two')
        self.whale.release(first)
        self.assertEqual(self.whale.reserve('three').reason, 'busy')


if __name__ == '__main__':
    unittest.main()
