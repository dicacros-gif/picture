from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blog_preferences import automation_config_snapshot, normalize_preferences
from keyword_database import consume, load_database, merge, reconcile, save_database, words
import picture_cleaner_pc as app_module


class RuntimeSettingsTests(unittest.TestCase):
    def preferences(self, text="이전 원고 지침"):
        return normalize_preferences({
            "prompts": [{"id": "chosen", "name": "선택 프롬프트", "text": text}],
            "selected_prompt_id": "chosen", "step_count": 2,
            "order": ["chatgpt", "claude", "antigravity", "chatgpt"],
            "models": {"chatgpt": "saved-default"},
            "stages": [{"provider": "chatgpt", "role": "작성", "model": "saved-stage"},
                       {"provider": "claude", "role": "문체 다듬기", "model": "saved-style"}],
            "publication_mode": "임시저장까지만", "image_retry_limit": 3,
            "editorial_mode": "strict",
        }, "기본 프롬프트")

    def test_snapshot_uses_selected_prompt_roles_models_and_policy_without_mutation(self):
        settings = {"cli_workflow": self.preferences(), "auto_interval_hours": "5", "blog_id": " owner "}
        fallback = {"base_prompt": "시작 때 지침", "publish": True, "custom_flag": "preserved"}
        original = copy.deepcopy(settings)
        snapshot = automation_config_snapshot(settings, fallback)
        self.assertEqual(snapshot["base_prompt"], "이전 원고 지침")
        self.assertEqual(snapshot["prompt_id"], "chosen")
        self.assertEqual(snapshot["steps"], ["chatgpt", "claude"])
        self.assertEqual(snapshot["stage_configs"][1]["role"], "문체 다듬기")
        self.assertEqual(snapshot["stage_configs"][0]["model"], "saved-stage")
        self.assertEqual(snapshot["models"]["chatgpt"], "saved-default")
        self.assertEqual(snapshot["interval_seconds"], 18000)
        self.assertEqual(snapshot["blog_id"], "owner")
        self.assertFalse(snapshot["publish"])
        self.assertTrue(snapshot["save_draft"])
        self.assertEqual(snapshot["image_retry_limit"], 3)
        self.assertEqual(snapshot["editorial_mode"], "strict")
        self.assertEqual(snapshot["custom_flag"], "preserved")
        snapshot["stage_configs"][0]["model"] = "changed-later"
        self.assertEqual(settings, original)
        self.assertTrue(fallback["publish"])

    def test_missing_saved_preferences_preserve_existing_configuration(self):
        fallback = {"base_prompt": "기존 지침", "steps": ["claude"], "interval_hours": 4}
        snapshot = automation_config_snapshot(None, fallback)
        self.assertEqual(snapshot["base_prompt"], "기존 지침")
        self.assertEqual(snapshot["steps"], ["claude"])
        self.assertEqual(snapshot["interval_hours"], 4)
        self.assertEqual(snapshot["image_retry_limit"], 2)
        self.assertEqual(snapshot["editorial_mode"], "natural")

    def test_image_retry_limits_and_editorial_defaults(self):
        for accepted in (1, 2, 3, "1", "3"):
            with self.subTest(accepted=accepted):
                result = normalize_preferences({"image_retry_limit": accepted}, "prompt")
                self.assertEqual(result["image_retry_limit"], int(accepted))
        for invalid in (0, "0", -1, 4, "", None, True, 1.5):
            with self.subTest(invalid=invalid):
                result = normalize_preferences({"image_retry_limit": invalid, "editorial_mode": "unknown"}, "prompt")
                self.assertEqual(result["image_retry_limit"], 2)
                self.assertEqual(result["editorial_mode"], "natural")

    def test_marked_title_policy_is_upgraded_in_saved_prompt(self):
        old = ("직접 작성한 앞 지침\n\n[제목 통합 작성 규칙 · 2026-09-13]\n"
               "40~65자를 권장하고 짧아도 허용합니다.\n\n"
               "[이미지 문구 최신 규칙 · 2026-09-13]\n이미지 지침")
        result = normalize_preferences({"prompts": [{"id": "saved", "name": "저장값", "text": old}],
                                        "selected_prompt_id": "saved"}, "기본")
        text = result["prompts"][0]["text"]
        self.assertIn("[제목 통합 작성 규칙 · 2026-09-13 v2]", text)
        self.assertIn("45~68자", text)
        self.assertNotIn("40~65자를 권장", text)
        self.assertIn("직접 작성한 앞 지침", text)
        self.assertIn("[이미지 문구 최신 규칙 · 2026-09-13]", text)

    def test_next_cycle_uses_new_settings_and_recalculates_changed_wait(self):
        for first_hours, changed_hours in ((6, 1), (1, 6)):
            with self.subTest(first_hours=first_hours, changed_hours=changed_hours):
                clock = [0.0]
                app = object.__new__(app_module.PictureCleanerApp)
                app.settings = {"cli_workflow": self.preferences(), "auto_interval_hours": str(first_hours)}
                app.events, app.naver_bot, app._naver_log = queue.Queue(), MagicMock(), MagicMock()
                app.full_auto_active = app.naver_task_active = True
                calls = []

                class Stop:
                    stopped = False
                    waits = []

                    def is_set(self): return self.stopped
                    def set(self): self.stopped = True
                    def wait(self, duration):
                        self.waits.append(duration)
                        if len(self.waits) == 1:
                            app.settings["auto_interval_hours"] = str(changed_hours)
                            app.settings["cli_workflow"]["prompts"][0]["text"] = "다음 글에 쓸 지침"
                            app.settings["cli_workflow"]["stages"][0]["model"] = "next-model"
                            clock[0] = changed_hours * 3600 - 1
                        else:
                            clock[0] += duration
                        return self.stopped

                app.full_auto_stop = Stop()
                def cycle(config, budget=None):
                    calls.append((clock[0], copy.deepcopy(config)))
                    if len(calls) == 1:
                        clock[0] = 30
                    else:
                        app.full_auto_stop.set()
                app._run_full_automation_cycle = cycle
                with patch.object(app_module.time, "monotonic", side_effect=lambda: clock[0]):
                    app._full_automation_loop({})
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0][1]["base_prompt"], "이전 원고 지침")
                self.assertEqual(calls[1][1]["base_prompt"], "다음 글에 쓸 지침")
                self.assertEqual(calls[1][1]["stage_configs"][0]["model"], "next-model")
                self.assertEqual(calls[1][0], changed_hours * 3600)
                self.assertTrue(all(0 < duration <= 1 for duration in app.full_auto_stop.waits))

    def test_stop_during_wait_prevents_another_cycle(self):
        app = object.__new__(app_module.PictureCleanerApp)
        app.settings, app.events = {}, queue.Queue()
        app.naver_bot, app._naver_log = MagicMock(), MagicMock()
        app.full_auto_active = app.naver_task_active = True
        app.full_auto_stop = MagicMock()
        stopped = [False]
        app.full_auto_stop.is_set.side_effect = lambda: stopped[0]
        def stop(duration):
            stopped[0] = True
            self.assertLessEqual(duration, 1)
            return True
        app.full_auto_stop.wait.side_effect = stop
        app._run_full_automation_cycle = MagicMock()
        with patch.object(app_module.time, "monotonic", return_value=0):
            app._full_automation_loop({"interval_hours": 6})
        app._run_full_automation_cycle.assert_called_once()
        app.naver_bot.reset_stop.assert_not_called()


