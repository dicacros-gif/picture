import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import picture_cleaner_pc as app_module
from picture_cleaner_pc import PictureCleanerApp
from blog_cli_bridge import BlogCliError
from blog_preferences import DEFAULT_BLOCKED_TERMS, blocked_term_hits, normalize_preferences
from blog_runtime import CliAccessRequired, account_problem, restart_command, wait_for_restart_parent
from blog_workflow import BlogWorkflow, WorkflowError, _json_hash, rank_topics


def bare_app():
    app = object.__new__(PictureCleanerApp)
    app.events = queue.Queue()
    app.full_auto_stop = threading.Event()
    app.naver_bot = MagicMock()
    app.naver_bot.stop_event = threading.Event()
    app.naver_bot.reset_stop.side_effect = app.naver_bot.stop_event.clear
    app._naver_log, app.status, app.root = MagicMock(), MagicMock(), MagicMock()
    app.naver_task_active = app.full_auto_active = app.realtime_task_active = app.cli_login_active = False
    app._closing = app._restart_requested = app._login_success_pending = app._resume_after_login = False
    app._launch_auto_pending, app._startup_refresh_finished = True, False
    app._login_generation, app.cli_login_cancel, app._login_window = 0, threading.Event(), None
    app.auto_start_on_launch = MagicMock()
    app.auto_start_on_launch.get.return_value = True
    app.cli_preferences = {"order": ["chatgpt", "claude", "antigravity", "chatgpt"], "step_count": 3}
    app.cli_capability_text = MagicMock()
    app.cli_app_dir = Path("unused")
    return app


class BlockedTopicTests(unittest.TestCase):
    def test_default_launch_and_blocked_words_and_explicit_overrides(self):
        pref = normalize_preferences({}, "prompt")
        self.assertTrue(pref["auto_start_on_launch"])
        self.assertEqual(pref["blocked_terms"], DEFAULT_BLOCKED_TERMS)
        pref["blocked_terms"].append("my term")
        self.assertNotIn("my term", DEFAULT_BLOCKED_TERMS)
        custom = normalize_preferences({"auto_start_on_launch": False, "blocked_terms": []}, "prompt")
        self.assertFalse(custom["auto_start_on_launch"])
        self.assertEqual(custom["blocked_terms"], [])

    def test_case_spacing_and_compatibility_characters_cannot_evade_blocking(self):
        self.assertIn("KBO", blocked_term_hits("ｋｂｏ 리그"))
        self.assertIn("극단적 선택", blocked_term_hits("극단적\n선택"))
        self.assertEqual(blocked_term_hits("한국 경기 전망", []), [])

    def test_entire_topic_rejected_if_any_source_related_result_is_blocked(self):
        groups = {"one": ["사진 정리", "야구 일정", "지역 지원"]}
        related = {"사진 정리": {"a": ["사진 정리 방법"]},
                   "야구 일정": ["야구 일정 확인"],
                   "지역 지원": {"a": ["지역 지원 조건"], "b": ["지역 지원 추모 행사"]}}
        ranked = rank_topics(groups, related)
        self.assertEqual([item["topic"] for item in ranked], ["사진 정리"])

    def test_cli_never_receives_blocked_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = MagicMock()
            workflow = BlogWorkflow(bridge, Path(folder), lambda _: None, threading.Event())
            with self.assertRaises(WorkflowError):
                workflow.select_topic([{"topic": "프로야구", "keywords": ["프로야구 일정"]}])
            bridge.run_text.assert_not_called()

    def test_topic_blocked_before_any_autocomplete_or_generation(self):
        app = bare_app()
        app._prepare_cli_worker = MagicMock()
        with patch.object(app_module, "fetch_autocomplete") as fetch, self.assertRaisesRegex(WorkflowError, "차단.*축구"):
            app._prepare_manual_cli_worker("축구 소식", [], {})
        fetch.assert_not_called()
        app._prepare_cli_worker.assert_not_called()

    def test_manual_related_block_reports_reason_and_never_generates(self):
        app = bare_app()
        app._prepare_cli_worker = MagicMock()
        with patch.object(app_module, "fetch_autocomplete", return_value={"a": ["지역지원 방법", "지역지원 추모 행사"]}), \
             self.assertRaisesRegex(WorkflowError, "차단.*추모"):
            app._prepare_manual_cli_worker("지역지원", [], {})
        app._prepare_cli_worker.assert_not_called()


