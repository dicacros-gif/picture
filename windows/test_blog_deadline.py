import json
import math
import threading
import time
import unittest

from blog_deadline import BudgetCancelSignal, CycleBudget, CycleDeadlineExceeded


class CycleBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.budget = CycleBudget(clock=lambda: self.now)

    def test_default_budget_reserves_final_review_and_publication(self):
        self.assertEqual(self.budget.remaining(), 3000)
        self.assertEqual(self.budget.remaining(600), 2400)
        self.now += 2400
        with self.assertRaises(CycleDeadlineExceeded):
            self.budget.check(600)
        self.assertEqual(self.budget.timeout(600, 300), 300)
        self.assertEqual(self.budget.remaining(), 600)

    def test_retries_share_elapsed_time_and_do_not_reset_the_budget(self):
        self.assertEqual(self.budget.timeout(600), 600)
        self.now += 2700
        self.assertEqual(self.budget.elapsed, 2700)
        self.assertEqual(self.budget.timeout(600), 300)
        self.now += 300
        for action in (self.budget.check, lambda: self.budget.timeout(600)):
            with self.assertRaises(CycleDeadlineExceeded) as failure:
                action()
            self.assertFalse(failure.exception.retryable)
            self.assertEqual(failure.exception.code, 'cycle_deadline_exceeded')
            self.assertEqual(failure.exception.elapsed, 3000)

    def test_exhaustion_is_clamped_and_subsecond_cli_time_is_not_rounded_up(self):
        self.now += 2999.5
        self.assertEqual(self.budget.remaining(), .5)
        with self.assertRaises(CycleDeadlineExceeded):
            self.budget.timeout(600)
        self.now += 2
        self.assertEqual(self.budget.remaining(), 0)

    def test_invalid_seconds_are_rejected(self):
        for value in (True, -1, math.inf, math.nan, '60', None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    CycleBudget(value)
                with self.assertRaises(ValueError):
                    self.budget.remaining(value)
                with self.assertRaises(ValueError):
                    self.budget.timeout(value)
        with self.assertRaises(ValueError):
            CycleBudget(0)

    def test_runtime_object_cannot_silently_enter_json_requests(self):
        with self.assertRaises(TypeError):
            json.dumps({'cycle_budget': self.budget})

    def test_deadline_signal_does_not_stop_future_user_cycles(self):
        user = threading.Event()
        signal = self.budget.cancel_event(user, 600)
        self.assertIsInstance(signal, BudgetCancelSignal)
        self.assertFalse(signal.is_set())
        self.now += 2400
        self.assertTrue(signal.is_set())
        self.assertTrue(signal.wait(0))
        self.assertFalse(user.is_set())
        self.assertFalse(self.budget.cancel_event(user, 300).is_set())

    def test_local_and_user_cancellation_are_combined_without_mutation(self):
        user = threading.Event()
        first, second = self.budget.cancel_event(user), self.budget.cancel_event(user)
        first.set()
        self.assertTrue(first.is_set())
        self.assertFalse(user.is_set())
        self.assertFalse(second.is_set())
        user.set()
        self.assertTrue(second.wait(0))

    def test_signal_wait_is_bounded_by_deadline_without_timer_threads(self):
        user = threading.Event()
        signal = CycleBudget(.03).cancel_event(user)
        started = time.monotonic()
        self.assertTrue(signal.wait(1))
        self.assertLess(time.monotonic() - started, .5)
        self.assertFalse(user.is_set())

    def test_zero_wait_checks_without_sleeping_or_changing_budget(self):
        self.assertFalse(self.budget.cancel_event().wait(0))
        self.assertEqual(self.budget.elapsed, 0)


if __name__ == '__main__':
    unittest.main()