class KeywordRetentionTests(unittest.TestCase):
    def test_adding_and_consuming_words_preserves_unobserved_timestamps(self):
        first = datetime(2026, 8, 20, tzinfo=timezone.utc)
        later = first + timedelta(days=10)
        records = merge({}, ["보관 키워드", "발행 키워드"], now=first)
        original = copy.deepcopy(records)
        records = merge(reconcile(records, words(records), now=later), ["새 키워드"], now=later)
        records = consume(records, ["발행 키워드"])
        self.assertEqual(records["보관키워드"], original["보관키워드"])
        self.assertNotIn("발행키워드", records)
        refreshed = merge(records, ["보관 키워드"], now=later)
        self.assertEqual(refreshed["보관키워드"]["first_seen"], first.isoformat())
        self.assertEqual(refreshed["보관키워드"]["last_seen"], later.isoformat())

    def test_save_prunes_expired_records_while_application_stays_open(self):
        first = datetime(2026, 8, 1, tzinfo=timezone.utc)
        later = first + timedelta(days=31)
        records = merge({}, ["만료 키워드"], now=first)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keywords.json"
            kept = save_database(path, records, now=later)
            self.assertEqual(kept, {})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["keywords"], [])
            self.assertEqual(load_database(path, now=later), {})

    def test_queue_cap_prefers_latest_observation_over_insertion_order(self):
        first = datetime(2026, 9, 1, tzinfo=timezone.utc)
        records = merge({}, ["A", "B"], now=first, limit=2)
        records = merge(records, ["A"], now=first + timedelta(days=1), limit=2)
        records = merge(records, ["C"], now=first + timedelta(days=2), limit=2)
        self.assertEqual(words(records), ["A", "C"])


if __name__ == "__main__":
    unittest.main()