class CandidateRetryTests(unittest.TestCase):
    def make_cycle(self, directory):
        app = bare_app()
        app.cli_app_dir, app.cli_bridge = Path(directory), MagicMock()
        app.auto_history = []
        app._preflight_cli_accounts = MagicMock()
        app._cli_realtime_groups = MagicMock(return_value={"a": ["A", "B", "C", "D"]})
        ranked = [{"topic": key, "keywords": [f"{key} 방법"], "score": 10, "reason": "질문"} for key in "ABCD"]
        app._rank_longtail_topics = MagicMock(return_value=(ranked, {key: {"a": [f"{key} 방법"]} for key in "ABCD"}))
        app._prepare_cli_worker = MagicMock(side_effect=lambda topic, words, cfg: {"topic": topic, "run_dir": directory})
        app._publish_cli_worker = MagicMock(return_value={"published": True, "status": "published"})
        config = {"steps": ["chatgpt"], "models": {}, "publish": True, "completion_label": "자동 발행"}
        return app, config

    def test_generation_failure_does_not_repeat_preparation_in_same_cycle(self):
        with tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
            app, config = self.make_cycle(folder)
            workflow.return_value.select_topic.side_effect = WorkflowError("의미 거절")
            app._prepare_cli_worker.side_effect = [WorkflowError("이미지 검수 실패"), {"run_dir": folder}]
            with self.assertRaises(WorkflowError):
                app._cli_automation_cycle(config)
            app._rank_longtail_topics.assert_called_once()
            self.assertEqual(workflow.return_value.select_topic.call_count, 1)
            self.assertEqual(len(workflow.return_value.select_topic.call_args.args[0]), 4)
            self.assertEqual([call.args[0] for call in app._prepare_cli_worker.call_args_list], ["A"])
            app._publish_cli_worker.assert_not_called()

    def test_exhausted_attempts_keep_topic_across_next_cycle(self):
        with tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
            app, config = self.make_cycle(folder)
            workflow.return_value.select_topic.return_value = {"topic": "A", "keywords": ["A 방법"]}
            app._prepare_cli_worker.side_effect = WorkflowError("도구 실패")
            with self.assertRaisesRegex(WorkflowError, "주제를 바꾸지 않고"):
                app._cli_automation_cycle(config)
            self.assertEqual([c.args[0] for c in app._prepare_cli_worker.call_args_list], ["A"])
            app._publish_cli_worker.assert_not_called()
            app._prepare_cli_worker.side_effect = None
            app._prepare_cli_worker.return_value = {"topic": "A", "run_dir": folder}
            app._cli_automation_cycle(config)
            workflow.return_value.select_topic.assert_called_once()
            app._rank_longtail_topics.assert_called_once()
            self.assertFalse((Path(folder) / "pending-blog-topic.json").exists())

    def test_quality_failure_saves_private_draft_without_repeating_and_releases_next_topic(self):
        with tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
            app, config = self.make_cycle(folder)
            from blog_topic_history import TopicHistory
            app.topic_history = TopicHistory(Path(folder) / 'published-topic-history.json')
            config['blog_id'] = 'testowner'
            run = Path(folder) / "blog-runs" / "quality-hold"
            run.mkdir(parents=True)
            article = {"title": "검토가 더 필요한 제목", "paragraphs": [f"검토 문단 {i}." for i in range(8)]}
            (run / "stage-1-chatgpt.json").write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
            (run / "stage-1-chatgpt.checkpoint.json").write_text(json.dumps({
                "response_name": "stage-1-chatgpt", "article_sha256": _json_hash(article)
            }), encoding="utf-8")
            (run / "manifest.json").write_text(json.dumps({"images": [], "image_candidates": []}), encoding="utf-8")
            workflow.return_value.select_topic.return_value = {"topic": "A", "keywords": ["A 방법"]}
            app._prepare_cli_worker.side_effect = WorkflowError(
                "팩트 보강 단계가 기존 제목·문단을 변경했습니다", run)
            app._publish_cli_worker.return_value = {"saved": True, "published": False, "status": "draft_saved",
                "draft_confirmation_verified": True, "article_key": "a" * 64, "blog_id": "testowner",
                "paragraph_count": 8, "image_count": 0, "saved_at": "2026-09-13T19:58:00+09:00",
                "url": "https://blog.naver.com/testowner/postwrite"}

            app._cli_automation_cycle(config)

            self.assertEqual(app._prepare_cli_worker.call_count, 1)
            self.assertEqual(app._publish_cli_worker.call_count, 1)
            args, kwargs = app._publish_cli_worker.call_args
            self.assertFalse(args[1]["publish"])
            self.assertTrue(args[1]["save_draft"])
            self.assertTrue(kwargs["allow_quality_draft"])
            self.assertFalse((Path(folder) / "pending-blog-topic.json").exists())
            self.assertTrue(app.auto_history[-1]["quality_hold"])

    def test_uncertain_publication_does_not_reselect_or_resubmit(self):
        with tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
            app, config = self.make_cycle(folder)
            workflow.return_value.select_topic.return_value = {"topic": "A", "keywords": ["A 방법"]}
            app._publish_cli_worker.side_effect = RuntimeError("timeout")
            with self.assertRaises(RuntimeError):
                app._cli_automation_cycle(config)
            with self.assertRaisesRegex(WorkflowError, "이전 발행 결과"):
                app._cli_automation_cycle(config)
            workflow.return_value.select_topic.assert_called_once()
            app._publish_cli_worker.assert_called_once()

    def test_three_total_attempts_never_reach_fourth_candidate(self):
        with tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
            app, config = self.make_cycle(folder)
            workflow.return_value.select_topic.side_effect = WorkflowError("모두 거절")
            app._cli_automation_cycle(config)
            self.assertEqual(workflow.return_value.select_topic.call_count, 1)
            self.assertEqual(app._prepare_cli_worker.call_args.args[:2], ("A", ["A 방법"]))
            app._publish_cli_worker.assert_called_once()

    def test_selection_compares_twelve_then_next_twelve(self):
        with tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
            app, config = self.make_cycle(folder)
            ranked = [{"topic": f"T{i}", "keywords": [f"T{i} 방법"], "score": 30 - i, "reason": "의도"} for i in range(24)]
            app._rank_longtail_topics.return_value = (ranked, {row["topic"]: {} for row in ranked})
            workflow.return_value.select_topic.side_effect = [WorkflowError("첫 묶음 거절"),
                {"topic": "T12", "keywords": ["T12 방법"]}]
            app._cli_automation_cycle(config)
            calls = workflow.return_value.select_topic.call_args_list
            self.assertEqual([len(call.args[0]) for call in calls], [12, 12])
            app._prepare_cli_worker.assert_called_once_with("T12", ["T12 방법"], config)

    def test_publication_failure_never_tries_another_candidate(self):
        for outcome in [RuntimeError("publish timeout"), {"published": False, "status": "uncertain"}]:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
                app, config = self.make_cycle(folder)
                workflow.return_value.select_topic.return_value = {"topic": "A", "keywords": ["A 방법"]}
                if isinstance(outcome, Exception):
                    app._publish_cli_worker.side_effect = outcome
                else:
                    app._publish_cli_worker.return_value = outcome
                with self.assertRaises(RuntimeError):
                    app._cli_automation_cycle(config)
                workflow.return_value.select_topic.assert_called_once()
                app._prepare_cli_worker.assert_called_once()
                app._publish_cli_worker.assert_called_once()

    def test_stop_during_preparation_never_retries_or_publishes(self):
        with tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
            app, config = self.make_cycle(folder)
            workflow.return_value.select_topic.return_value = {"topic": "A", "keywords": ["A 방법"]}
            def stop(*args):
                app.full_auto_stop.set()
                raise WorkflowError("cancel")
            app._prepare_cli_worker.side_effect = stop
            with self.assertRaisesRegex(WorkflowError, "중지"):
                app._cli_automation_cycle(config)
            workflow.return_value.select_topic.assert_called_once()
            app._publish_cli_worker.assert_not_called()

    def test_login_failure_before_ranking_does_not_consume_three_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            app, config = self.make_cycle(folder)
            app._preflight_cli_accounts.side_effect = CliAccessRequired({"claude": "로그인 필요"})
            with self.assertRaises(CliAccessRequired):
                app._cli_automation_cycle(config)
            app._cli_realtime_groups.assert_not_called()
            app._rank_longtail_topics.assert_not_called()

    def test_runtime_wrapped_auth_error_pauses_without_next_candidate(self):
        with tempfile.TemporaryDirectory() as folder, patch("blog_controls.BlogWorkflow") as workflow:
            app, config = self.make_cycle(folder)
            workflow.return_value.select_topic.return_value = {"topic": "A", "keywords": ["A 방법"]}
            error = WorkflowError("실제 요청 로그인 실패")
            error.__cause__ = BlogCliError("authentication_required", "login", provider="antigravity")
            app._prepare_cli_worker.side_effect = error
            with self.assertRaises(CliAccessRequired):
                app._cli_automation_cycle(config)
            workflow.return_value.select_topic.assert_called_once()


