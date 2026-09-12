"""Non-modal launch, account recovery and orderly restart controls."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import BooleanVar, Toplevel, ttk

from blog_cli_bridge import BlogCliBridge, BlogCliError
from blog_preferences import PROVIDER_LABELS


class CliAccessRequired(RuntimeError):
    def __init__(self, failures):
        self.failures = failures
        super().__init__("필수 CLI 사전 확인 실패 · " + " / ".join(
            f"{PROVIDER_LABELS.get(key, key)}: {reason}" for key, reason in failures.items()))


def account_problem(statuses, steps):
    failures = {}
    for provider in dict.fromkeys([*steps, "chatgpt", "antigravity"]):
        status = statuses.get(provider, {})
        if not status.get("installed"):
            failures[provider] = "CLI 설치 또는 실행 파일 경로 확인이 필요합니다."
        elif "authentication_required" in (status.get("auth_status"), status.get("text_status")):
            failures[provider] = "구독 계정 로그인이 필요합니다."
        elif status.get("auth_status") in {"launch_failed", "timeout"}:
            failures[provider] = "CLI 연결을 확인하지 못했습니다. 로그인 또는 설치 상태를 확인하세요."
    return CliAccessRequired(failures) if failures else None


def access_error_from_exception(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, CliAccessRequired):
            return error
        if isinstance(error, BlogCliError) and error.code in {"authentication_required", "not_installed"}:
            return CliAccessRequired({error.provider: str(error)})
        error = error.__cause__
    return None


def restart_command():
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).with_name("picture_cleaner_pc.py")))
    return [*command, "--wait-parent-pid", str(os.getpid())]


def wait_for_restart_parent(argv):
    """A replacement process cannot start automation while its parent is closing."""
    if "--wait-parent-pid" not in argv:
        return
    index = argv.index("--wait-parent-pid")
    pid = int(argv[index + 1])
    if pid <= 0 or pid == os.getpid():
        raise RuntimeError("다시 시작할 이전 프로세스 ID가 올바르지 않습니다.")
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # Already exited.
                return
            raise RuntimeError("이전 프로그램의 종료를 확인하지 못해 자동 시작하지 않았습니다.")
        try:
            if kernel.WaitForSingleObject(handle, 60000) != 0:
                raise RuntimeError("이전 프로그램이 아직 종료되지 않아 중복 자동 실행을 중단했습니다.")
        finally:
            kernel.CloseHandle(handle)


class UnattendedControls:
    def _init_unattended_controls(self, preferences):
        self.auto_start_on_launch = BooleanVar(value=preferences["auto_start_on_launch"])
        self._launch_auto_pending = self.auto_start_on_launch.get()
        self._startup_refresh_finished = False
        self._closing = False
        self._restart_requested = False
        self._resume_after_login = False
        self._login_success_pending = False
        self._login_generation = 0
        self.cli_login_active = False
        self.cli_login_cancel = threading.Event()
        self._login_window = None

    def _save_launch_choice(self):
        if not self.auto_start_on_launch.get():
            self._launch_auto_pending = False
        self._save_cli_selection()

    def _cancel_automatic_resume(self):
        self._launch_auto_pending = False
        self._resume_after_login = False
        self._login_success_pending = False
        self._login_generation = getattr(self, "_login_generation", 0) + 1
        if hasattr(self, "cli_login_cancel"):
            self.cli_login_cancel.set()

    def _maybe_launch_automation(self):
        if not (getattr(self, "_launch_auto_pending", False) or getattr(self, "_login_success_pending", False)):
            return
        if (getattr(self, "_closing", False) or getattr(self, "_restart_requested", False)
                or self.full_auto_stop.is_set() or self._browser_task_busy()):
            return
        resume = getattr(self, "_login_success_pending", False)
        launch = (getattr(self, "_launch_auto_pending", False)
                  and getattr(self, "_startup_refresh_finished", False)
                  and self.auto_start_on_launch.get())
        if resume or launch:
            self._launch_auto_pending = self._login_success_pending = False
            self._resume_after_login = False
            self.start_cli_automation(automatic=True)

    def _handle_runtime_event(self, event):
        kind = event[0]
        if kind == "realtime_finished":
            if event[1]:
                self._startup_refresh_finished = True
            return True
        if kind == "cli_access_required":
            # A stop/restart request wins over an already queued failure.
            if self.full_auto_stop.is_set() or self._closing:
                return True
            self._resume_after_login = bool(event[2])
            self.show_cli_login_required(event[1])
            return True
        if kind == "cli_login_checked":
            _, token, statuses, error = event
            self.cli_login_active = False
            if token != self._login_generation or self._closing:
                return True
            if error:
                self._naver_log(f"CLI 로그인 재확인 실패: {error}")
                self.status.set(f"CLI 로그인 재확인 실패: {error}")
                return True
            self.cli_bridge = BlogCliBridge(self.cli_app_dir, self._naver_log, self.full_auto_stop)
            pref = self.cli_preferences
            problem = account_problem(statuses, pref["order"][:pref["step_count"]])
            if problem:
                self.show_cli_login_required(problem)
            else:
                if self._login_window is not None and self._login_window.winfo_exists():
                    self._login_window.destroy()
                self.cli_capability_text.set("CLI 연결 확인 완료 · Antigravity 계정은 실제 요청에서 확인합니다.")
                self.status.set("CLI 연결 확인 완료 · Antigravity 계정은 실제 요청에서 확인합니다.")
                self._login_success_pending = self._resume_after_login
            return True
        return False

    def show_cli_login_required(self, problem=None):
        # Deliberately no grab_set/wait_window: the app remains operable unattended.
        if self._login_window is not None and self._login_window.winfo_exists():
            self._login_window.destroy()
        window = self._login_window = Toplevel(self.root)
        window.title("CLI 로그인 필요" if problem else "CLI 계정 로그인")
        window.transient(self.root)
        panel = ttk.Frame(window, padding=18)
        panel.pack(fill="both", expand=True)
        message = str(problem) if problem else "사용할 CLI에 로그인하세요. 로그인 콘솔을 닫으면 자동으로 연결을 다시 확인합니다."
        ttk.Label(panel, text=message, wraplength=600).pack(anchor="w", pady=(0, 12))
        providers = problem.failures if problem else PROVIDER_LABELS
        for provider in providers:
            ttk.Button(panel, text=PROVIDER_LABELS.get(provider, provider) + " · 지금 로그인",
                       command=lambda chosen=provider: self._open_blog_cli_login(chosen)).pack(fill="x", pady=3)
        ttk.Label(panel, text="로그인 창에서 인증을 마친 뒤 콘솔을 닫으세요. 필요하면 실행 순서를 바꾸고 다시 시작할 수 있습니다.",
                  wraplength=600).pack(anchor="w", pady=8)
        ttk.Button(panel, text="실행 순서 변경", command=self._edit_cli_order).pack(fill="x", pady=3)
        ttk.Button(panel, text="프로그램 다시 시작", command=self.restart_program).pack(fill="x", pady=3)
        self.status.set(message)
        self._naver_log(message)

    def _edit_cli_order(self):
        self._resume_after_login = self._login_success_pending = False
        self.tabs.select(self.blog_tab)
        if self._login_window is not None and self._login_window.winfo_exists():
            self._login_window.destroy()
        self.status.set("CLI 실행 단계·순서를 변경하세요. 변경값은 저장되며 다시 시작하면 적용됩니다.")

    def _open_blog_cli_login(self, provider):
        if self.cli_login_active:
            self.status.set("열려 있는 CLI 로그인 콘솔을 먼저 완료하거나 닫아 주세요.")
            return
        if self._browser_task_busy():
            self.status.set("현재 작업을 중지한 뒤 CLI 로그인을 실행하세요.")
            return
        self.cli_login_cancel = threading.Event()
        bridge = BlogCliBridge(self.cli_app_dir, self._naver_log, self.cli_login_cancel)
        try:
            process = bridge.open_login(provider, return_process=True)
        except Exception as exc:
            self.status.set(f"CLI 로그인 창 실행 실패: {exc}")
            self._naver_log(f"CLI 로그인 창 실행 실패: {exc}")
            return
        self.cli_login_active = True
        token, cancel = self._login_generation, self.cli_login_cancel
        self.status.set(f"{PROVIDER_LABELS[provider]} 로그인 콘솔을 닫으면 연결을 재점검합니다.")
        def monitor():
            statuses, error = {}, ""
            try:
                while process.poll() is None:
                    if cancel.wait(0.25):
                        return
                if not cancel.is_set():
                    bridge.login_console_closed(provider)
                    statuses = bridge.check_accounts()
            except Exception as exc:
                error = str(exc)
            finally:
                self.events.put(("cli_login_checked", token, statuses, error))
        threading.Thread(target=monitor, daemon=True).start()

    def restart_program(self):
        if self._restart_requested:
            return
        self._restart_requested = True
        self.stop_full_automation()
        self.status.set("작업 중단과 설정 저장 후 프로그램을 다시 시작합니다.")
        self._finish_restart_when_idle()

    def _finish_restart_when_idle(self):
        if self._browser_task_busy():
            self.root.after(100, self._finish_restart_when_idle)
            return
        try:
            if not self.save_cli_prompt(silent=True):
                raise ValueError("프롬프트와 실행 설정을 저장하지 못했습니다.")
            subprocess.Popen(restart_command(), cwd=str(Path(sys.executable).parent), shell=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            self._restart_requested = False
            self.status.set(f"프로그램 다시 시작 실패: {exc}")
            self._naver_log(f"프로그램 다시 시작 실패: {exc}")
            return
        self.close()
