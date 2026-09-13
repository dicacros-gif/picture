import queue
import unittest
from unittest.mock import Mock, patch

import picture_cleaner_pc as app_module
from blog_deadline import CycleDeadlineExceeded


class ParallelCycleScheduleTests(unittest.TestCase):
    def run_two_cycles(self, google_alive=False):
        app = object.__new__(app_module.PictureCleanerApp)
        app.settings, app.events, app.naver_bot, app._naver_log = {}, queue.Queue(), Mock(), Mock()
        app.full_auto_active = app.naver_task_active = True
        clock, starts, budgets = [0.0], [], []

        class Stop:
            stopped = False
            def is_set(self): return self.stopped
            def wait(self, duration):
                clock[0] += duration
                return self.stopped

        app.full_auto_stop = Stop()
        if google_alive:
            app._google_search_job = Mock()
            app._google_search_job.alive.return_value = True

        def cycle(config, budget=None):
            starts.append(clock[0])
            budgets.append(budget)
            self.assertEqual(budget.remaining(), 3000)
            if len(starts) == 1:
                clock[0] += 3000
                budget.check()
            app.full_auto_stop.stopped = True

        app._run_full_automation_cycle = cycle
        config = {'interval_hours': 1, 'interval_seconds': 3600, 'completion_label': '자동 발행'}
        with patch.object(app_module.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(app_module, 'automation_config_snapshot', side_effect=lambda settings, current: current):
            app._full_automation_loop(config)
        return app, starts, budgets, config

    def test_expired_50_minute_cycle_keeps_hourly_cadence_and_gets_new_budget(self):
        app, starts, budgets, config = self.run_two_cycles()
        self.assertEqual(starts, [0, 3600])
        self.assertIsNot(budgets[0], budgets[1])
        self.assertNotIn('budget', config)
        app.naver_bot.reset_stop.assert_called_once()
        errors = [event for event in app.events.queue if event[0] == 'auto_error']
        self.assertEqual(len(errors), 1)
        self.assertIn('회차 시간 예산 도달', errors[0][1])

    def test_late_google_worker_keeps_its_stop_signal_until_owner_guard_runs(self):
        app, starts, _, _ = self.run_two_cycles(google_alive=True)
        self.assertEqual(starts, [0, 3600])
        app.naver_bot.reset_stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
