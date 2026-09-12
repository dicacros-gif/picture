"""Tk controls for the CLI-only article workflow."""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, StringVar, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from blog_cli_bridge import BlogCliBridge
from blog_preferences import (PROVIDER_LABELS, DEFAULT_BLOCKED_TERMS, STAGE_ROLES, normalize_preferences,
                              store_prompt, normalize_blocked_terms, blocked_term_hits)
from blog_workflow import BlogWorkflow, REVIEW_MODES, WorkflowError, _related_to_topic
from blog_runtime import UnattendedControls, account_problem, access_error_from_exception
from blog_topic_history import TopicHistory, TopicHistoryError


def next_cycle_tick(previous_tick: float, now: float, interval: float) -> float:
    """Keep a fixed cadence without overlapping runs or catching up missed slots."""
    if interval <= 0:
        raise ValueError("반복 간격은 양수여야 합니다.")
    candidate = previous_tick + interval
    if candidate <= now:
        candidate += (int((now - candidate) // interval) + 1) * interval
    return candidate


class BlogWorkflowControls(UnattendedControls):
    def _browser_task_busy(self):
        return any(getattr(self, name, False) for name in
                   ("naver_task_active", "full_auto_active", "realtime_task_active", "cli_login_active"))

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
                protected = {Path(p).resolve() for p in (pending.get("resume_run_dir"), pending.get("run_dir"),
                    pending.get("choice", {}).get("selection_run_dir")) if p}
            except (ValueError, OSError):
                return  # Preserve work if its pending receipt cannot be read.
        for parent_name in ("blog-runs", "google-reference-candidates"):
            parent = self.cli_app_dir / parent_name
            if not parent.is_dir():
                continue
            for path in parent.iterdir():
                try:
                    if path.resolve() not in protected and path.is_dir() and path.stat().st_mtime < cutoff:
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
        ttk.Button(settings, text="관심 주제 자동 선정", command=self.select_cli_topic).grid(row=0, column=6)
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
        ttk.Checkbutton(options, text="Google 후보 2장도 검수", variable=self.cli_google, command=self._save_cli_selection).pack(side="left", padx=8)
        ttk.Label(options, text="완료 후").pack(side="left", padx=(8, 0))
        mode = ttk.Combobox(options, textvariable=self.cli_publication, values=["자동 발행", "임시저장까지만", "편집기에 입력만"], state="readonly", width=16)
        mode.pack(side="left", padx=5)
        self.cli_runtime_selectors.append(mode)
        mode.bind("<<ComboboxSelected>>", self._save_cli_selection)
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
        ttk.Label(self.blog_tab, text="연관 검색어의 질문 → 제목·8문단 → CLI 교차 검수 → Antigravity 4장 + ChatGPT 4장 → 검수 통과 6장", style="Sub.TLabel").grid(row=1, column=0, sticky="w", pady=7)
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
        ttk.Button(actions, text="글·이미지 준비", command=self.prepare_cli_article, style="Accent.TButton").pack(side="left")
        ttk.Button(actions, text="준비된 글 네이버에 입력·실행", command=self.publish_cli_article, style="Copy.TButton").pack(side="left", padx=6)
        ttk.Button(actions, text="실행 자료 폴더", command=self.open_cli_artifacts).pack(side="left")
        ttk.Button(actions, text="결과 복사", command=lambda: self.copy_widget(self.blog_result)).pack(side="left", padx=6)
        ttk.Button(actions, text="웨일 네이버 로그인", command=self.open_naver_login).pack(side="left")
        ttk.Button(actions, text="작업 중지", command=self.stop_full_automation, style="Danger.TButton").pack(side="right")
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
        self.cli_preferences.update(
            order=[reverse[value.get()] for value in self.cli_order],
            stages=[{"provider": reverse[value.get()], "role": self.cli_roles[index].get(),
                     "model": self.cli_stage_models[index].get().strip()} for index, value in enumerate(self.cli_order)],
            step_count=int(self.cli_step_count.get()), review_mode=self.cli_review_mode.get(),
            models={key: value.get().strip() for key, value in self.cli_models.items()},
            include_google=self.cli_google.get(), publication_mode=self.cli_publication.get(),
            auto_start_on_launch=self.auto_start_on_launch.get(),
            blocked_terms=normalize_blocked_terms(self.cli_blocked_terms.get("1.0", "end")),
            duplicate_keyword_threshold=float(self.cli_duplicate_keywords.get()),
            duplicate_title_threshold=float(self.cli_duplicate_titles.get()),
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
        self.settings["cli_workflow"] = copy.deepcopy(self.cli_preferences)
        self.settings["auto_interval_hours"] = self.auto_interval_hours.get()
        self.settings["blog_id"] = self.blog_id.get().strip()
        self.cli_config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cli_config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.cli_config_path)

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
        google = []
        google_folder = None
        if config["include_google"]:
            self._naver_log("Google 이미지 후보 1·2번의 화면과 출처를 확인합니다.")
            try:
                folder = self.cli_app_dir / "google-reference-candidates" / datetime.now().strftime("%Y%m%d-%H%M%S")
                google_folder = folder
                google = self.naver_bot.capture_google_reference_candidates(topic, folder, count=2)
            except Exception as exc:
                self._naver_log(f"Google 참고 이미지 생략: {exc}")
        workflow = BlogWorkflow(self.cli_bridge, self.cli_app_dir / "blog-runs", self._naver_log, self.full_auto_stop)
        resume_options = {"resume_run_dir": config["resume_run_dir"]} if config.get("resume_run_dir") else {}
        if config.get("stage_configs"):
            resume_options["stage_configs"] = config["stage_configs"]
        brief = config["base_prompt"]
        if config.get("selection_intent") or config.get("selection_question"):
            brief += "\n확정된 검색 의도(주제를 바꾸지 말고 이 궁금증에 답한다): " + json.dumps({
                "intent": config.get("selection_intent", ""), "question": config.get("selection_question", ""),
                "related_keywords": keywords}, ensure_ascii=False)
        article = workflow.prepare(topic, keywords, brief, config["steps"],
                                   config["review_mode"], models=config["models"], google_candidates=google,
                                   **resume_options)
        article["blog_id"] = config["blog_id"]
        article["auxiliary_dirs"] = [str(google_folder)] if google_folder else []
        self.events.put(("cli_article", article))
        return article

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

    def _publish_cli_worker(self, article, config):
        if self.full_auto_stop.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")
        self._ensure_topic_allowed(article.get("topic", ""), article.get("keywords", []), config)
        publish_payload = copy.deepcopy(article)
        if publish_payload.get("google_images"):
            publish_payload["images"] = [image for image in publish_payload["images"] if image.get("provider") != "google"] + publish_payload["google_images"]
        result = self.naver_bot.publish_naver_article(config["blog_id"], publish_payload,
            publish=config["publish"], save_draft=config.get("save_draft", False))
        article["publication"] = result
        # A bookkeeping failure must never hide an already successful publish.
        self.events.put(("cli_article", article))
        self.events.put(("cli_publication", result))
        try:
            if hasattr(self, "topic_history") and article.get("topic"):
                if self.topic_history.record_publication(article["topic"], result, article.get("run_dir", ""),
                                                         article.get("keywords", []), article.get("title", "")):
                    consumed = [article["topic"], *article.get("keywords", [])]
                    from keyword_database import consume, load_database, save_database
                    database_path = self.cli_app_dir / "keywords.json"
                    save_database(database_path, consume(load_database(database_path), consumed))
                    self.events.put(("cli_topic_consumed", article["topic"], consumed))
                    self._naver_log(f"발행 확인 · '{article['topic']}' 키워드를 후보 목록에서 제외했습니다.")
                    self._cleanup_published_artifacts(article)
                elif result.get("status") == "uncertain":
                    self.topic_history.record_uncertain(article["topic"], result, article.get("run_dir", ""))
        except TopicHistoryError as exc:
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
        self._preflight_cli_accounts(config)
        pending_path = self.cli_app_dir / "pending-blog-topic.json"
        pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
        if pending.get("publication_started"):
            raise WorkflowError("확정 주제의 이전 발행 결과 확인이 필요합니다. 중복 발행을 막기 위해 원고를 보존합니다.")
        if pending.get("choice"):
            return self._complete_selected_topic(config, pending["groups"], pending["related"],
                                                 pending["choice"], pending)
        groups = self._cli_realtime_groups()
        ranked, related_by_topic = self._rank_longtail_topics(groups, config=config)
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
                failed_dir = Path(getattr(exc, "run_dir", ""))
                if failed_dir.name.startswith("topic-review-") and failed_dir.is_dir(): shutil.rmtree(failed_dir, ignore_errors=True)
                self._naver_log(f"후보 {offset + 1}~{offset + len(batch)} CLI 선정 거절: {exc}")
        if choice is None:
            if not ranked:
                raise WorkflowError("진행 가능한 미사용 주제가 없습니다. 다음 수집에서 다시 확인합니다.")
            fallback = ranked[0]
            choice = {**fallback, "source_topic": fallback["topic"], "intent": "연관 검색어 기반 최고 점수 후보",
                      "selection_run_dir": ""}
            self._naver_log(f"CLI 선정 거절 2회 · 스포츠·사망이 아닌 최고 점수 후보 '{fallback['topic']}'로 진행합니다.")
        self._ensure_topic_allowed(choice["topic"], choice["keywords"], config)
        pending = {"choice": choice, "groups": groups, "related": related_by_topic}
        self._save_pending_topic(pending)
        return self._complete_selected_topic(config, groups, related_by_topic, choice, pending)

    def _save_pending_topic(self, pending):
        path = self.cli_app_dir / "pending-blog-topic.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _complete_selected_topic(self, config, groups, related_by_topic, choice, pending):
        attempts, article = [], None
        # Once selected, keep the topic fixed throughout preparation and recovery.
        recovery_config = dict(config)
        if choice.get("intent"):
            recovery_config["selection_intent"] = choice["intent"]
        if choice.get("semantic_selection", {}).get("intent_question"):
            recovery_config["selection_question"] = choice["semantic_selection"]["intent_question"]
        if pending.get("resume_run_dir"):
            recovery_config["resume_run_dir"] = pending["resume_run_dir"]
        for index in range(1, 4):
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
                    raise WorkflowError("확정 주제의 원고가 발행 이력과 유사합니다. 같은 주제의 원고 수정이 필요합니다.", Path(prepared["run_dir"]))
                article = prepared
                selection_value = current.get("selection_run_dir", "")
                selection_dir = Path(selection_value) if selection_value else None
                if selection_dir is not None and selection_dir.is_dir():
                    destination = Path(article["run_dir"]) / "topic-review"
                    if destination.exists(): shutil.rmtree(destination)
                    shutil.move(str(selection_dir), str(destination))
                break
            except Exception as exc:
                if self.full_auto_stop.is_set():
                    raise WorkflowError("사용자가 작업을 중지했습니다.") from exc
                article = None
                problem = access_error_from_exception(exc)
                if problem:
                    raise problem from exc
                attempts.append({"topic": topic, "stage": "prepare", "error": str(exc),
                                 "run_dir": getattr(exc, "run_dir", "")})
                resume_dir = getattr(exc, "run_dir", "")
                if resume_dir and (Path(resume_dir) / "manifest.json").is_file():
                    recovery_config["resume_run_dir"] = str(resume_dir)
                    pending["resume_run_dir"] = str(resume_dir)
                    self._save_pending_topic(pending)
                failed_dir = Path(getattr(exc, "run_dir", ""))
                if failed_dir.name.startswith("topic-review-") and failed_dir.is_dir():
                    shutil.rmtree(failed_dir, ignore_errors=True)
                self._write_cycle_attempts(attempts)
                self._naver_log(f"'{topic}' 주제 유지 · 준비 재개 필요: {exc}")
        if article is None:
            raise WorkflowError(f"확정 주제 '{topic}'의 준비를 {len(attempts)}회 시도했지만 준비되지 않았습니다. 주제를 바꾸지 않고 검토 자료를 보존합니다.", recovery_config.get("resume_run_dir"))
        # Browser publication/draft failures never enter the candidate retry loop.
        pending["publication_started"] = True
        pending["run_dir"] = article["run_dir"]
        self._save_pending_topic(pending)
        result = self._publish_cli_worker(article, config)
        if not result.get("published") and config["publish"]:
            raise RuntimeError("네이버 발행 완료를 확인하지 못했습니다.")
        (self.cli_app_dir / "pending-blog-topic.json").unlink(missing_ok=True)
        record = {"topic": topic, "keywords": keywords, "providers": config["steps"],
                  "saved_at": datetime.now().isoformat(timespec="seconds"),
                  "draft_only": config.get("save_draft", False), "completion_action": config.get("completion_label", "자동 발행"),
                  "run_dir": article["run_dir"], "publication": result}
        self.auto_history = [*self.auto_history, record][-200:]
        history = self.cli_app_dir / "automation-history.json"
        history.write_text(json.dumps(self.auto_history, ensure_ascii=False, indent=2), encoding="utf-8")
        self._naver_log(f"'{topic}' 회차 완료 · {config.get('completion_label', '자동 발행')}")

    def _write_cycle_attempts(self, attempts):
        path = self.cli_app_dir / "last-cycle-attempts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"updated_at": datetime.now().isoformat(timespec="seconds"),
                                         "attempts": attempts}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
