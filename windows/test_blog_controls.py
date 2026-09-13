import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from tkinter import Tk
from unittest.mock import MagicMock, patch

import picture_cleaner_pc as app_module
from blog_preferences import DEFAULT_ORDER, normalize_preferences, store_prompt
from picture_cleaner_pc import PictureCleanerApp
from blog_controls import next_cycle_tick
from blog_topic_history import TopicHistoryError


class BlogPreferenceTests(unittest.TestCase):
    def test_legacy_prompt_migrates_and_duplicate_cli_steps_are_preserved(self):
        pref = normalize_preferences(None, "default", "saved custom instructions")
        self.assertEqual(pref["prompts"][0]["text"], "default")
        self.assertEqual(pref["prompts"][1]["text"], "saved custom instructions")
        self.assertEqual(pref["order"], ["chatgpt", "claude", "antigravity", "chatgpt"])
        custom = normalize_preferences({**pref, "step_count": 2, "order": ["antigravity", "chatgpt", "claude", "chatgpt"]}, "default")
        self.assertEqual(custom["step_count"], 2)
        self.assertEqual(custom["order"][0], "antigravity")

    def test_saved_prompts_are_independent_and_names_unique(self):
        original = normalize_preferences(None, "original text")
        added = store_prompt(original, "default", "생활 정보", "생활 정보용 지침", create=True)
        self.assertEqual(len(original["prompts"]), 1)
        self.assertEqual(len(added["prompts"]), 2)
        changed = store_prompt(added, added["selected_prompt_id"], "생활 정보", "수정된 지침")
        self.assertEqual(changed["prompts"][0]["text"], "original text")
        with self.assertRaises(ValueError):
            store_prompt(changed, "default", "생활 정보", "duplicate", create=True)

    def test_invalid_saved_order_is_repaired(self):
        pref = normalize_preferences({"order": ["unknown"], "step_count": "bad", "models": "bad"}, "default")
        self.assertEqual(pref["order"], DEFAULT_ORDER)
        self.assertEqual(pref["step_count"], 4)
        self.assertEqual(pref["models"]["chatgpt"], "")

    def test_hourly_cadence_does_not_add_generation_duration(self):
        self.assertEqual(next_cycle_tick(0, 900, 3600), 3600)
        self.assertEqual(next_cycle_tick(3600, 4400, 3600), 7200)
        self.assertEqual(next_cycle_tick(0, 7500, 3600), 10800)
        self.assertEqual(next_cycle_tick(0, 900, 7200), 7200)


