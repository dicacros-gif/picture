"""Tk controls for the CLI-only article workflow."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, StringVar, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from blog_cli_bridge import BlogCliBridge
from blog_preferences import (PROVIDER_LABELS, DEFAULT_BLOCKED_TERMS, STAGE_ROLES, normalize_preferences,
                              store_prompt, normalize_blocked_terms, blocked_term_hits, atomic_json_write, save_settings_json)
from blog_workflow import BlogWorkflow, REVIEW_MODES, WorkflowError, _related_to_topic
from blog_runtime import UnattendedControls, account_problem, access_error_from_exception
from blog_topic_history import TopicHistory, TopicHistoryError, _confirmed as confirmed_publication


def next_cycle_tick(previous_tick: float, now: float, interval: float) -> float:
    """Keep a fixed cadence without overlapping runs or catching up missed slots."""
    if interval <= 0:
        raise ValueError("반복 간격은 양수여야 합니다.")
    candidate = previous_tick + interval
    if candidate <= now:
        candidate += (int((now - candidate) // interval) + 1) * interval
    return candidate


def _google_search_context_hash(topic, keywords, config):
    """Invalidate the optional-search receipt when its inputs or policy change."""
    context = {"version": 1, "topic": topic, "keywords": keywords,
               "count": config.get("google_reference_count", 4),
               "steps": config.get("steps"), "models": config.get("models"),
               "stage_configs": config.get("stage_configs"),
               "reuse_only": True, "english_only": True}
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class BlogWorkflowControls(UnattendedControls):
    def _browser_task_busy(self):
        return any(getattr(self, name, False) for name in
                   ("naver_task_active", "full_auto_active", "realtime_task_active", "cli_login_active"))

    def _discard_topic_review(self, value):
        if not value:
            return
        path = Path(value).resolve()
        root = (self.cli_app_dir / "blog-runs").resolve()
        if root in path.parents and path.name.startswith("topic-review-") and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    def _init_cli_controls(self, config_path, app_dir, default_prompt):
        self.cli_config_path, self.cli_app_dir = Path(config_path), Path(app_dir)
        self.cli_preferences = normalize_preferences(
            self.settings.get("cli_workflow"), default_prompt, self.settings.get("blog_prompt", "")
        )
        pref = self.cli_preferences
        self._init_unattended_controls(pref)
        if pref.get("default_revision") != "user-20260912-thumbnail-v3":
            if self.settings.get("cli_workflow"):
                supplied = {"id": "user-default-20260912-thumbnail-v3", "name": "사용자 기본 프롬프트 · 1:1 한글 썸네일", "text": default_prompt.strip()}
                for field in ("id", "name"):
                    original, suffix = supplied[field], 2
                    while any(p[field] == supplied[field] for p in pref["prompts"]):
                        supplied[field] = f"{original} ({suffix})"
                        suffix += 1
                pref["prompts"] = [supplied, *pref["prompts"]]
            pref["default_revision"] = "user-20260912-thumbnail-v3"
        self.cli_active_prompt = pref["selected_prompt_id"]
        selected = next(p for p in pref["prompts"] if p["id"] == self.cli_active_prompt)
        self.cli_preset_choice = StringVar(value=selected["name"])
        self.cli_preset_name = StringVar(value=selected["name"])
        self.cli_step_count = StringVar(value=str(pref["step_count"]))
        self.cli_order = [StringVar(value=PROVIDER_LABELS[p]) for p in pref["order"]]
        self.cli_roles = [StringVar(value=s["role"]) for s in pref["stages"]]
        self.cli_stage_models = [StringVar(value=s["model"]) for s in pref["stages"]]
        self.cli_review_mode = StringVar(value=pref["review_mode"] if pref["review_mode"] in REVIEW_MODES else REVIEW_MODES[0])
        self.cli_models = {key: StringVar(value=model) for key, model in pref["models"].items()}
        self.cli_google = BooleanVar(value=pref["include_google"])
        self.cli_image_retries = StringVar(value=str(pref.get("image_retry_limit", 2)))
        self.cli_google_count = StringVar(value=str(pref.get("google_reference_count", 4)))
        self.cli_editorial_mode = StringVar(value="자연스러운 구성" if pref.get("editorial_mode", "natural") == "natural" else "구역별 분량·밀도")
        self.cli_publication = StringVar(value=pref["publication_mode"])
        self.cli_duplicate_keywords = StringVar(value=str(pref["duplicate_keyword_threshold"]))
        self.cli_duplicate_titles = StringVar(value=str(pref["duplicate_title_threshold"]))
        self.cli_capability_text = StringVar(value="CLI 연결 확인을 눌러 설치·로그인 상태를 확인하세요.")
        self.cli_article = None
        self.cli_runtime_selectors = []
        self.cli_bridge = BlogCliBridge(self.cli_app_dir, self._naver_log, self.full_auto_stop)
        self.topic_history = TopicHistory(self.cli_app_dir / "published-topic-history.json")
        self.topic_history.import_legacy(self.auto_history)
        self.keyword_db = self.topic_history.filter_keywords(self.keyword_db)
        self._cleanup_stale_artifacts()

    def _cleanup_stale_artifacts(self):
        cutoff = datetime.now().timestamp() - timedelta(days=7).total_seconds()
        protected = set()
        pending_path = self.cli_app_dir / "pending-blog-topic.json"
        if pending_path.exists():
            try:
                pending = json.loads(pending_path.read_text(encoding="utf-8"))
                prepared = pending.get("prepared_article", {})
                protected = {Path(p).resolve() for p in (pending.get("resume_run_dir"), pending.get("run_dir"),
                    pending.get("choice", {}).get("selection_run_dir"), *prepared.get("auxiliary_dirs", [])) if p}
                for run_dir in tuple(protected):
                    request_path = run_dir / "request.json"
                    if (self.cli_app_dir.resolve() in run_dir.parents and request_path.is_file()):
                        request = json.loads(request_path.read_text(encoding="utf-8"))
                        protected.update(Path(item["path"]).resolve().parent
                            for item in request.get("google_candidates", []) if isinstance(item, dict) and item.get("path"))
            except (ValueError, OSError, TypeError, AttributeError):
                return  # Preserve work if its pending receipt cannot be read.
        for parent_name in ("blog-runs", "google-reference-candidates"):
            parent = self.cli_app_dir / parent_name
            if not parent.is_dir():
                continue
            for path in parent.iterdir():
                try:
                    resolved = path.resolve()
                    if (parent.resolve() in resolved.parents and not any(resolved == kept or resolved in kept.parents for kept in protected) and path.is_dir()
                            and path.stat().st_mtime < cutoff):
                        shutil.rmtree(path)
                except OSError as exc:
                    self._naver_log(f"7일 경과 산출물 정리 실패 · {path}: {exc}")

    def _cli_blog_ui(self):
        self.blog_tab.columnconfigure(0, weight=1)
        self.blog_tab.rowconfigure(2, weight=1)
        settings = ttk.LabelFrame(self.blog_tab, text="CLI 실행 순서 · 생성과 검수", padding=9)
        settings.grid(row=0, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="주제 입력어").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.topic).grid(row=0, column=1, columnspan=5, sticky="ew", padx=7)
        sequence = ttk.Frame(settings)
        sequence.grid(row=1, column=0, columnspan=7, sticky="ew", pady=6)
        ttk.Label(sequence, text="실행 단계").grid(row=0, column=0)
        count = ttk.Combobox(sequence, textvariable=self.cli_step_count, values=["1", "2", "3", "4"], state="readonly", width=3)
        count.grid(row=0, column=1, sticky="w", padx=5)
        self.cli_order_boxes = []
        self.cli_role_boxes, self.cli_stage_model_boxes = [], []
        for index, variable in enumerate(self.cli_order):
            row, column = 1 + index // 2, (index % 2) * 4
            ttk.Label(sequence, text=f"{index + 1}.").grid(row=row, column=column, padx=(6, 2))
            box = ttk.Combobox(sequence, textvariable=variable, values=list(PROVIDER_LABELS.values()), state="readonly", width=21)
            box.grid(row=row, column=column + 1, pady=2)
            box.bind("<<ComboboxSelected>>", self._save_cli_selection)
            self.cli_order_boxes.append(box)
            role = ttk.Combobox(sequence, textvariable=self.cli_roles[index], values=STAGE_ROLES, state="readonly", width=20)
            role.grid(row=row, column=column + 2, padx=4)
            role.bind("<<ComboboxSelected>>", self._save_cli_selection)
            self.cli_role_boxes.append(role)
            model = ttk.Entry(sequence, textvariable=self.cli_stage_models[index], width=16)
            model.grid(row=row, column=column + 3)
            model.bind("<KeyRelease>", self._schedule_prompt_save)
            model.bind("<FocusOut>", self._save_cli_selection)
            self.cli_stage_model_boxes.append(model)
        ttk.Label(sequence, text="단계별 모델: 비우면 아래 CLI 기본 모델 사용").grid(row=0, column=2, columnspan=6, sticky="w")
        count.bind("<<ComboboxSelected>>", self._save_cli_selection)
        options = ttk.Frame(settings)
        options.grid(row=2, column=0, columnspan=7, sticky="ew")
        ttk.Label(options, text="검수 방식").pack(side="left")
        review = ttk.Combobox(options, textvariable=self.cli_review_mode, values=REVIEW_MODES, state="readonly", width=24)
        review.pack(side="left", padx=5)
        review.bind("<<ComboboxSelected>>", self._save_cli_selection)
        ttk.Checkbutton(options, text="Google 캡처도 활용", variable=self.cli_google, command=self._save_cli_selection).pack(side="left", padx=8)
        ttk.Button(options, text="CLI 연결 확인", command=self.check_blog_cli).pack(side="left", padx=8)
        ttk.Button(options, text="CLI 로그인 안내", command=self.show_cli_login_help).pack(side="left")
        ttk.Button(options, text="프로그램 다시 시작", command=self.restart_program).pack(side="left", padx=5)
        models = ttk.Frame(settings)
        models.grid(row=3, column=0, columnspan=7, sticky="ew", pady=(6, 0))
        for key, label in PROVIDER_LABELS.items():
            ttk.Label(models, text=label.replace(" CLI", "") + " 모델").pack(side="left", padx=(5, 3))
            entry = ttk.Entry(models, textvariable=self.cli_models[key], width=17)
            entry.pack(side="left")
            entry.bind("<FocusOut>", self._save_cli_selection)
            entry.bind("<KeyRelease>", self._schedule_prompt_save)
        ttk.Label(models, text="비우면 CLI 기본값", style="Sub.TLabel").pack(side="left", padx=7)
        ttk.Label(settings, textvariable=self.cli_capability_text, wraplength=1100, style="Sub.TLabel").grid(row=4, column=0, columnspan=7, sticky="w", pady=(6, 0))
        blocked = ttk.Frame(settings)
        blocked.grid(row=5, column=0, columnspan=7, sticky="ew", pady=(7, 0))
        ttk.Label(blocked, text="스포츠·사망 차단어\n쉼표 또는 줄바꿈으로 구분").pack(side="left", padx=(0, 8))
        self.cli_blocked_terms = ScrolledText(blocked, height=3, wrap="word", font=("맑은 고딕", 9))
        self.cli_blocked_terms.pack(side="left", fill="x", expand=True)
        self.cli_blocked_terms.insert("1.0", ", ".join(self.cli_preferences["blocked_terms"]))
        self.cli_blocked_terms.bind("<FocusOut>", self._save_cli_selection)
        self.cli_blocked_terms.bind("<KeyRelease>", self._schedule_prompt_save)
        ttk.Button(blocked, text="차단어 저장", command=self._save_cli_selection).pack(side="left", padx=5)
        ttk.Button(blocked, text="기본값 복원", command=self._restore_blocked_terms).pack(side="left")
        ttk.Label(blocked, text=" 연관어 중복").pack(side="left")
        duplicate_keywords = ttk.Entry(blocked, textvariable=self.cli_duplicate_keywords, width=5)
        duplicate_keywords.pack(side="left")
        ttk.Label(blocked, text=" 제목 유사도").pack(side="left")
        duplicate_titles = ttk.Entry(blocked, textvariable=self.cli_duplicate_titles, width=5)
        duplicate_titles.pack(side="left")
        duplicate_keywords.bind("<FocusOut>", self._save_cli_selection)
        duplicate_titles.bind("<FocusOut>", self._save_cli_selection)
        policy_row = ttk.Frame(settings)
        policy_row.grid(row=6, column=0, columnspan=7, sticky="ew", pady=(6, 0))
        ttk.Label(policy_row, text="글 구성").pack(side="left")
        for variable, values, width in ((self.cli_editorial_mode, ["자연스러운 구성", "구역별 분량·밀도"], 19),
                                       (self.cli_image_retries, ["1", "2", "3"], 3),
                                       (self.cli_google_count, [str(i) for i in range(1, 11)], 3)):
            if variable is self.cli_image_retries:
                ttk.Label(policy_row, text=" 이미지별 재생성").pack(side="left", padx=(12, 2))
            elif variable is self.cli_google_count:
                ttk.Label(policy_row, text="회 · Google 캡처").pack(side="left", padx=(5, 2))
            selector = ttk.Combobox(policy_row, textvariable=variable, values=values, width=width, state="readonly")
            selector.pack(side="left", padx=3)
            selector.bind("<<ComboboxSelected>>", self._save_cli_selection)
        ttk.Label(policy_row, text="장 · 설정 변경은 다음 새 글부터", style="Sub.TLabel").pack(side="left", padx=5)
        ttk.Label(self.blog_tab, text="검색 의도 → 제목·8구역 → CLI 교차 검수 → 생성 이미지 + 한글 설명을 넣은 Google 캡처", style="Sub.TLabel").grid(row=1, column=0, sticky="w", pady=7)
        panes = ttk.Panedwindow(self.blog_tab, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew")
        left, right = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=1)
        preset_row = ttk.Frame(left)
        preset_row.pack(fill="x")
        ttk.Label(preset_row, text="저장된 프롬프트").pack(side="left")
        self.cli_preset_box = ttk.Combobox(preset_row, textvariable=self.cli_preset_choice, values=[p["name"] for p in self.cli_preferences["prompts"]], state="readonly", width=23)
        self.cli_preset_box.pack(side="left", fill="x", expand=True, padx=5)
        self.cli_preset_box.bind("<<ComboboxSelected>>", self.select_blog_preset)
        edit_row = ttk.Frame(left)
        edit_row.pack(fill="x", pady=5)
        ttk.Entry(edit_row, textvariable=self.cli_preset_name, width=20).pack(side="left", fill="x", expand=True)
        ttk.Button(edit_row, text="저장", command=self.save_blog_prompt).pack(side="left", padx=3)
        ttk.Button(edit_row, text="새 이름으로 저장", command=lambda: self.save_cli_prompt(create=True)).pack(side="left")
        ttk.Button(edit_row, text="삭제", command=self.delete_cli_prompt).pack(side="left", padx=3)
        self.base_text = ScrolledText(left, wrap="word", font=("맑은 고딕", 10), height=12)
        self.base_text.pack(fill="both", expand=True, padx=(0, 5))
        self.base_text.insert("1.0", next(p["text"] for p in self.cli_preferences["prompts"] if p["id"] == self.cli_active_prompt))
        self.base_text.bind("<KeyRelease>", self._schedule_prompt_save)
        self.base_text.edit_modified(False)
        self.base_text.bind("<<Modified>>", self._prompt_modified)
        self.cli_preset_name.trace_add("write", lambda *_: self._schedule_prompt_save())
        ttk.Label(right, text="검수된 글 · 실행 기록").pack(anchor="w")
        self.blog_result = ScrolledText(right, wrap="word", font=("맑은 고딕", 10), height=12)
        self.blog_result.pack(fill="both", expand=True, padx=(5, 0))
        self.cli_log = ScrolledText(right, wrap="word", font=("맑은 고딕", 9), height=5, state="disabled")
        self.cli_log.pack(fill="x", padx=(5, 0), pady=(5, 0))
        actions = ttk.Frame(self.blog_tab)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="실행 자료 폴더", command=self.open_cli_artifacts).pack(side="left")
        ttk.Button(actions, text="결과 복사", command=lambda: self.copy_widget(self.blog_result)).pack(side="left", padx=6)
        ttk.Button(actions, text="웨일 네이버 로그인", command=self.open_naver_login).pack(side="left")
        self._sync_cli_step_boxes()

    def _sync_cli_step_boxes(self):
        count = int(self.cli_step_count.get())
        for index, box in enumerate(getattr(self, "cli_order_boxes", [])):
            box.configure(state="readonly" if index < count else "disabled")
        for index, box in enumerate(getattr(self, "cli_role_boxes", [])):
            box.configure(state="readonly" if index < count else "disabled")
        for index, box in enumerate(getattr(self, "cli_stage_model_boxes", [])):
            box.configure(state="normal" if index < count else "disabled")

    def _prompt_modified(self, _event=None):
        if self.base_text.edit_modified():
            self.base_text.edit_modified(False)
            self._schedule_prompt_save()

    def _save_cli_selection(self, _event=None):
        reverse = {label: key for key, label in PROVIDER_LABELS.items()}
        def threshold(variable, key, default):
            try:
                value = float(variable.get())
                if not math.isfinite(value) or not .1 <= value <= 1:
                    raise ValueError
                return value
            except (ValueError, TypeError):
                return self.cli_preferences.get(key, default)
        self.cli_preferences.update(
            order=[reverse[value.get()] for value in self.cli_order],
            stages=[{"provider": reverse[value.get()], "role": self.cli_roles[index].get(),
                     "model": self.cli_stage_models[index].get().strip()} for index, value in enumerate(self.cli_order)],
            step_count=int(self.cli_step_count.get()), review_mode=self.cli_review_mode.get(),
            models={key: value.get().strip() for key, value in self.cli_models.items()},
            include_google=self.cli_google.get(), publication_mode=self.cli_publication.get(),
            image_retry_limit=int(self.cli_image_retries.get()),
            google_reference_count=int(self.cli_google_count.get()),
            editorial_mode="natural" if self.cli_editorial_mode.get() == "자연스러운 구성" else "strict",
            auto_start_on_launch=self.auto_start_on_launch.get(),
            blocked_terms=normalize_blocked_terms(self.cli_blocked_terms.get("1.0", "end")),
            duplicate_keyword_threshold=threshold(self.cli_duplicate_keywords, "duplicate_keyword_threshold", .4),
            duplicate_title_threshold=threshold(self.cli_duplicate_titles, "duplicate_title_threshold", .5),
        )
        self._sync_cli_step_boxes()
        self._persist_cli_preferences()

    def _schedule_prompt_save(self, _event=None):
        pending = getattr(self, "_prompt_save_job", None)
        if pending:
            self.root.after_cancel(pending)
        self._prompt_save_job = self.root.after(700, self._autosave_prompt)

    def _autosave_prompt(self):
        self._prompt_save_job = None
        self.save_cli_prompt(silent=True)

    def _restore_blocked_terms(self):
        self.cli_blocked_terms.delete("1.0", "end")
        self.cli_blocked_terms.insert("1.0", ", ".join(DEFAULT_BLOCKED_TERMS))
        self._save_cli_selection()

    @staticmethod
    def _ensure_topic_allowed(topic, keywords, config):
        hits = blocked_term_hits([topic, keywords], config.get("blocked_terms"))
        if hits:
            raise WorkflowError(f"'{topic}' 차단 · 주제 또는 연관어에 차단어 포함: {', '.join(hits)}")

    def _persist_cli_preferences(self):
        if hasattr(self, "_general_settings_snapshot"):
            self.settings.update(self._general_settings_snapshot())
        self.settings["cli_workflow"] = copy.deepcopy(self.cli_preferences)
        self.settings["auto_interval_hours"] = self.auto_interval_hours.get()
        self.settings["blog_id"] = self.blog_id.get().strip()
        save_settings_json(self.cli_config_path, self.settings)

    def save_cli_prompt(self, *, create=False, silent=False):
        try:
            self.cli_preferences = store_prompt(self.cli_preferences, self.cli_active_prompt,
                self.cli_preset_name.get(), self.base_text.get("1.0", "end"), create=create)
            self.cli_active_prompt = self.cli_preferences["selected_prompt_id"]
            self.cli_preset_choice.set(self.cli_preset_name.get().strip())
            self.cli_preset_box.configure(values=[p["name"] for p in self.cli_preferences["prompts"]])
            self._save_cli_selection()
            self.status.set("프롬프트와 CLI 실행 설정을 저장했습니다.")
        except (ValueError, OSError) as exc:
            if silent:
                self.status.set(str(exc))
                self._naver_log(str(exc))
            else:
                messagebox.showinfo("Blog", str(exc))
            return False
        return True

    def select_blog_preset(self, _event=None):
        name = self.cli_preset_choice.get()
        # Keep the old preset's edited text when switching choices.
        previous = next(p for p in self.cli_preferences["prompts"] if p["id"] == self.cli_active_prompt)
        if self.base_text.get("1.0", "end").strip():
            previous["text"] = self.base_text.get("1.0", "end").strip()
        selected = next(p for p in self.cli_preferences["prompts"] if p["name"] == name)
        self.cli_active_prompt = selected["id"]
        self.cli_preferences["selected_prompt_id"] = selected["id"]
        self.cli_preset_name.set(selected["name"])
        self.base_text.delete("1.0", "end")
        self.base_text.insert("1.0", selected["text"])
        self._save_cli_selection()

    def delete_cli_prompt(self):
        if len(self.cli_preferences["prompts"]) < 2:
            self.status.set("프롬프트는 최소 한 개를 유지합니다.")
            return
        self.cli_preferences["prompts"] = [p for p in self.cli_preferences["prompts"] if p["id"] != self.cli_active_prompt]
        selected = self.cli_preferences["prompts"][0]
        self.cli_active_prompt = selected["id"]
        self.cli_preferences["selected_prompt_id"] = selected["id"]
        self.cli_preset_choice.set(selected["name"])
        self.cli_preset_name.set(selected["name"])
        self.cli_preset_box.configure(values=[p["name"] for p in self.cli_preferences["prompts"]])
        self.base_text.delete("1.0", "end")
        self.base_text.insert("1.0", selected["text"])
        self._save_cli_selection()

    def _cli_configuration(self, *, silent=False):
        if not self.save_cli_prompt(silent=silent):
            raise ValueError("프롬프트를 저장한 뒤 실행하세요.")
        pref = copy.deepcopy(self.cli_preferences)
        selected = next(p for p in pref["prompts"] if p["id"] == pref["selected_prompt_id"])
        return {"steps": pref["order"][:pref["step_count"]], "review_mode": pref["review_mode"],
                "quality_checks": True,
                "image_retry_limit": pref.get("image_retry_limit", 2),
                "google_reference_count": pref.get("google_reference_count", 4),
                "editorial_mode": pref.get("editorial_mode", "natural"),
                "stage_configs": pref["stages"][:pref["step_count"]],
                "models": pref["models"], "base_prompt": selected["text"],
                "include_google": pref["include_google"], "publish": pref["publication_mode"] == "자동 발행",
                "save_draft": pref["publication_mode"] == "임시저장까지만", "completion_label": pref["publication_mode"],
                "blocked_terms": pref["blocked_terms"],
                "duplicate_keyword_threshold": pref["duplicate_keyword_threshold"],
                "duplicate_title_threshold": pref["duplicate_title_threshold"],
                "blog_id": self.blog_id.get().strip(), "prompt_id": selected["id"]}

    def check_blog_cli(self):
        def work():
            try:
                diagnostics = BlogCliBridge(self.cli_app_dir, self._naver_log, threading.Event())
                statuses = diagnostics.check_accounts()
                parts = []
                labels = {"available": "사용 가능", "authenticated": "로그인 확인", "login_verified": "로그인 확인", "authentication_required": "로그인 필요",
                          "not_checked": "실행 시 확인", "account_not_checked": "실행 시 계정 확인", "installed": "설치 확인"}
                for key, label in PROVIDER_LABELS.items():
                    status = statuses.get(key, {})
                    state = status.get("text_status", "상태 미확인") if status.get("installed") else "미설치"
                    parts.append(f"{label}: {labels.get(state, state)}")
                self.events.put(("cli_status", " · ".join(parts)))
                self._naver_log("CLI 설치·연결 상태 확인 완료")
            except Exception as exc:
                self.events.put(("cli_status", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def show_cli_login_help(self):
        self.show_cli_login_required()

    def _start_cli_job(self, label, work):
        if self._browser_task_busy():
            messagebox.showinfo("Blog", "진행 중인 작업을 완료하거나 중지한 뒤 실행하세요.")
            return
        self.naver_task_active = True
        self._set_cli_runtime_controls(True)
        self.full_auto_stop.clear()
        self.naver_bot.reset_stop()
        self.status.set(label)
        def wrapped():
            try:
                work()
            except Exception as exc:
                self._naver_log(f"{label} 실패: {exc}")
                problem = access_error_from_exception(exc)
                self.events.put(("cli_access_required", problem, False) if problem else ("error", f"{label}\n{exc}"))
            finally:
                self.naver_task_active = False
                self.events.put(("cli_idle",))
        threading.Thread(target=wrapped, daemon=True).start()

    def select_cli_topic(self):
        def work():
            groups = self._cli_realtime_groups()
            topic, keywords, related = self._select_longtail_topic(groups)
            self.events.put(("auto_topic", groups, topic, keywords, related))
        self._start_cli_job("관심 주제와 검색 의도 분석", work)

    def _cli_realtime_groups(self):
        # UI can keep the visible groups; automatic cycles request fresh data.
        from picture_cleaner_pc import fetch_realtime_groups
        groups = fetch_realtime_groups()
        if not any(groups.values()):
            raise RuntimeError("실시간 검색어를 가져오지 못했습니다.")
        return self.topic_history.filter_groups(groups, include_pending=True)

    def _set_cli_runtime_controls(self, active):
        for selector in self.cli_runtime_selectors:
            selector.configure(state="disabled" if active else "readonly")

    def _prepare_cli_worker(self, topic, keywords, config):
        self._ensure_topic_allowed(topic, keywords, config)
        self.events.put(("cli_preparing", topic))
        self._preflight_cli_accounts(config)
        workflow = BlogWorkflow(self.cli_bridge, self.cli_app_dir / "blog-runs", self._naver_log, self.full_auto_stop)
        google = []
        google_folder = None
        search_folder = None
        google_search_completed = False
        google_queries = []
        google_context = _google_search_context_hash(topic, keywords, config)
        if config["include_google"] and config.get("resume_run_dir"):
            run_dir = Path(config["resume_run_dir"]).resolve()
            if (self.cli_app_dir / "blog-runs").resolve() in run_dir.parents:
                try:
                    request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
                    for item in request.get("google_candidates", []):
                        if not isinstance(item, dict) or item.get("english_source_verified") is not True:
                            continue
                        path = Path(str(item.get("path", ""))).resolve()
                        if ((self.cli_app_dir / "google-reference-candidates").resolve() in path.parents and path.is_file()
                                and item.get("capture_sha256") == hashlib.sha256(path.read_bytes()).hexdigest()):
                            google.append(item)
                    google = google[:config.get("google_reference_count", 4)]
                    if google:
                        self._naver_log(f"같은 회차의 영어 원문 확인을 마친 Google 후보 {len(google)}장 재사용")
                except (OSError, ValueError, TypeError, AttributeError):
                    google = []
                if not google:
                    try:
                        receipt = json.loads((run_dir / "google-search-checkpoint.json").read_text(encoding="utf-8"))
                        google_search_completed = (receipt.get("version") == 1
                            and receipt.get("run_dir") == str(run_dir)
                            and receipt.get("context_sha256") == google_context
                            and receipt.get("status") == "completed"
                            and type(receipt.get("candidate_count")) is int and receipt["candidate_count"] == 0)
                        if google_search_completed:
                            google_queries = receipt.get("queries", [])[:3]
                            self._naver_log("같은 회차에서 Google 검색을 마쳤으나 사용 가능한 후보가 없어 생성 이미지 작업을 이어갑니다.")
                    except (OSError, ValueError, TypeError, AttributeError):
                        google_search_completed = False
        if config["include_google"] and not google and not google_search_completed:
            self._naver_log(f"Google 캡처 후보 최대 {config.get('google_reference_count', 4)}장의 화면과 사용 조건을 확인합니다.")
            try:
                search = workflow.plan_google_image_search(topic, keywords, config["steps"],
                    config["models"], stage_configs=config.get("stage_configs"))
                search_folder = search.get("run_dir")
                queries = list(dict.fromkeys([search["query"], *search.get("queries", [])]))[:3]
                google_queries = queries
                completed_queries = 0
                folder = self.cli_app_dir / "google-reference-candidates" / (
                    datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
                google_folder = folder
                goal = config.get("google_reference_count", 4)
                seen_sources, seen_files = set(), set()
                for number, query in enumerate(queries, 1):
                    if self.full_auto_stop.is_set():
                        raise WorkflowError("사용자가 작업을 중지했습니다.")
                    self._naver_log(f"Google 영어 이미지 검색 {number}/{len(queries)}: {query}")
                    try:
                        # A later query can start with already captured photos.
                        # Scan a full bounded batch, then retain only the missing unique photos.
                        candidates = self.naver_bot.capture_google_reference_candidates(query, folder / f"query-{number}",
                            count=goal, reuse_only=True, english_only=True)
                        # Returning normally includes a bounded scan with no eligible
                        # photo or unavailable previews. Exceptions remain retryable.
                        completed_queries += 1
                    except Exception as exc:
                        if self.full_auto_stop.is_set():
                            raise
                        self._naver_log(f"Google 검색 {number} 처리 실패 · 다음 검색어 확인: {exc}")
                        continue
                    for item in candidates:
                        if not isinstance(item, dict):
                            continue
                        source_key = item.get("image_url") or item.get("source_url")
                        file_key = item.get("capture_sha256") or item.get("path")
                        if (source_key and source_key in seen_sources) or (file_key and file_key in seen_files):
                            continue
                        google.append(item)
                        if source_key:
                            seen_sources.add(source_key)
                        if file_key:
                            seen_files.add(file_key)
                        if len(google) >= goal:
                            break
                    if len(google) >= goal:
                        break
                    if number < len(queries):
                        self._naver_log(f"Google 후보 {len(google)}/{goal}장 확보 · 같은 주제의 다음 영어 검색어로 보충합니다.")
                google_search_completed = completed_queries == len(queries)
            except Exception as exc:
                if self.full_auto_stop.is_set():
                    raise WorkflowError("사용자가 작업을 중지했습니다.") from exc
                self._naver_log(f"Google 참고 이미지 생략: {exc}")
        resume_options = {"resume_run_dir": config["resume_run_dir"]} if config.get("resume_run_dir") else {}
        resume_options["quality_checks"] = True
        resume_options["quality_topic"] = config.get("quality_topic", topic)
        resume_options["image_retry_limit"] = config.get("image_retry_limit", 2)
        resume_options["editorial_mode"] = config.get("editorial_mode", "natural")
        if config.get("revision_feedback"):
            resume_options["revision_feedback"] = config["revision_feedback"]
        if config.get("stage_configs"):
            resume_options["stage_configs"] = config["stage_configs"]
        def remember_run(run_dir):
            self._remember_preparing_run(topic, run_dir)
            if config["include_google"] and google_search_completed and not google:
                # Store beside the request so a later article/image failure does
                # not repeat this completed optional search on the same run.
                path = Path(run_dir).resolve()
                try:
                    atomic_json_write(path / "google-search-checkpoint.json", {
                        "version": 1, "run_dir": str(path), "context_sha256": google_context,
                        "status": "completed", "candidate_count": 0, "queries": google_queries,
                        "completed_at": datetime.now().isoformat(timespec="seconds")})
                except OSError as exc:
                    self._naver_log(f"Google 검색 완료 기록 저장 실패 · 다음 재개 시 재확인합니다: {exc}")
        resume_options["on_run_created"] = remember_run
        brief = config["base_prompt"]
        if config.get("selection_intent") or config.get("selection_question"):
            brief += "\n확정된 검색 의도(주제를 바꾸지 말고 이 궁금증에 답한다): " + json.dumps({
                "intent": config.get("selection_intent", ""), "question": config.get("selection_question", ""),
                "related_keywords": keywords}, ensure_ascii=False)
        article = workflow.prepare(topic, keywords, brief, config["steps"],
                                   config["review_mode"], models=config["models"], google_candidates=google,
                                   **resume_options)
        article["blog_id"] = config["blog_id"]
        article["source_topic"] = config.get("quality_topic", topic)
        article["auxiliary_dirs"] = list(dict.fromkeys([
            *[str(folder) for folder in (google_folder, search_folder) if folder],
            *[str(Path(item["path"]).resolve().parent) for item in google if item.get("path")]]))
        self.events.put(("cli_article", article))
        return article

    def _remember_preparing_run(self, topic, run_dir):
        """Save the resume pointer before the first slow/charged CLI request."""
        path = Path(run_dir).resolve()
        parent = (self.cli_app_dir / "blog-runs").resolve()
        if parent not in path.parents or not all((path / name).is_file() for name in ("request.json", "manifest.json")):
            raise WorkflowError("회차 재개 위치를 확인하지 못했습니다.", path)
        pending_path = self.cli_app_dir / "pending-blog-topic.json"
        if not pending_path.exists():
            return  # Manual preparation has no automatic-cycle receipt.
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        if (pending.get("choice", {}).get("topic") != topic
                or pending.get("phase") not in {None, "preparing"}):
            raise WorkflowError("현재 확정 주제와 회차 재개 위치가 일치하지 않습니다.", path)
        pending["resume_run_dir"] = str(path)
        self._save_pending_topic(pending)
        self._naver_log(f"회차 재개 위치 저장: {path.name}")

    def _preflight_cli_accounts(self, config):
        """Fail before image capture or generation when a required CLI cannot sign in."""
        statuses = self.cli_bridge.check_accounts()
        problem = account_problem(statuses, config["steps"])
        if problem:
            self.events.put(("cli_status", str(problem)))
            raise problem
        self._naver_log("필수 CLI 설치·로그인 사전 확인 완료. Antigravity 계정은 실제 요청에서 확인합니다.")
        return statuses

    def prepare_cli_article(self):
        try:
            config = self._cli_configuration()
            topic = self.topic.get().strip() or self.seed.get().strip()
            entered_keywords = self._all_related_keywords()
            if not topic:
                raise ValueError("주제를 입력하거나 관심 주제 자동 선정을 실행하세요.")
            self._ensure_topic_allowed(topic, [], config)
        except (ValueError, WorkflowError) as exc:
            messagebox.showinfo("Blog", str(exc))
            return
        self._start_cli_job("글·이미지 생성과 교차 검수", lambda: self._prepare_manual_cli_worker(topic, entered_keywords, config))

    def _prepare_manual_cli_worker(self, topic, entered_keywords, config):
        # Snapshot Tk values before starting this worker; old result tabs may
        # belong to a completely different topic from the user's current entry.
        from picture_cleaner_pc import fetch_autocomplete, keyword_comparison_key, normalize_keyword
        if self.full_auto_stop.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")
        self._ensure_topic_allowed(topic, [], config)
        self._naver_log(f"직접 입력한 주제 '{topic}'의 최신 연관 검색어를 조회합니다.")
        related = fetch_autocomplete(topic)
        if self.full_auto_stop.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")
        actual = [word for words in related.values() if isinstance(words, list) for word in words]
        self._ensure_topic_allowed(topic, actual, config)
        keywords, seen = [], {keyword_comparison_key(topic)}
        for word in [*actual, *entered_keywords]:
            if not isinstance(word, str):
                continue
            word = normalize_keyword(word)
            key = keyword_comparison_key(word)
            if key and key not in seen and _related_to_topic(topic, word):
                seen.add(key)
                keywords.append(word)
        if not keywords:
            raise RuntimeError(f"'{topic}' 전체 주제와 일치하는 연관 검색어를 찾지 못했습니다. 같은 주제를 포함하는 구체적인 검색어를 연관 검색어 창에 입력한 뒤 다시 실행하세요.")
        self._naver_log(f"'{topic}'에 해당하는 조회·사용자 입력 연관 검색어 {len(keywords)}개로 원고를 준비합니다.")
        return self._prepare_cli_worker(topic, keywords, config)

    @staticmethod
    def _publication_payload(article):
        payload = copy.deepcopy(article)
        images, seen = [], set()
        for image in [*payload.get("images", []), *payload.get("google_images", [])]:
            key = (image.get("path"), image.get("sha256"), image.get("paragraph_index"))
            if not (key[0] or key[1]) or key not in seen:
                seen.add(key)
                images.append(image)
        payload["images"] = images
        # Older manifests kept the approved cover text only on the image record.
        if not payload.get("cover_headline"):
            cover = next((item for item in images if item.get("provider") != "google"
                          and item.get("paragraph_index") == 0 and item.get("cover_text_applied") is True), {})
            if isinstance(cover.get("cover_headline"), str):
                payload["cover_headline"] = cover["cover_headline"]
        return payload

    def _pending_publication_receipt(self, article, config):
        reader = getattr(self.naver_bot, "publication_receipt_for", None)
        if not callable(reader) or not config.get("blog_id"):
            raise WorkflowError("이전 발행 결과를 확인할 영수증 조회 기능 또는 블로그 ID가 없습니다.")
        receipt = reader(config["blog_id"], self._publication_payload(article))
        if receipt is not None and not isinstance(receipt, dict):
            raise WorkflowError("이전 발행 결과를 확인할 수 없습니다. 원고와 제출 상태를 보존합니다.")
        return receipt

    def _publish_cli_worker(self, article, config):
        if self.full_auto_stop.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")
        self._ensure_topic_allowed(article.get("topic", ""), article.get("keywords", []), config)
        publish_payload = self._publication_payload(article)
        result = self.naver_bot.publish_naver_article(config["blog_id"], publish_payload,
            publish=config["publish"], save_draft=config.get("save_draft", False))
        return self._record_cli_publication(article, config, result)

    def _recover_article_keywords(self, article):
        """Fill old manifests only from their app-owned, matching request."""
        existing = article.get("keywords")
        if isinstance(existing, list) and existing and all(isinstance(word, str) and word.strip() for word in existing):
            return  # Explicit article metadata remains authoritative.
        article.pop("keywords", None)
        raw_run, topic = article.get("run_dir"), article.get("topic")
        app_dir = getattr(self, "cli_app_dir", None)
        if not app_dir or not isinstance(raw_run, (str, Path)) or not raw_run or not isinstance(topic, str) or not topic.strip():
            return
        try:
            run = Path(raw_run).resolve()
            if (Path(app_dir) / "blog-runs").resolve() not in run.parents:
                return
            request_path = (run / "request.json").resolve()
            if request_path.parent != run:
                return
            request = json.loads(request_path.read_text(encoding="utf-8"))
            requested_topic, keywords = request.get("topic"), request.get("keywords")
            if (not isinstance(requested_topic, str) or " ".join(requested_topic.split()) != " ".join(topic.split())
                    or not isinstance(keywords, list) or not keywords
                    or any(not isinstance(word, str) or not word.strip() for word in keywords)):
                return
            article["keywords"] = list(dict.fromkeys(word.strip() for word in keywords))
        except (OSError, ValueError, TypeError, AttributeError):
            return  # Invalid legacy metadata cannot consume unrelated candidates.

    def _record_cli_publication(self, article, config, result):
        self._recover_article_keywords(article)
        article["publication"] = result
        # A bookkeeping failure must never hide an already successful publish.
        self.events.put(("cli_article", article))
        self.events.put(("cli_publication", result))
        try:
            if hasattr(self, "topic_history") and article.get("topic"):
                source_topic = article.get("source_topic") or article["topic"]
                consumed_keywords = list(dict.fromkeys([source_topic, *article.get("keywords", [])]))
                newly_recorded = self.topic_history.record_publication(article["topic"], result, article.get("run_dir", ""),
                                                                       consumed_keywords, article.get("title", ""))
                if newly_recorded or confirmed_publication(result):
                    consumed = [article["topic"], *consumed_keywords]
                    from keyword_database import update_database
                    database_path = self.cli_app_dir / "keywords.json"
                    update_database(database_path, consumed=consumed)
                    self.events.put(("cli_topic_consumed", article["topic"], consumed))
                    self._naver_log(f"발행 확인 · '{article['topic']}' 키워드를 후보 목록에서 제외했습니다.")
                    if result.get("content_verified", True) is True:
                        self._cleanup_published_artifacts(article)
                    else:
                        self._naver_log("게시 URL은 확인했습니다. 게시된 본문·사진을 확인할 자료를 보존하고 재발행하지 않습니다.")
                elif result.get("status") == "uncertain":
                    self.topic_history.record_uncertain(article["topic"], result, article.get("run_dir", ""))
        except (TopicHistoryError, OSError) as exc:
            self.full_auto_stop.set()
            self.naver_bot.stop_event.set()
            prefix = "게시글 발행은 완료됐지만" if result.get("published") else "발행 제출 결과 확인이 필요한 상태에서"
            message = f"{prefix} 이력 저장에 실패했습니다. 중복 발행을 막기 위해 자동화를 중단했습니다. 기존 발행 기록을 확인하세요: {exc}"
            self._naver_log(message)
            raise RuntimeError(message) from exc
        if config["publish"] and not result.get("published"):
            raise RuntimeError(result.get("message") or "발행 완료를 확인하지 못했습니다. 자동 재발행하지 않습니다.")
        if config.get("save_draft") and not result.get("saved"):
            raise RuntimeError(result.get("message") or "임시저장 완료를 확인하지 못했습니다.")
        return result

    def _cleanup_published_artifacts(self, article):
        """Delete only per-run artifacts below app-owned roots after a confirmed receipt was recorded."""
        root = self.cli_app_dir.resolve()
        allowed = [(root / "blog-runs").resolve(), (root / "google-reference-candidates").resolve()]
        for raw in [article.get("run_dir", ""), *article.get("auxiliary_dirs", [])]:
            if not raw:
                continue
            path = Path(raw).resolve()
            if path == root or not any(path != parent and parent in path.parents for parent in allowed):
                self._naver_log(f"산출물 정리 경로 차단: {path}")
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
            except OSError as exc:
                self._naver_log(f"발행 산출물 정리 실패 · {path}: {exc}")

    def publish_cli_article(self):
        if not self.cli_article:
            messagebox.showinfo("Blog", "먼저 글·이미지 준비를 완료하세요.")
            return
        if self.blog_result.get("1.0", "end").strip() != self.cli_article["text"].strip():
            messagebox.showinfo("Blog", "검수 후 본문이 변경되었습니다. 글·이미지 준비를 다시 실행하세요.")
            return
        try:
            config = self._cli_configuration()
        except ValueError:
            return
        article = copy.deepcopy(self.cli_article)
        self._start_cli_job("네이버 · " + config["completion_label"], lambda: self._publish_cli_worker(article, config))

    def open_cli_artifacts(self):
        folder = Path(self.cli_article["run_dir"]) if self.cli_article else self.cli_app_dir / "blog-runs"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def start_cli_automation(self, *, automatic=False):
        if self._browser_task_busy():
            if automatic:
                self.status.set("현재 작업 종료를 기다리고 있습니다.")
            else:
                messagebox.showinfo("Blog", "진행 중인 작업을 완료하거나 중지한 뒤 실행하세요.")
            return
        try:
            config = self._cli_configuration(silent=automatic)
            hours = int(self.auto_interval_hours.get())
            if hours not in range(1, 7):
                raise ValueError("자동화 간격은 1~6시간입니다.")
            config.update(interval_seconds=hours * 3600, interval_hours=hours)
        except ValueError as exc:
            if automatic:
                self.status.set(str(exc))
                self._naver_log(f"자동 시작 중단: {exc}")
            else:
                messagebox.showinfo("Blog", str(exc))
            return
        self._launch_auto_pending = self._login_success_pending = False
        self.full_auto_active = self.naver_task_active = True
        self._set_cli_runtime_controls(True)
        self.full_auto_stop.clear()
        self.naver_bot.reset_stop()
        self.status.set(f"{hours}시간마다 · 주제 자동 선정 → CLI 교차 검수 → 이미지 준비 → {config['completion_label']}")
        threading.Thread(target=self._full_automation_loop, args=(config,), daemon=True).start()

    def _cli_automation_cycle(self, config):
        pending_path = self.cli_app_dir / "pending-blog-topic.json"
        pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
        if isinstance(pending.get("config"), dict):
            config = copy.deepcopy(pending["config"])
        if pending.get("publication_started") or pending.get("phase") in {"publishing", "submitted_uncertain"}:
            was_uncertain = pending.get("phase") == "submitted_uncertain"
            prepared = pending.get("prepared_article")
            if not isinstance(prepared, dict):
                run_dir = Path(pending.get("run_dir", "")).resolve()
                parent = (self.cli_app_dir / "blog-runs").resolve()
                if parent in run_dir.parents and (run_dir / "manifest.json").is_file():
                    prepared = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                    prepared["run_dir"] = str(run_dir)
                    prepared.setdefault("source_topic", pending.get("choice", {}).get("source_topic")
                                        or pending.get("choice", {}).get("topic", prepared.get("topic", "")))
                    self._recover_article_keywords(prepared)
                    pending["prepared_article"] = prepared
            if not isinstance(prepared, dict):
                raise WorkflowError("이전 발행 결과와 준비된 원고를 확인할 자료가 필요합니다.")
            receipt = self._pending_publication_receipt(prepared, config) if config.get("publish") else None
            if (isinstance(receipt, dict) and receipt.get("published") is not True) or (was_uncertain and receipt is None):
                pending["phase"] = "submitted_uncertain"
                self._save_pending_topic(pending)
                raise WorkflowError("이전 발행 결과 확인이 필요합니다. 이미 제출한 글은 다시 발행하지 않습니다.")
            if isinstance(receipt, dict):
                pending["confirmed_receipt"] = receipt
                self._naver_log("저장된 게시 성공 영수증을 확인했습니다. 원고 재생성·재발행 없이 이력 처리를 이어갑니다.")
            else:
                self._naver_log("최종 발행 제출 기록이 없습니다. 준비된 같은 원고로 편집기 작업을 다시 시작합니다.")
            pending["publication_started"] = False
            pending["phase"] = "prepared"
            self._save_pending_topic(pending)
        if not isinstance(pending.get("prepared_article"), dict):
            self._preflight_cli_accounts(config)
        if pending.get("choice"):
            return self._complete_selected_topic(config, pending["groups"], pending["related"],
                                                 pending["choice"], pending)
        groups = self._cli_realtime_groups()
        ranked, related_by_topic = self._rank_longtail_topics(groups, config=config)
        if config.get("quality_checks"):
            ranked = [candidate for candidate in ranked if candidate.get("keywords")]
            if not ranked:
                raise WorkflowError("검색 의도를 확인할 실제 연관어가 있는 미사용 주제를 수집 중입니다.")
        selector = BlogWorkflow(self.cli_bridge, self.cli_app_dir / "blog-runs", self._naver_log, self.full_auto_stop)
        provider = config["steps"][0]
        attempts, article, choice = [], None, None
        selection_options = {"provider": provider, "model": config.get("models", {}).get(provider, ""),
                             "blocked_terms": config.get("blocked_terms")}
        if config.get("stage_configs") and config["stage_configs"][0].get("model"):
            selection_options["model"] = config["stage_configs"][0]["model"]
        recent = self.topic_history.recent_publications(30) if hasattr(self, "topic_history") else None
        if isinstance(recent, list): selection_options["recent_publications"] = recent
        for offset in (0, 12):
            batch = ranked[offset:offset + 12]
            if not batch: break
            try:
                choice = selector.select_topic(batch, **selection_options)
                break
            except Exception as exc:
                problem = access_error_from_exception(exc)
                if problem: raise problem from exc
                self._discard_topic_review(getattr(exc, "run_dir", ""))
                self._naver_log(f"후보 {offset + 1}~{offset + len(batch)} CLI 선정 거절: {exc}")
        if choice is None:
            if not ranked:
                raise WorkflowError("진행 가능한 미사용 주제가 없습니다. 다음 수집에서 다시 확인합니다.")
            fallback = ranked[0]
            choice = {**fallback, "source_topic": fallback["topic"], "intent": "연관 검색어 기반 최고 점수 후보",
                      "selection_run_dir": ""}
            self._naver_log(f"CLI 선정 거절 2회 · 스포츠·사망이 아닌 최고 점수 후보 '{fallback['topic']}'로 진행합니다.")
        self._ensure_topic_allowed(choice["topic"], choice["keywords"], config)
        pending = {"choice": choice, "groups": groups, "related": related_by_topic, "config": copy.deepcopy(config), "phase": "preparing"}
        self._save_pending_topic(pending)
        return self._complete_selected_topic(config, groups, related_by_topic, choice, pending)

    def _save_pending_topic(self, pending):
        path = self.cli_app_dir / "pending-blog-topic.json"
        atomic_json_write(path, pending)

    def _complete_selected_topic(self, config, groups, related_by_topic, choice, pending):
        attempts, article = [], None
        # Once selected, keep the topic fixed throughout preparation and recovery.
        recovery_config = dict(config)
        if choice.get("source_topic"):
            recovery_config["quality_topic"] = choice["source_topic"]
        if choice.get("intent"):
            recovery_config["selection_intent"] = choice["intent"]
        if choice.get("semantic_selection", {}).get("intent_question"):
            recovery_config["selection_question"] = choice["semantic_selection"]["intent_question"]
        if pending.get("resume_run_dir"):
            recovery_config["resume_run_dir"] = pending["resume_run_dir"]
        if pending.get("revision_feedback"):
            recovery_config["revision_feedback"] = pending["revision_feedback"]
        topic, keywords = choice["topic"], choice["keywords"]
        if isinstance(pending.get("prepared_article"), dict):
            article = copy.deepcopy(pending["prepared_article"])
            self._naver_log(f"'{topic}' · 승인된 준비 원고와 이미지를 다시 사용합니다.")
        for index in ([] if article is not None else range(1, 4)):
            if self.full_auto_stop.is_set():
                raise WorkflowError("사용자가 작업을 중지했습니다.")
            topic = choice["topic"]
            article = None
            self._naver_log(f"확정 주제 '{topic}' · 준비 시도 {index}/3")
            try:
                current = choice
                topic, keywords = current["topic"], current["keywords"]
                self._ensure_topic_allowed(topic, keywords, config)
                source_topic = current.get("source_topic", topic)
                self.events.put(("auto_topic", groups, topic, keywords, related_by_topic.get(source_topic, {})))
                prepared = self._prepare_cli_worker(topic, keywords, recovery_config)
                if hasattr(self, "topic_history") and self.topic_history.is_duplicate(topic, keywords, prepared.get("title", ""),
                        keyword_threshold=config.get("duplicate_keyword_threshold", .4),
                        title_threshold=config.get("duplicate_title_threshold", .5)) is True:
                    recent = self.topic_history.recent_publications(10)
                    feedback = "같은 검색 주제를 유지하면서 기존 발행 글과 겹치는 제목·관점을 수정하세요. " + json.dumps({
                        "duplicate_title": str(prepared.get("title", ""))[:120], "recent_publications": [
                            {"title": str(item.get("title", ""))[:100], "topic": str(item.get("topic", ""))[:60]}
                            for item in recent[:10]]}, ensure_ascii=False)
                    pending["revision_feedback"] = recovery_config["revision_feedback"] = feedback
                    raise WorkflowError("확정 주제의 원고가 발행 이력과 유사합니다. 같은 주제의 원고 수정이 필요합니다.", Path(prepared["run_dir"]))
                article = prepared
                article["source_topic"] = current.get("source_topic", topic)
                selection_value = current.get("selection_run_dir", "")
                selection_dir = Path(selection_value).resolve() if selection_value else None
                if selection_dir is not None and selection_dir.is_dir():
                    destination = (Path(article["run_dir"]) / "topic-review").resolve()
                    root = (self.cli_app_dir / "blog-runs").resolve()
                    if root in selection_dir.parents and root in destination.parents and selection_dir != destination:
                        if destination.exists(): shutil.rmtree(destination)
                        shutil.move(str(selection_dir), str(destination))
                break
            except Exception as exc:
                article = None
                resume_dir = getattr(exc, "run_dir", "")
                if resume_dir and (Path(resume_dir) / "manifest.json").is_file():
                    recovery_config["resume_run_dir"] = str(resume_dir)
                    pending["resume_run_dir"] = str(resume_dir)
                    self._save_pending_topic(pending)
                if self.full_auto_stop.is_set():
                    raise WorkflowError("사용자가 작업을 중지했습니다.", resume_dir or recovery_config.get("resume_run_dir")) from exc
                problem = access_error_from_exception(exc)
                if problem:
                    raise problem from exc
                if resume_dir:
                    try:
                        failed = json.loads((Path(resume_dir) / "manifest.json").read_text(encoding="utf-8"))
                        flags = ("approved", "facts_verified", "sources_verified", "search_intent_satisfied", "natural_korean")
                        rejected = [item.get("review", {}) for item in failed.get("final_review_attempts", [])
                            if isinstance(item, dict) and isinstance(item.get("review"), dict)
                            and (any(item["review"].get(flag) is not True for flag in flags) or item["review"].get("issues"))]
                    except (OSError, ValueError, TypeError, AttributeError):
                        rejected = []
                    if rejected:
                        pending["revision_number"] = int(pending.get("revision_number", 0)) + 1
                        issues = [str(issue)[:240] for review in rejected[-2:] for issue in review.get("issues", [])][:8]
                        missing_flags = sorted({flag for review in rejected[-2:] for flag in flags if review.get(flag) is not True})
                        feedback = ("확정 주제를 유지하고 아래 최종 검수 지적만 근거에 맞게 수정하세요. "
                            "확인되지 않은 사실을 단정하거나 검수 통과를 꾸미지 마세요. " + json.dumps({
                                "revision": pending["revision_number"], "issues": issues, "unverified_checks": missing_flags,
                                "title": str(failed.get("title", ""))[:120]}, ensure_ascii=False))
                        pending["revision_feedback"] = recovery_config["revision_feedback"] = feedback
                        self._save_pending_topic(pending)
                        self._naver_log("최종 검수 지적을 같은 주제의 원고 수정에 반영합니다.")
                attempts.append({"topic": topic, "stage": "prepare", "error": str(exc),
                                 "run_dir": resume_dir})
                self._discard_topic_review(getattr(exc, "run_dir", ""))
                self._write_cycle_attempts(attempts)
                self._naver_log(f"'{topic}' 주제 유지 · 준비 재개 필요: {exc}")
        if article is None:
            raise WorkflowError(f"확정 주제 '{topic}'의 준비를 {len(attempts)}회 시도했지만 준비되지 않았습니다. 주제를 바꾸지 않고 검토 자료를 보존합니다.", recovery_config.get("resume_run_dir"))
        # Browser publication/draft failures never enter the candidate retry loop.
        self._recover_article_keywords(article)
        pending["prepared_article"] = copy.deepcopy(article)
        pending["phase"] = "publishing"
        pending["publication_started"] = False
        pending["run_dir"] = article["run_dir"]
        self._save_pending_topic(pending)
        if isinstance(pending.get("completion_result"), dict):
            result = pending["completion_result"]
            self._naver_log("완료한 회차의 저장 처리를 이어갑니다. 네이버 작업은 반복하지 않습니다.")
        elif pending.get("confirmed_receipt"):
            result = self._record_cli_publication(article, config, pending["confirmed_receipt"])
        else:
            result = self._publish_cli_worker(article, config)
        if not result.get("published") and config["publish"]:
            raise RuntimeError("네이버 발행 완료를 확인하지 못했습니다.")
        pending["completion_result"] = copy.deepcopy(result)
        pending["phase"] = "completed"
        self._save_pending_topic(pending)
        record = {"topic": topic, "keywords": keywords, "providers": config["steps"],
                  "source_topic": article.get("source_topic", topic), "title": article.get("title", ""),
                  "saved_at": datetime.now().isoformat(timespec="seconds"),
                  "draft_only": config.get("save_draft", False), "completion_action": config.get("completion_label", "자동 발행"),
                  "run_dir": article["run_dir"], "publication": result}
        self.auto_history = [*[item for item in self.auto_history if item.get("run_dir") != article["run_dir"]], record][-200:]
        history = self.cli_app_dir / "automation-history.json"
        atomic_json_write(history, self.auto_history)
        (self.cli_app_dir / "pending-blog-topic.json").unlink(missing_ok=True)
        self._naver_log(f"'{topic}' 회차 완료 · {config.get('completion_label', '자동 발행')}")

    def _write_cycle_attempts(self, attempts):
        path = self.cli_app_dir / "last-cycle-attempts.json"
        atomic_json_write(path, {"updated_at": datetime.now().isoformat(timespec="seconds"), "attempts": attempts})
