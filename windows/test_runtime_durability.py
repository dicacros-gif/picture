from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from tkinter import Tk
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from blog_preferences import atomic_json_write, load_settings_json, save_settings_json, normalize_preferences
from blog_runtime import application_instance_lock
from blog_topic_history import TopicHistory
from keyword_database import load_database, merge, update_database, words
import picture_cleaner_pc as app_module
from progress_panel import ProgressPanel


class AtomicStorageTests(unittest.TestCase):
    def test_settings_backup_recovers_latest_selected_prompt_and_preserves_damage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            settings = {"blog_id": "saved-owner", "cli_workflow": {
                "auto_start_on_launch": False, "selected_prompt_id": "custom",
                "prompts": [{"id": "custom", "name": "내 지침", "text": "사용자가 수정한 문체"}]}}
            save_settings_json(path, settings)
            self.assertEqual(json.loads((Path(folder) / "settings.last-good.json").read_text(encoding="utf-8")), settings)
            damaged = b'{"blog_id": "truncated'
            path.write_bytes(damaged)
            restored = load_settings_json(path)
            self.assertEqual(restored["blog_id"], "saved-owner")
            self.assertEqual(restored["cli_workflow"]["selected_prompt_id"], "custom")
            self.assertEqual(restored["cli_workflow"]["prompts"][0]["text"], "사용자가 수정한 문체")
            self.assertEqual(restored["_settings_recovery"]["status"], "restored")
            self.assertEqual(Path(restored["_settings_recovery"]["damaged_path"]).read_bytes(), damaged)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), settings)
            save_settings_json(path, restored)
            self.assertNotIn("_settings_recovery", json.loads(path.read_text(encoding="utf-8")))

    def test_unrecoverable_settings_never_enable_default_account_or_auto_publish(self):
        for damaged in (b'{bad json', b'[]', b'{"cli_workflow": null}', b'{"number": NaN}'):
            with self.subTest(damaged=damaged), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "settings.json"
                path.write_bytes(damaged)
                loaded = load_settings_json(path, {})
                pref = normalize_preferences(loaded["cli_workflow"], "default")
                self.assertEqual(loaded["blog_id"], "")
                self.assertFalse(pref["auto_start_on_launch"])
                self.assertEqual(pref["publication_mode"], "편집기에 입력만")
                self.assertEqual(path.read_bytes(), damaged)
                self.assertEqual(Path(loaded["_settings_recovery"]["damaged_path"]).read_bytes(), damaged)

    def test_valid_settings_load_creates_last_good_backup_for_existing_installs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            original = {"blog_id": "existing-user", "cli_workflow": {"auto_start_on_launch": False}}
            atomic_json_write(path, original)
            self.assertEqual(load_settings_json(path), original)
            self.assertEqual(json.loads((Path(folder) / "settings.last-good.json").read_text(encoding="utf-8")), original)

    def test_damage_preservation_failure_keeps_original_and_disables_automatic_work(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            save_settings_json(path, {"blog_id": "owner", "cli_workflow": {"auto_start_on_launch": True}})
            path.write_bytes(b'broken settings')
            with patch("blog_preferences.shutil.copy2", side_effect=PermissionError("cannot preserve original")):
                loaded = load_settings_json(path)
            self.assertEqual(path.read_bytes(), b'broken settings')
            self.assertEqual(loaded["blog_id"], "")
            self.assertFalse(loaded["cli_workflow"]["auto_start_on_launch"])

    def test_legacy_recovery_and_repeat_receipt_preserve_consumed_source_keywords(self):
        receipt = {"published": True, "status": "published", "url": "https://blog.naver.com/owner/123456789"}
        with tempfile.TemporaryDirectory() as folder:
            history = TopicHistory(Path(folder) / "history.json")
            self.assertEqual(history.import_legacy([{"topic": "매장 재고 확인법", "source_topic": "하이마트",
                "keywords": ["하이마트 재고"], "title": "하이마트 재고는 어떻게 확인할까요?", "publication": receipt}]), 1)
            self.assertEqual(history.filter_keywords(["하이마트", "하이마트 재고", "미사용 검색어"]), ["미사용 검색어"])
            self.assertFalse(history.record_publication("매장 재고 확인법", receipt, keywords=["하이마트 지점"]))
            self.assertEqual(history.filter_keywords(["하이마트 지점"]), [])
            self.assertEqual(len(history.published_topics()), 1)
            self.assertEqual(history.recent_publications()[0]["title"], "하이마트 재고는 어떻게 확인할까요?")

    def test_permission_retry_keeps_complete_old_file_until_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            atomic_json_write(path, {"value": "before"})
            replace = os.replace
            attempts = []
            def guarded(source, destination):
                attempts.append(source)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": "before"})
                if len(attempts) < 3:
                    raise PermissionError("temporarily held by scanner")
                return replace(source, destination)
            with patch("blog_preferences.os.replace", side_effect=guarded), patch("blog_preferences.time.sleep"):
                atomic_json_write(path, {"value": "after"})
            self.assertEqual(len(attempts), 3)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": "after"})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_failed_write_leaves_old_document_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            atomic_json_write(path, {"saved_prompt": "원래 지침"})
            with patch("blog_preferences.os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
                atomic_json_write(path, {"saved_prompt": "편집한 지침"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["saved_prompt"], "원래 지침")
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_concurrent_keyword_transactions_keep_all_observations_and_consumption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keywords.json"
            update_database(path, observed=["published"])
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(update_database, path, observed=[f"query-{i}"], consumed=["published"])
                           for i in range(24)]
                for future in futures:
                    future.result()
            self.assertEqual(set(words(load_database(path))), {f"query-{i}" for i in range(24)})

    def test_stale_ui_records_cannot_restore_consumed_topic_or_drop_disk_observation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app_module, "DB_FILE", Path(folder) / "keywords.json"):
            path = app_module.DB_FILE
            old = merge({}, ["already published", "old observation"])
            update_database(path, observed=words(old))
            update_database(path, observed=["new disk observation"], consumed=["already published"])
            app = object.__new__(app_module.PictureCleanerApp)
            app.keyword_db_records, app.keyword_db = old, words(old)
            app.topic_history = SimpleNamespace(filter_keywords=lambda values: [v for v in values if v != "already published"])
            app._update_keyword_queue(observed=["already published", "new UI observation"])
            self.assertEqual(set(app.keyword_db), {"old observation", "new disk observation", "new UI observation"})

    def test_corrupt_optional_settings_do_not_prevent_restart(self):
        result = normalize_preferences({"prompts": None, "order": [{}, "claude", "chatgpt", "antigravity"],
            "step_count": float("inf"), "duplicate_keyword_threshold": float("nan"),
            "duplicate_title_threshold": float("inf"), "publication_mode": {}, "review_mode": []}, "default")
        self.assertEqual(result["prompts"][0]["text"], "default")
        self.assertEqual(result["step_count"], 4)
        self.assertEqual(result["duplicate_keyword_threshold"], .4)
        self.assertEqual(result["duplicate_title_threshold"], .5)
        self.assertEqual(result["review_mode"], "단계별 교차 검수")


class ApplicationLifecycleTests(unittest.TestCase):
    def test_duplicate_process_is_rejected_and_lock_releases_on_exit(self):
        code = ("import sys; from blog_runtime import application_instance_lock, ApplicationAlreadyRunning\n"
                "try:\n with application_instance_lock(sys.argv[1]): print('acquired')\n"
                "except ApplicationAlreadyRunning: print('blocked')\n")
        def child(folder):
            return subprocess.run([sys.executable, "-c", code, folder], capture_output=True, text=True,
                cwd=str(Path(__file__).parent), timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True).stdout.strip()
        with tempfile.TemporaryDirectory() as folder:
            with application_instance_lock(folder):
                self.assertEqual(child(folder), "blocked")
            self.assertEqual(child(folder), "acquired")

    def test_second_main_never_constructs_automation(self):
        with tempfile.TemporaryDirectory() as folder, application_instance_lock(folder), \
                patch.object(app_module, "APP_DIR", Path(folder)), patch.object(app_module, "Tk") as root, \
                patch.object(app_module, "PictureCleanerApp") as application, \
                patch.object(app_module, "wait_for_restart_parent"), patch.object(app_module.messagebox, "showinfo"):
            app_module.main()
            application.assert_not_called()
            root.return_value.destroy.assert_called_once()

    def make_closing_app(self):
        app = object.__new__(app_module.PictureCleanerApp)
        app._closing = False
        app.settings = {}
        app.cli_preferences = {"prompts": [{"id": "selected", "text": "saved"}]}
        app.cli_active_prompt = "selected"
        app.base_text = MagicMock()
        app.base_text.get.return_value = "edited"
        app._general_settings_snapshot = MagicMock(return_value={})
        app._save_cli_selection, app._cancel_automatic_resume = MagicMock(), MagicMock()
        app.root, app.status, app._naver_log = MagicMock(), MagicMock(), MagicMock()
        app.full_auto_stop, app.naver_bot = threading.Event(), MagicMock()
        app.naver_task_active = True
        return app

    def test_close_stops_first_and_waits_for_worker_before_browser_close(self):
        app = self.make_closing_app()
        app._save_cli_selection.side_effect = lambda: self.assertTrue(app.full_auto_stop.is_set())
        with patch.object(app_module, "save_json"):
            app.close()
        app.naver_bot.stop.assert_called_once()
        app.naver_bot.close.assert_not_called()
        app.root.destroy.assert_not_called()
        app.naver_task_active = False
        app.root.after.call_args.args[1]()
        app.naver_bot.close.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_save_failure_still_stops_work_and_keeps_editable_window(self):
        app = self.make_closing_app()
        app._save_cli_selection.side_effect = OSError("disk full")
        app.close()
        self.assertTrue(app.full_auto_stop.is_set())
        self.assertFalse(app._closing)
        app.naver_bot.stop.assert_called_once()
        app.root.destroy.assert_not_called()

    def test_stopped_comment_worker_does_not_start_browser_action(self):
        app = self.make_closing_app()
        app.naver_task_active = False
        app.naver_bot.stop_event = threading.Event()
        app.naver_bot.reset_stop.side_effect = app.naver_bot.stop_event.clear
        target = MagicMock()
        with patch.object(app_module.threading, "Thread") as thread:
            app._start_naver_task("comments", target)
            app.naver_bot.stop_event.set()
            thread.call_args.kwargs["target"]()
        target.assert_not_called()
        self.assertFalse(app.naver_task_active)

    def test_comment_worker_launch_failure_does_not_leave_application_busy(self):
        app = self.make_closing_app()
        app.naver_task_active = False
        with patch.object(app_module.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("cannot start thread")
            with self.assertRaises(RuntimeError):
                app._start_naver_task("comments", MagicMock())
        self.assertFalse(app.naver_task_active)


class ProgressAndGeneralSettingsTests(unittest.TestCase):
    def test_window_resize_restores_saved_log_height(self):
        panel = object.__new__(ProgressPanel)
        panel.app = SimpleNamespace(settings={"progress_pane_height": 180}, root=MagicMock())
        panel.split = MagicMock()
        panel.split.panes.return_value = ("notebook", "log")
        panel.split.winfo_height.return_value = 900
        panel.collapsed, panel.restored = False, True
        panel._position_job, panel._last_split_height = None, 560
        panel._resized(SimpleNamespace(height=900))
        panel.app.root.after_idle.call_args.args[0]()
        panel.split.sashpos.assert_called_once_with(0, 720)
        self.assertEqual(panel.app.settings["progress_pane_height"], 180)

    def test_delayed_position_callback_is_safe_after_collapse(self):
        panel = object.__new__(ProgressPanel)
        panel.collapsed, panel.split = True, MagicMock()
        panel._position()
        panel.split.sashpos.assert_not_called()

    def test_general_options_autosave_and_comment_days_restore(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            directory = Path(folder)
            for name, value in (("APP_DIR", directory), ("CONFIG_FILE", directory / "settings.json"),
                                ("DB_FILE", directory / "keywords.json"), ("AUTO_HISTORY_FILE", directory / "automation-history.json")):
                stack.enter_context(patch.object(app_module, name, value))
            stack.enter_context(patch.object(app_module.PictureCleanerApp, "run_realtime"))
            stack.enter_context(patch.object(app_module.PictureCleanerApp, "_maximize_window"))
            root = Tk()
            root.withdraw()
            try:
                app = app_module.PictureCleanerApp(root)
                app.comment_days.set("23")
                app.neighbor_interval.set("105")
                app.dark_mode.set(True)
                self.assertIsNotNone(app._general_settings_job)
                app._autosave_general_settings()
                saved = json.loads(app_module.CONFIG_FILE.read_text(encoding="utf-8"))
                self.assertEqual(saved["comment_days"], "23")
                self.assertEqual(saved["neighbor_interval"], "105")
                self.assertTrue(saved["dark_mode"])
                app.neighbor_interval.set("")
                self.assertEqual(app._general_settings_snapshot()["neighbor_interval"], "105")
            finally:
                root.destroy()
            restored_root = Tk()
            restored_root.withdraw()
            try:
                restored = app_module.PictureCleanerApp(restored_root)
                self.assertEqual(restored.comment_days.get(), "23")
                self.assertTrue(restored.dark_mode.get())
            finally:
                restored_root.destroy()


if __name__ == "__main__":
    unittest.main()