class BlogUiTests(unittest.TestCase):
    def make_app(self, folder, settings):
        patches = [patch.object(app_module, "APP_DIR", Path(folder)),
                   patch.object(app_module, "CONFIG_FILE", Path(folder) / "settings.json"),
                   patch.object(app_module, "load_json", side_effect=lambda path, default: settings if Path(path).name == "settings.json" else default),
                   patch.object(PictureCleanerApp, "run_realtime"),
                   patch.object(PictureCleanerApp, "_maximize_window")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        root = Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        return PictureCleanerApp(root)

    def test_prompt_selection_order_and_publish_choice_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, {})
            app.cli_preset_name.set("질문 해결형")
            app.base_text.delete("1.0", "end")
            app.base_text.insert("1.0", "독자 질문에 바로 답하고 조건과 차이를 설명한다.")
            self.assertTrue(app.save_cli_prompt(create=True))
            app.cli_step_count.set("2")
            app.cli_order[0].set("Antigravity CLI")
            app.cli_order[1].set("ChatGPT CLI (Codex)")
            app.cli_publication.set("편집기에 입력만")
            app._save_cli_selection()
            saved = json.loads((Path(directory) / "settings.json").read_text(encoding="utf-8"))
            restored = self.make_app(directory, saved)
            self.assertEqual(restored.cli_preset_choice.get(), "질문 해결형")
            self.assertEqual(restored.cli_step_count.get(), "2")
            self.assertEqual(restored.cli_order[0].get(), "Antigravity CLI")
            self.assertEqual(restored.cli_publication.get(), "편집기에 입력만")
            self.assertIn("독자 질문", restored.base_text.get("1.0", "end"))
            config = restored._cli_configuration()
            self.assertEqual(config["steps"], ["antigravity", "chatgpt"])
            self.assertFalse(config["publish"])
            self.assertFalse(hasattr(restored, "api_key"))

    def test_startup_persists_migrated_image_typography_policy(self):
        old_prompt = ("내가 저장한 지침\n\n[제목 통합 작성 규칙 · 2026-09-13 v2]\n제목 지침\n\n"
                      "[이미지 문구 최신 규칙 · 2026-09-13]\n"
                      "형광 녹색만 사용하고 빨간 글자는 쓰지 않습니다.")
        settings = {"cli_workflow": {
            "default_revision": "user-20260913-title-synthesis-v2",
            "prompts": [{"id": "saved", "name": "저장값", "text": old_prompt}],
            "selected_prompt_id": "saved",
        }}
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, settings)
            saved = json.loads((Path(directory) / "settings.json").read_text(encoding="utf-8"))
            text = saved["cli_workflow"]["prompts"][0]["text"]
            self.assertIn("[이미지 문구 최신 규칙 · 2026-09-13 v2]", text)
            self.assertIn("형광 빨간색", text)
            self.assertNotIn("빨간 글자는 쓰지 않습니다", text)
            self.assertIn("내가 저장한 지침", app.base_text.get("1.0", "end"))

    def test_default_migration_preserves_user_presets_with_colliding_name_and_id(self):
        prompts = [{"id": "mine", "name": "사용자 기본 프롬프트 · 통합 확장 제목", "text": "사용자 직접 작성한 문장"},
                   {"id": "user-default-20260913-title-synthesis-v2", "name": "직접 저장한 옵션", "text": "보존할 두 번째 내용"}]
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, {"cli_workflow": {"prompts": prompts}})
            saved = app.cli_preferences["prompts"]
            self.assertEqual(saved[1:], prompts)
            self.assertEqual(saved[0]["name"], "사용자 기본 프롬프트 · 통합 확장 제목 (2)")
            self.assertNotEqual(saved[0]["id"], prompts[1]["id"])
            app._save_cli_selection()
            settings = json.loads((Path(directory) / "settings.json").read_text(encoding="utf-8"))
            restored = self.make_app(directory, settings)
            self.assertEqual(restored.cli_preferences["prompts"], settings["cli_workflow"]["prompts"])
            self.assertEqual(restored.cli_preferences["prompts"][1:], prompts)

    def test_required_login_failure_stops_before_google_or_generation(self):
        app = object.__new__(PictureCleanerApp)
        app.cli_bridge = MagicMock()
        app.cli_bridge.check_accounts.return_value = {
            "chatgpt": {"installed": True, "auth_status": "available"},
            "claude": {"installed": True, "auth_status": "authentication_required"},
            "antigravity": {"installed": True, "text_status": "not_checked"}}
        app.events, app.naver_bot = MagicMock(), MagicMock()
        config = {"steps": DEFAULT_ORDER, "include_google": True}
        with patch("blog_controls.BlogWorkflow") as workflow, self.assertRaisesRegex(RuntimeError, "Claude CLI.*로그인"):
            app._prepare_cli_worker("주제", [], config)
        app.naver_bot.capture_google_reference_candidates.assert_not_called()
        workflow.assert_not_called()
        self.assertEqual(config["steps"], DEFAULT_ORDER)

    def test_unselected_claude_and_unknown_antigravity_login_do_not_block(self):
        app = object.__new__(PictureCleanerApp)
        app.cli_bridge, app.events, app._naver_log = MagicMock(), MagicMock(), MagicMock()
        statuses = {"chatgpt": {"installed": True, "auth_status": "available"},
                    "claude": {"installed": False, "text_status": "authentication_required"},
                    "antigravity": {"installed": True, "text_status": "not_checked"}}
        app.cli_bridge.check_accounts.return_value = statuses
        self.assertEqual(app._preflight_cli_accounts({"steps": ["antigravity", "chatgpt"]}), statuses)
        statuses["antigravity"]["installed"] = False
        with self.assertRaisesRegex(RuntimeError, "Antigravity CLI.*설치"):
            app._preflight_cli_accounts({"steps": ["chatgpt"]})

    def test_topic_selection_uses_run_snapshot_while_manual_selection_uses_current_preferences(self):
        app = object.__new__(PictureCleanerApp)
        topic = "배터리 수명 늘리기"
        groups = {"검색": [topic]}
        app.topic_history, app.cli_bridge, app.events, app._naver_log = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        app.topic_history.filter_groups.return_value = groups
        app.topic_history.blocked_topics.return_value = []
        app.full_auto_stop, app.cli_app_dir = threading.Event(), Path("unused")
        app.cli_preferences = {"order": ["claude"], "models": {"claude": "current-model"}}
        config = {"steps": ["antigravity", "chatgpt"], "models": {"antigravity": "saved-model"}}
        with patch.object(app_module, "fetch_autocomplete", return_value={"검색": ["배터리 설정"]}), \
             patch.object(app_module, "BlogWorkflow") as workflow:
            workflow.rank_topics.return_value = [{"topic": topic, "score": 10}]
            workflow.return_value.select_topic.return_value = {"topic": topic, "keywords": ["배터리 설정"], "score": 10, "reason": "관심"}
            app._select_longtail_topic(groups, config=config)
            self.assertEqual(workflow.return_value.select_topic.call_args.kwargs, {"provider": "antigravity", "model": "saved-model", "blocked_terms": None})
            app._select_longtail_topic(groups)
            self.assertEqual(workflow.return_value.select_topic.call_args.kwargs, {"provider": "claude", "model": "current-model", "blocked_terms": None})

    def test_automatic_cycle_passes_same_snapshot_to_selection_and_preparation(self):
        app = object.__new__(PictureCleanerApp)
        config = {"steps": ["antigravity"], "models": {"antigravity": "saved-model"}, "publish": True}
        groups = {"검색": ["주제"]}
        app.full_auto_stop = threading.Event()
        app.cli_bridge, app._naver_log = MagicMock(), MagicMock()
        app._preflight_cli_accounts = MagicMock()
        app._cli_realtime_groups = MagicMock(return_value=groups)
        app._rank_longtail_topics = MagicMock(return_value=([{"topic": "주제", "keywords": ["주제 연관"]}], {"주제": {}}))
        app._publish_cli_worker = MagicMock(return_value={"published": True})
        app.auto_history = []
        app.events = MagicMock()
        with tempfile.TemporaryDirectory() as directory, patch("blog_controls.BlogWorkflow") as workflow:
            app.cli_app_dir = Path(directory)
            app._prepare_cli_worker = MagicMock(return_value={"run_dir": directory})
            workflow.return_value.select_topic.return_value = {"topic": "주제", "keywords": ["주제 연관"]}
            app._cli_automation_cycle(config)
            self.assertEqual(workflow.return_value.select_topic.call_args.kwargs, {"provider": "antigravity", "model": "saved-model", "blocked_terms": None})
        app._rank_longtail_topics.assert_called_once_with(groups, config=config)
        app._prepare_cli_worker.assert_called_once_with("주제", ["주제 연관"], config)

    def test_launch_checkbox_and_edited_blocked_terms_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, {})
            self.assertTrue(app.auto_start_on_launch.get())
            app.auto_start_on_launch.set(False)
            app.cli_blocked_terms.delete("1.0", "end")
            app.cli_blocked_terms.insert("1.0", "야구, 사망\n맞춤 금지어")
            app._save_launch_choice()
            settings = json.loads((Path(directory) / "settings.json").read_text(encoding="utf-8"))
            restored = self.make_app(directory, settings)
            self.assertFalse(restored.auto_start_on_launch.get())
            self.assertFalse(restored._launch_auto_pending)
            self.assertEqual(restored._cli_configuration()["blocked_terms"], ["야구", "사망", "맞춤 금지어"])

    def test_manual_topic_fetches_related_words_in_worker_even_when_tabs_are_empty(self):
        app = object.__new__(PictureCleanerApp)
        app.topic, app.seed = MagicMock(), MagicMock()
        app.topic.get.return_value = "기부금 공제"
        app.seed.get.return_value = "이전 주제"
        config = {"steps": ["chatgpt"]}
        app._cli_configuration = MagicMock(return_value=config)
        app._all_related_keywords = MagicMock(return_value=[])
        app._start_cli_job, app._prepare_cli_worker, app._naver_log = MagicMock(), MagicMock(), MagicMock()
        app.full_auto_stop = threading.Event()
        with patch.object(app_module, "fetch_autocomplete", return_value={"네이버": ["기부금 공제 한도"]}) as fetch:
            app.prepare_cli_article()
            fetch.assert_not_called()
            app.topic.get.return_value = "나중에 바뀐 입력어"
            app._start_cli_job.call_args.args[1]()
        fetch.assert_called_once_with("기부금 공제")
        app._prepare_cli_worker.assert_called_once_with("기부금 공제", ["기부금 공제 한도"], config)
        app.topic.get.assert_called_once()
        app.seed.get.assert_not_called()

    def test_manual_preparation_excludes_stale_and_prefix_only_words_but_keeps_matching_user_words(self):
        app = object.__new__(PictureCleanerApp)
        app.full_auto_stop = threading.Event()
        app._prepare_cli_worker, app._naver_log = MagicMock(), MagicMock()
        config = {"steps": ["antigravity"]}
        with patch.object(app_module, "fetch_autocomplete", return_value={
            "네이버": ["기부금 공제", "기부금 공제 한도", "기부금 행사", "사진 정리 방법"],
            "구글": ["기부금공제 한도", "기부금 공제 신청 서류"]}):
            app._prepare_manual_cli_worker("기부금 공제", ["사진 정리 비용", "기부금 행사 일정", "기부금 공제 개인사업자 조건"], config)
        app._prepare_cli_worker.assert_called_once_with("기부금 공제",
            ["기부금 공제 한도", "기부금 공제 신청 서류", "기부금 공제 개인사업자 조건"], config)

    def test_manual_preparation_without_real_matches_stops_before_generation(self):
        app = object.__new__(PictureCleanerApp)
        app.full_auto_stop = threading.Event()
        app._prepare_cli_worker, app._naver_log = MagicMock(), MagicMock()
        with patch.object(app_module, "fetch_autocomplete", return_value={"네이버": [], "구글": ["다른 주제"]}), \
             self.assertRaisesRegex(RuntimeError, "전체 주제와 일치하는 연관 검색어"):
            app._prepare_manual_cli_worker("기부금 공제", ["이전 검색어"], {})
        app._prepare_cli_worker.assert_not_called()

    def test_cancelling_manual_related_lookup_does_not_start_generation(self):
        app = object.__new__(PictureCleanerApp)
        app.full_auto_stop = threading.Event()
        app._prepare_cli_worker, app._naver_log = MagicMock(), MagicMock()
        def cancelled_lookup(topic):
            app.full_auto_stop.set()
            return {"네이버": ["기부금 공제 한도"]}
        with patch.object(app_module, "fetch_autocomplete", side_effect=cancelled_lookup), self.assertRaisesRegex(RuntimeError, "중지"):
            app._prepare_manual_cli_worker("기부금 공제", [], {})
        app._prepare_cli_worker.assert_not_called()

    def test_switching_presets_keeps_edits_and_last_preset_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, {})
            app.delete_cli_prompt()
            self.assertEqual(len(app.cli_preferences["prompts"]), 1)
            app.cli_preset_name.set("두 번째")
            self.assertTrue(app.save_cli_prompt(create=True))
            app.base_text.delete("1.0", "end")
            app.base_text.insert("1.0", "두 번째만 수정한 내용")
            app.cli_preset_choice.set("기본 글쓰기")
            app.select_blog_preset()
            app.cli_preset_choice.set("두 번째")
            app.select_blog_preset()
            self.assertEqual(app.base_text.get("1.0", "end").strip(), "두 번째만 수정한 내용")

    def test_uncertain_publication_is_reported_and_not_retried(self):
        app = object.__new__(PictureCleanerApp)
        app.full_auto_stop = threading.Event()
        app.naver_bot = MagicMock()
        app.events = MagicMock()
        app.naver_bot.publish_naver_article.return_value = {"published": False, "status": "uncertain", "message": "발행 확인 필요"}
        with self.assertRaisesRegex(RuntimeError, "발행 확인 필요"):
            app._publish_cli_worker({}, {"blog_id": "owner", "publish": True})
        app.naver_bot.publish_naver_article.assert_called_once()

    def test_draft_choice_is_saved_and_passed_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, {})
            app.cli_publication.set("임시저장까지만")
            app.auto_interval_hours.set("2")
            config = app._cli_configuration()
            self.assertFalse(config["publish"])
            self.assertTrue(config["save_draft"])
            saved = json.loads((Path(directory) / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["auto_interval_hours"], "2")
            self.assertEqual(saved["cli_workflow"]["publication_mode"], "임시저장까지만")
            app.naver_bot = MagicMock()
            app.naver_bot.publish_naver_article.return_value = {"published": False, "saved": True, "status": "draft_saved"}
            app._publish_cli_worker({}, config)
            self.assertEqual(app.naver_bot.publish_naver_article.call_args.kwargs, {"publish": False, "save_draft": True})

    def test_optional_google_images_reach_publisher_without_mutating_six_generated_images(self):
        app = object.__new__(PictureCleanerApp)
        app.full_auto_stop = threading.Event()
        app.naver_bot = MagicMock()
        app.events = MagicMock()
        app.naver_bot.publish_naver_article.return_value = {"published": True, "url": "https://example.test/post"}
        article = {"images": [{"provider": "chatgpt"}] * 6,
                   "google_images": [{"provider": "google", "approved": True}]}
        app._publish_cli_worker(article, {"blog_id": "owner", "publish": True})
        payload = app.naver_bot.publish_naver_article.call_args.args[1]
        self.assertEqual(len(payload["images"]), 7)
        self.assertEqual(len(article["images"]), 6)

    def test_late_keyword_results_cannot_reinsert_consumed_topics(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory, {})
            app.topic_history.record_publication("사진 정리", {"published": True, "status": "published",
                "url": "https://blog.naver.com/owner/223123456789"})
            app.keyword_db = ["사진 정리", "기존 후보"]
            with patch.object(app_module, "DB_FILE", Path(directory) / "keywords.json"):
                app.events.put(("cli_topic_consumed", "사진 정리"))
                app.events.put(("keywords", "사진", {"네이버": ["사진-정리", "사진 저장 방법"]}))
                app._poll()
                self.assertEqual(app.keyword_db, ["기존 후보", "사진 저장 방법"])
                app.seed.set("사진")
                app.related_request_id = 7
                app.events.put(("related_split", 7, "사진", "", {"네이버": ["사진정리", "사진 파일 이동"]}, {}))
                app._poll()
                saved = json.loads((Path(directory) / "keywords.json").read_text(encoding="utf-8"))
                saved_words = [item["keyword"] for item in saved["keywords"]]
                self.assertNotIn("사진정리", saved_words)
                self.assertIn("사진 파일 이동", saved_words)


class BrowserJobConcurrencyTests(unittest.TestCase):
    def make_bare_app(self):
        app = object.__new__(PictureCleanerApp)
        app.naver_task_active = app.full_auto_active = app.realtime_task_active = False
        app.full_auto_stop = threading.Event()
        app.naver_bot = MagicMock()
        app.naver_bot.stop_event = threading.Event()
        app.naver_bot.reset_stop.side_effect = app.naver_bot.stop_event.clear
        app.events = queue.Queue()
        app.status = MagicMock()
        app.blog_id = MagicMock()
        app.blog_id.get.return_value = "owner"
        app._set_cli_runtime_controls = MagicMock()
        return app

    def test_startup_refresh_reserves_browser_before_worker_starts(self):
        app = self.make_bare_app()
        app._run_realtime_worker = MagicMock()
        with patch.object(app_module.threading, "Thread") as thread, patch("blog_controls.messagebox.showinfo"), patch.object(app_module.messagebox, "showinfo"):
            self.assertTrue(app.run_realtime())
            self.assertTrue(app.realtime_task_active)
            worker = thread.call_args.kwargs["target"]
            app._start_cli_job("publish", MagicMock())
            app.start_cli_automation()
            app._start_naver_task("comments", MagicMock())
            self.assertFalse(app.run_realtime())
            self.assertEqual(thread.call_count, 1)
            app.blog_id.get.return_value = "changed-later"
            worker()
        self.assertFalse(app.realtime_task_active)
        app._run_realtime_worker.assert_called_once_with("owner")
        app.blog_id.get.assert_called_once()

    def test_refresh_cannot_navigate_while_cli_comments_or_schedule_are_active(self):
        for flag in ("naver_task_active", "full_auto_active"):
            app = self.make_bare_app()
            setattr(app, flag, True)
            with patch.object(app_module.threading, "Thread") as thread:
                self.assertFalse(app.run_realtime())
                thread.assert_not_called()
            app.naver_bot.reset_stop.assert_not_called()

    def test_refresh_releases_browser_flag_after_fetch_failure(self):
        app = self.make_bare_app()
        app._run_realtime_worker = MagicMock(side_effect=RuntimeError("fetch failed"))
        with patch.object(app_module.threading, "Thread") as thread:
            app.run_realtime()
            thread.call_args.kwargs["target"]()
        self.assertFalse(app.realtime_task_active)

    def test_cancelled_refresh_does_not_start_following_browser_requests(self):
        app = self.make_bare_app()
        def stop_after_http_fetch():
            app.full_auto_stop.set()
            return {"다음": ["주제"]}
        with patch.object(app_module, "fetch_realtime_groups", side_effect=stop_after_http_fetch):
            app._run_realtime_worker("owner")
        app.naver_bot.fetch_adsensefarm_realtime.assert_not_called()
        app.naver_bot.fetch_daum_realtime_trends.assert_not_called()
        app.naver_bot.fetch_google_trending_now.assert_not_called()
        app.naver_bot.fetch_creator_advisor_trends.assert_not_called()

    def test_history_failure_keeps_publish_result_visible_and_stops_next_cycle(self):
        app = self.make_bare_app()
        result = {"published": True, "status": "published", "url": "https://blog.naver.com/owner/223123456789"}
        app.naver_bot.publish_naver_article.return_value = result
        app.topic_history = MagicMock()
        app.topic_history.record_publication.side_effect = TopicHistoryError("disk full")
        config = {"blog_id": "owner", "publish": True, "interval_seconds": 3600, "interval_hours": 1}
        article = {"topic": "사진 정리", "run_dir": "owned-run"}
        app._run_full_automation_cycle = MagicMock(side_effect=lambda config, **kwargs: app._publish_cli_worker(article, config))
        with patch.object(app_module, "next_cycle_tick") as next_tick:
            app._full_automation_loop(config)
            next_tick.assert_not_called()
        app._run_full_automation_cycle.assert_called_once()
        self.assertTrue(app.full_auto_stop.is_set())
        self.assertTrue(app.naver_bot.stop_event.is_set())
        recorded_events = list(app.events.queue)
        self.assertIn(("cli_publication", result), recorded_events)
        self.assertTrue(any(event[0] == "naver_log" and "발행은 완료됐지만" in event[1] for event in recorded_events))


if __name__ == "__main__":
    unittest.main()