class LaunchAndLoginTests(unittest.TestCase):
    def test_launch_only_after_finished_event_and_browser_release_once(self):
        app = bare_app()
        app.start_cli_automation = MagicMock()
        app._maybe_launch_automation()
        app.start_cli_automation.assert_not_called()
        app._handle_runtime_event(("realtime_finished", True))
        app.realtime_task_active = True
        app._maybe_launch_automation()
        app.start_cli_automation.assert_not_called()
        app.realtime_task_active = False
        app._maybe_launch_automation()
        app._maybe_launch_automation()
        app.start_cli_automation.assert_called_once_with(automatic=True)

    def test_stop_or_disabled_option_prevents_late_startup_event(self):
        for stop in (True, False):
            app = bare_app()
            app.start_cli_automation = MagicMock()
            if stop:
                app.stop_full_automation()
            else:
                app.auto_start_on_launch.get.return_value = False
            app._handle_runtime_event(("realtime_finished", True))
            app._maybe_launch_automation()
            app.start_cli_automation.assert_not_called()

    def test_refresh_finally_emits_event_after_releasing_browser_on_failure(self):
        app = bare_app()
        app.blog_id = MagicMock()
        app.blog_id.get.return_value = "owner"
        app._run_realtime_worker = MagicMock(side_effect=RuntimeError("fetch failed"))
        with patch.object(app_module.threading, "Thread") as thread:
            app.run_realtime(startup=True)
            self.assertTrue(app.realtime_task_active)
            thread.call_args.kwargs["target"]()
        self.assertFalse(app.realtime_task_active)
        self.assertIn(("realtime_finished", True), list(app.events.queue))

    def test_automatic_configuration_error_has_no_messagebox_or_worker(self):
        app = bare_app()
        app._cli_configuration = MagicMock(side_effect=ValueError("bad saved settings"))
        with patch("blog_controls.messagebox.showinfo") as modal, patch("blog_controls.threading.Thread") as worker:
            app.start_cli_automation(automatic=True)
        modal.assert_not_called()
        worker.assert_not_called()
        app._cli_configuration.assert_called_once_with(silent=True)

    def test_account_failure_exits_loop_and_queues_non_modal_recovery(self):
        app = bare_app()
        problem = CliAccessRequired({"claude": "로그인 필요"})
        app._run_full_automation_cycle = MagicMock(side_effect=problem)
        with patch.object(app_module, "next_cycle_tick") as wait:
            app._full_automation_loop({"interval_seconds": 3600})
        wait.assert_not_called()
        self.assertFalse(app.full_auto_active)
        self.assertIn(("cli_access_required", problem, True), list(app.events.queue))

    def test_cli_order_is_respected_without_substituting_provider(self):
        statuses = {"chatgpt": {"installed": True, "auth_status": "available"},
                    "antigravity": {"installed": True},
                    "claude": {"installed": True, "auth_status": "authentication_required"}}
        self.assertIsNone(account_problem(statuses, ["chatgpt", "antigravity", "chatgpt"]))
        self.assertIn("claude", account_problem(statuses, ["claude"]).failures)

    def test_login_console_exit_triggers_account_check_then_one_automatic_resume(self):
        app = bare_app()
        app._launch_auto_pending, app._resume_after_login = False, True
        app.start_cli_automation = MagicMock()
        statuses = {key: {"installed": True, "auth_status": "available"} for key in ("chatgpt", "claude", "antigravity")}
        with patch("blog_runtime.BlogCliBridge") as bridge, patch("blog_runtime.threading.Thread") as thread:
            bridge.return_value.open_login.return_value.poll.return_value = 0
            bridge.return_value.check_accounts.return_value = statuses
            app._open_blog_cli_login("claude")
            bridge.return_value.check_accounts.assert_not_called()
            thread.call_args.kwargs["target"]()
            bridge.return_value.open_login.assert_called_once_with("claude", return_process=True)
            bridge.return_value.check_accounts.assert_called_once()
        app._handle_runtime_event(app.events.get_nowait())
        app._maybe_launch_automation()
        app._maybe_launch_automation()
        app.start_cli_automation.assert_called_once_with(automatic=True)

    def test_stop_cancels_even_already_queued_login_success(self):
        app = bare_app()
        app._resume_after_login, app.cli_login_active = True, True
        app.start_cli_automation = MagicMock()
        event = ("cli_login_checked", 0, {key: {"installed": True} for key in ("chatgpt", "claude", "antigravity")}, "")
        app.stop_full_automation()
        app._handle_runtime_event(event)
        app._maybe_launch_automation()
        self.assertFalse(app.cli_login_active)
        app.start_cli_automation.assert_not_called()

    def test_login_failure_does_not_resume_automation(self):
        app = bare_app()
        app._resume_after_login, app._launch_auto_pending = True, False
        app.show_cli_login_required, app.start_cli_automation = MagicMock(), MagicMock()
        app._handle_runtime_event(("cli_login_checked", 0, {
            "chatgpt": {"installed": True}, "antigravity": {"installed": True},
            "claude": {"installed": True, "auth_status": "authentication_required"}}, ""))
        app._maybe_launch_automation()
        app.show_cli_login_required.assert_called_once()
        app.start_cli_automation.assert_not_called()

    def test_readonly_account_check_after_stop_uses_fresh_cancellation_event(self):
        app = bare_app()
        app.full_auto_stop.set()
        with patch("blog_controls.BlogCliBridge") as bridge, patch("blog_controls.threading.Thread") as thread:
            bridge.return_value.check_accounts.return_value = {}
            app.check_blog_cli()
            thread.call_args.kwargs["target"]()
            self.assertFalse(bridge.call_args.args[2].is_set())
            bridge.return_value.check_accounts.assert_called_once()

    def test_restart_waits_for_browser_then_saves_before_spawn_and_close(self):
        app = bare_app()
        order = []
        app.save_cli_prompt = MagicMock(side_effect=lambda **kw: order.append("save") or True)
        app.close = MagicMock(side_effect=lambda: order.append("close"))
        app.naver_task_active = True
        with patch("blog_runtime.subprocess.Popen", side_effect=lambda *a, **kw: order.append("spawn")) as spawn:
            app.restart_program()
            spawn.assert_not_called()
            app.naver_task_active = False
            app.root.after.call_args.args[1]()
        self.assertEqual(order, ["save", "spawn", "close"])
        self.assertIn("--wait-parent-pid", spawn.call_args.args[0])

    def test_restart_launch_failure_keeps_current_window_open(self):
        app = bare_app()
        app.save_cli_prompt, app.close = MagicMock(return_value=True), MagicMock()
        with patch("blog_runtime.subprocess.Popen", side_effect=OSError("missing exe")):
            app.restart_program()
        app.close.assert_not_called()
        self.assertFalse(app._restart_requested)

    def test_restart_command_uses_frozen_executable_without_source_file(self):
        with patch("blog_runtime.sys.frozen", True, create=True), patch("blog_runtime.sys.executable", "D:/app.exe"):
            command = restart_command()
        self.assertEqual(command[:2], ["D:/app.exe", "--wait-parent-pid"])

    @unittest.skipUnless(os.name == "nt", "Windows restart synchronization")
    def test_replacement_waits_for_real_process_exit(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            wait_for_restart_parent(["--wait-parent-pid", str(process.pid)])
            self.assertEqual(process.poll(), 0)
        finally:
            process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
