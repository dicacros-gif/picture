from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


class ChatGPTClassicAutomation:
    """ChatGPT Classic의 공개 Windows UI Automation 요소만 사용한다."""

    APP_TITLE = "ChatGPT Classic"
    PROJECT_NAME = "Phone 미래 전망"
    PROMPT_AUTOMATION_ID = "prompt-textarea"
    RERUN_TEXT = "지침대로 다시 실행"
    APP_USER_MODEL_ID = "OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT"

    def __init__(
        self,
        log: Callable[[str], None],
        stop_event: threading.Event | None = None,
    ):
        self.log = log
        self.stop_event = stop_event or threading.Event()

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise RuntimeError("사용자가 작업을 중지했습니다.")

    @staticmethod
    @contextmanager
    def _com_scope():
        initialized = False
        try:
            import pythoncom

            pythoncom.CoInitialize()
            initialized = True
        except Exception:
            pythoncom = None
        try:
            yield
        finally:
            if initialized and pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    @staticmethod
    def _control_name(control) -> str:
        try:
            return (control.element_info.name or "").strip()
        except Exception:
            try:
                return (control.window_text() or "").strip()
            except Exception:
                return ""

    @staticmethod
    def _control_type(control) -> str:
        try:
            return (control.element_info.control_type or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _automation_id(control) -> str:
        try:
            return (control.element_info.automation_id or "").strip()
        except Exception:
            return ""

    @classmethod
    def _usable(cls, control) -> bool:
        try:
            return bool(control.is_visible() and control.is_enabled())
        except Exception:
            return False

    @classmethod
    def _controls(cls, window) -> list:
        try:
            return list(window.descendants())
        except Exception:
            return []

    @staticmethod
    def _runtime_token(control) -> tuple:
        try:
            runtime_id = control.element_info.runtime_id
            if runtime_id:
                return ("runtime", *tuple(runtime_id))
        except Exception:
            pass
        try:
            rectangle = control.rectangle()
            return (
                "fallback",
                control.element_info.handle,
                rectangle.left,
                rectangle.top,
                rectangle.right,
                rectangle.bottom,
            )
        except Exception:
            return ("object", id(control))

    @staticmethod
    def _find_executable() -> Path | None:
        command = shutil.which("chatgpt-classic.exe")
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        try:
            installed = sorted(
                (
                    program_files
                    / "WindowsApps"
                ).glob(
                    "OpenAI.ChatGPT-Desktop_*/app/ChatGPT Classic.exe"
                ),
                key=lambda path: path.parent.parent.name,
                reverse=True,
            )
            if installed:
                return installed[0]
        except (OSError, PermissionError):
            pass
        candidates = [
            Path(command) if command else None,
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft"
            / "WindowsApps"
            / "chatgpt-classic.exe",
        ]
        found = next(
            (candidate for candidate in candidates if candidate and candidate.is_file()),
            None,
        )
        if found is not None:
            return found
        return None

    @classmethod
    def _find_window(cls):
        from pywinauto import Desktop

        expected = cls.APP_TITLE.casefold()
        # Electron/UWP 창은 작업 스레드에서 UIA 최상위 창 열거가 잠시
        # 비어 보일 때가 있다. UIA를 먼저 사용하되 Win32 핸들로도 찾아
        # 다시 UIA 래퍼로 연결한다.
        try:
            for window in Desktop(backend="uia").windows(visible_only=True):
                title = cls._control_name(window).casefold()
                if title == expected or expected in title:
                    return window
        except Exception:
            pass
        try:
            for window in Desktop(backend="win32").windows(visible_only=True):
                title = (window.window_text() or "").strip().casefold()
                if title == expected or expected in title:
                    return Desktop(backend="uia").window(handle=window.handle)
        except Exception:
            pass
        return None

    def _launch_process(self) -> None:
        executable = self._find_executable()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if executable:
            subprocess.Popen(
                [str(executable), "--force-renderer-accessibility=complete"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return
        subprocess.Popen(
            [
                "explorer.exe",
                f"shell:AppsFolder\\{self.APP_USER_MODEL_ID}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def _open_window(self, timeout: int = 35):
        window = self._find_window()
        if window is None:
            self.log("ChatGPT Classic을 실행합니다.")
            self._launch_process()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_stop()
            window = self._find_window()
            if window is not None:
                try:
                    if window.is_minimized():
                        window.restore()
                except Exception:
                    pass
                try:
                    window.set_focus()
                except Exception:
                    pass
                return window
            time.sleep(0.5)
        raise RuntimeError(
            "ChatGPT Classic 창을 찾지 못했습니다. "
            "Microsoft Store의 ChatGPT 앱 설치 상태를 확인해 주세요."
        )

    @classmethod
    def _project_new_chat_is_open(cls, window) -> bool:
        composer = cls._find_composer(window)
        if composer is None:
            return False
        name = cls._control_name(composer)
        return (
            cls.PROJECT_NAME in name
            and ("새 채팅" in name or "new chat" in name.casefold())
        )

    @classmethod
    def _project_controls(cls, window) -> list:
        preferred_types = {"ListItem": 4, "Hyperlink": 3, "Button": 2, "Text": 1}
        candidates = []
        try:
            window_rect = window.rectangle()
            sidebar_limit = window_rect.left + int(window_rect.width() * 0.42)
        except Exception:
            sidebar_limit = None
        for control in cls._controls(window):
            name = cls._control_name(control)
            control_type = cls._control_type(control)
            if (
                cls.PROJECT_NAME in name
                and control_type in preferred_types
                and cls._usable(control)
            ):
                exact = int(name == cls.PROJECT_NAME)
                open_label = int(
                    name
                    in {
                        f"프로젝트 {cls.PROJECT_NAME} 열기",
                        f"Open project {cls.PROJECT_NAME}",
                    }
                )
                try:
                    in_sidebar = int(
                        sidebar_limit is not None
                        and control.rectangle().left < sidebar_limit
                    )
                except Exception:
                    in_sidebar = 0
                candidates.append(
                    (
                        in_sidebar,
                        open_label,
                        exact,
                        preferred_types[control_type],
                        control,
                    )
                )
        candidates.sort(key=lambda item: item[:-1], reverse=True)
        return [item[-1] for item in candidates]

    @classmethod
    def _project_new_chat_controls(cls, window, project_control) -> list:
        """Find the compose icon displayed at the right edge of a project row."""
        candidates = []
        try:
            project_rect = project_control.rectangle()
            project_center_y = (project_rect.top + project_rect.bottom) // 2
        except Exception:
            project_rect = None
            project_center_y = None
        for control in cls._controls(window):
            if cls._control_type(control) != "Button" or not cls._usable(control):
                continue
            name = cls._control_name(control).strip().casefold()
            automation_id = cls._automation_id(control).strip().casefold()
            score = 0
            has_new_chat_label = any(
                label in name
                for label in ("새 채팅", "새 대화", "new chat", "compose")
            )
            belongs_to_project = cls.PROJECT_NAME.casefold() in name
            if has_new_chat_label:
                score += 30
            if belongs_to_project:
                score += 15
            has_compose_id = any(
                label in automation_id
                for label in ("new-chat", "new_chat", "compose")
            )
            if has_compose_id:
                score += 20
            try:
                rectangle = control.rectangle()
                aligned = (
                    project_rect is not None
                    and abs(
                        ((rectangle.top + rectangle.bottom) // 2)
                        - project_center_y
                    )
                    <= max(12, project_rect.height() // 2)
                    and rectangle.left >= project_rect.left + project_rect.width() // 2
                    and rectangle.right <= project_rect.right + 45
                )
                if aligned:
                    score += 25
                right = rectangle.right
            except Exception:
                aligned = False
                right = 0
            # Do not confuse the global "새 채팅" item at the top of the
            # sidebar with the small compose icon belonging to this project.
            if score and (aligned or belongs_to_project):
                candidates.append((score, right, control))
        candidates.sort(key=lambda item: item[:2], reverse=True)
        return [item[-1] for item in candidates]

    @classmethod
    def _activate_project_new_chat(cls, window, project_control) -> bool:
        """Click the project's right-side compose icon, including hover-only UI."""
        try:
            rectangle = project_control.rectangle()
            from pywinauto import mouse

            mouse.move(
                coords=(
                    rectangle.right - min(24, max(8, rectangle.width() // 12)),
                    (rectangle.top + rectangle.bottom) // 2,
                )
            )
            time.sleep(0.25)
        except Exception:
            rectangle = None

        controls = cls._project_new_chat_controls(window, project_control)
        if controls:
            cls._invoke_control(controls[0])
            return True

        # Some ChatGPT Classic builds expose the pencil icon visually but not
        # as a UIA Button. Only use the row-edge fallback for a full-width row.
        if rectangle is not None and rectangle.width() >= 160:
            try:
                from pywinauto import mouse

                mouse.click(
                    coords=(
                        rectangle.right - 18,
                        (rectangle.top + rectangle.bottom) // 2,
                    )
                )
                return True
            except Exception:
                pass
        return False

    @classmethod
    def _activate_project_control(cls, control) -> None:
        """사이드바 항목은 실제 마우스 클릭을 우선하고 키보드로 보완한다."""
        try:
            control.click_input()
            return
        except Exception:
            pass
        try:
            control.set_focus()
            from pywinauto import keyboard

            keyboard.send_keys("{ENTER}", pause=0.04)
            return
        except Exception:
            pass
        cls._invoke_control(control)

    @classmethod
    def _invoke_control(cls, control) -> None:
        try:
            control.invoke()
            return
        except Exception:
            pass
        try:
            control.click_input()
            return
        except Exception as exc:
            raise RuntimeError(
                f"'{cls._control_name(control) or '화면 요소'}'를 실행하지 못했습니다."
            ) from exc

    @classmethod
    def _find_composer(cls, window):
        candidates = []
        for control in cls._controls(window):
            if not cls._usable(control):
                continue
            name = cls._control_name(control)
            automation_id = cls._automation_id(control)
            control_type = cls._control_type(control)
            score = 0
            if automation_id == cls.PROMPT_AUTOMATION_ID:
                score += 20
            if control_type == "Edit":
                score += 5
            if name in {
                "ChatGPT와 채팅",
                "무엇이든 물어보세요",
                f"{cls.PROJECT_NAME}에서 새 채팅",
                "Message ChatGPT",
            }:
                score += 8
            if not score:
                continue
            try:
                bottom = control.rectangle().bottom
            except Exception:
                bottom = 0
            candidates.append((score, bottom, control))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[:2], reverse=True)
        return candidates[0][-1]

    def _open_project(self):
        window = self._open_window()
        project_controls = self._project_controls(window)
        if not project_controls:
            raise RuntimeError(
                "ChatGPT Classic에서 'Phone 미래 전망' 프로젝트를 찾지 못했습니다. "
                "로그인 후 왼쪽 프로젝트 목록에 표시되도록 해주세요."
            )

        project_control = project_controls[0]
        if not self._project_new_chat_is_open(window):
            self._activate_project_control(project_controls[0])
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                self._check_stop()
                window = self._find_window() or window
                refreshed = self._project_controls(window)
                if refreshed:
                    project_control = refreshed[0]
                    break
                time.sleep(0.3)

        window = self._find_window() or window
        refreshed = self._project_controls(window)
        if refreshed:
            project_control = refreshed[0]
        clicked = self._activate_project_new_chat(window, project_control)
        if not clicked and not self._project_new_chat_is_open(window):
            raise RuntimeError(
                "Phone 미래 전망 오른쪽의 새 채팅 아이콘을 누르지 못했습니다."
            )

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self._check_stop()
            window = self._find_window() or window
            if self._project_new_chat_is_open(window):
                break
            time.sleep(0.4)
        else:
            raise RuntimeError(
                "Phone 미래 전망 새 채팅 입력창을 찾지 못했습니다."
            )
        self.log(
            "ChatGPT Classic의 Phone 미래 전망 오른쪽 새 채팅 아이콘을 눌렀습니다."
        )
        return window

    @staticmethod
    def _set_clipboard(text: str) -> None:
        import win32clipboard
        import win32con

        last_error = None
        for _ in range(20):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(
                        text, win32con.CF_UNICODETEXT
                    )
                finally:
                    win32clipboard.CloseClipboard()
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.05)
        raise RuntimeError("Windows 클립보드에 내용을 복사하지 못했습니다.") from last_error

    @staticmethod
    def _get_clipboard() -> str:
        import win32clipboard
        import win32con

        last_error = None
        for _ in range(20):
            try:
                win32clipboard.OpenClipboard()
                try:
                    return str(
                        win32clipboard.GetClipboardData(
                            win32con.CF_UNICODETEXT
                        )
                        or ""
                    )
                finally:
                    win32clipboard.CloseClipboard()
            except Exception as exc:
                last_error = exc
                time.sleep(0.05)
        raise RuntimeError("Windows 클립보드 내용을 읽지 못했습니다.") from last_error

    @classmethod
    def _composer_has_content(cls, composer) -> bool:
        values = []
        for getter in ("get_value", "window_text"):
            try:
                values.append(str(getattr(composer, getter)() or "").strip())
            except Exception:
                continue
        placeholders = {
            "",
            "ChatGPT와 채팅",
            "무엇이든 물어보세요",
            f"{cls.PROJECT_NAME}에서 새 채팅",
            "Message ChatGPT",
        }
        return any(value not in placeholders for value in values)

    def _send_message(self, window, text: str) -> None:
        from pywinauto import keyboard

        composer = self._find_composer(window)
        if composer is None:
            raise RuntimeError("ChatGPT Classic 입력창을 찾지 못했습니다.")
        if self._composer_has_content(composer):
            raise RuntimeError(
                "ChatGPT Classic 입력창에 작성 중인 내용이 있어 덮어쓰지 않았습니다. "
                "입력창을 비운 뒤 다시 실행해 주세요."
            )
        self._set_clipboard(text)
        try:
            composer.click_input()
        except Exception:
            try:
                composer.set_focus()
            except Exception as exc:
                raise RuntimeError("ChatGPT Classic 입력창에 포커스를 둘 수 없습니다.") from exc
        keyboard.send_keys("^v", pause=0.04)
        time.sleep(0.5)
        # 붙여넣은 뒤 contenteditable이 포커스를 넘기는 경우가 있어
        # 입력창을 다시 잡은 다음 Enter를 보낸다.
        composer = self._find_composer(window) or composer
        try:
            composer.click_input()
        except Exception:
            try:
                composer.set_focus()
            except Exception:
                pass
        keyboard.send_keys("{ENTER}", pause=0.04)
        if self._wait_for_submission(window, timeout=4):
            return

        # 일부 ChatGPT Classic 버전은 Enter를 줄바꿈으로 처리한다.
        # 이때만 오른쪽의 전송 버튼을 한 번 눌러 확실히 전송한다.
        send_button = self._find_send_button(window)
        if send_button is None:
            raise RuntimeError(
                "ChatGPT Classic 입력 내용이 전송되지 않았습니다. "
                "입력창의 Enter 전송 설정을 확인해 주세요."
            )
        self._invoke_control(send_button)
        if not self._wait_for_submission(window, timeout=6):
            raise RuntimeError("ChatGPT Classic 메시지 전송을 확인하지 못했습니다.")

    @classmethod
    def _find_send_button(cls, window):
        candidates = []
        known_names = {
            "메시지 보내기",
            "프롬프트 보내기",
            "보내기",
            "send message",
            "send prompt",
            "send",
        }
        for control in cls._controls(window):
            if cls._control_type(control) != "Button" or not cls._usable(control):
                continue
            name = cls._control_name(control).strip().casefold()
            automation_id = cls._automation_id(control).casefold()
            score = 0
            if name in known_names:
                score += 20
            if "send" in automation_id or "submit" in automation_id:
                score += 15
            if not score:
                continue
            try:
                rectangle = control.rectangle()
                score += int(rectangle.right)
                bottom = rectangle.bottom
            except Exception:
                bottom = 0
            candidates.append((score, bottom, control))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[:2], reverse=True)
        return candidates[0][-1]

    def _wait_for_submission(self, window, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_stop()
            window = self._find_window() or window
            if self._generation_is_running(window):
                return True
            composer = self._find_composer(window)
            if composer is None or not self._composer_has_content(composer):
                return True
            time.sleep(0.2)
        return False

    @classmethod
    def _copy_buttons(cls, window) -> list:
        matches = []
        for control in cls._controls(window):
            if cls._control_type(control) != "Button" or not cls._usable(control):
                continue
            name = cls._control_name(control).casefold()
            if (
                "응답 복사" in name
                or name == "복사"
                or "copy response" in name
                or name == "copy"
            ):
                matches.append(control)
        return matches

    @classmethod
    def _copy_tokens(cls, window) -> set[tuple]:
        return {cls._runtime_token(control) for control in cls._copy_buttons(window)}

    @classmethod
    def _generation_is_running(cls, window) -> bool:
        for control in cls._controls(window):
            if cls._control_type(control) != "Button" or not cls._usable(control):
                continue
            name = cls._control_name(control).casefold()
            if (
                ("중지" in name and ("응답" in name or "생성" in name))
                or "stop generating" in name
                or "stop response" in name
            ):
                return True
        return False

    def _wait_for_new_response(
        self,
        window,
        before_tokens: set[tuple],
        timeout_seconds: int,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        stable_since = None
        while time.monotonic() < deadline:
            self._check_stop()
            current_tokens = self._copy_tokens(window)
            has_new_copy = bool(current_tokens - before_tokens)
            running = self._generation_is_running(window)
            if has_new_copy and not running:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 2:
                    return
            else:
                stable_since = None
            time.sleep(1)
        raise TimeoutError("ChatGPT Classic 응답 대기 시간이 초과되었습니다.")

    @classmethod
    def _rerun_controls(cls, window) -> list:
        return [
            control
            for control in cls._controls(window)
            if cls._control_type(control) == "Button"
            and cls._usable(control)
            and cls._control_name(control).strip() == cls.RERUN_TEXT
        ]

    def _trigger_rerun(self, window) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self._check_stop()
            buttons = self._rerun_controls(window)
            if buttons:
                self._invoke_control(buttons[-1])
                time.sleep(0.6)
                composer = self._find_composer(window)
                if composer is not None and self._composer_has_content(composer):
                    from pywinauto import keyboard

                    try:
                        composer.click_input()
                    except Exception:
                        composer.set_focus()
                    keyboard.send_keys("{ENTER}", pause=0.04)
                    if not self._wait_for_submission(window, timeout=4):
                        send_button = self._find_send_button(window)
                        if send_button is None:
                            raise RuntimeError(
                                "개선 지시 메시지를 전송하지 못했습니다."
                            )
                        self._invoke_control(send_button)
                self.log("'지침대로 다시 실행' 버튼을 눌렀습니다.")
                return
            time.sleep(0.5)
        self._send_message(window, self.RERUN_TEXT)
        self.log("'지침대로 다시 실행'을 두 번째 메시지로 전송했습니다.")

    def _copy_latest_response(self, window) -> str:
        buttons = self._copy_buttons(window)
        if not buttons:
            raise RuntimeError("ChatGPT Classic의 최종 응답 복사 버튼을 찾지 못했습니다.")

        def position(control):
            try:
                rectangle = control.rectangle()
                return rectangle.bottom, rectangle.right
            except Exception:
                return 0, 0

        buttons.sort(key=position)
        latest = buttons[-1]
        marker = f"__PICTURE_CLEANER_{uuid.uuid4().hex}__"
        self._set_clipboard(marker)
        self._invoke_control(latest)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self._check_stop()
            copied = self._get_clipboard().strip()
            if copied and copied != marker:
                return copied
            time.sleep(0.2)
        raise RuntimeError("ChatGPT Classic 최종 응답을 클립보드로 복사하지 못했습니다.")

    def open_project(self) -> None:
        with self._com_scope():
            self._open_project()

    def open_project_and_send(self, prompt: str) -> None:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("ChatGPT Classic에 입력할 연관 검색어가 없습니다.")
        with self._com_scope():
            window = self._open_project()
            self._send_message(window, prompt)
            self.log(
                "연관 검색어를 입력하고 Enter 또는 오른쪽 보내기 버튼으로 실행했습니다."
            )

    def generate_phone_future(
        self,
        prompt: str,
        timeout_seconds: int = 420,
    ) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("ChatGPT Classic에 입력할 연관 검색어가 없습니다.")
        with self._com_scope():
            window = self._open_project()
            first_before = self._copy_tokens(window)
            self._send_message(window, prompt)
            self.log("선택한 연관 검색어를 붙여넣고 실행했습니다.")
            self._wait_for_new_response(window, first_before, timeout_seconds)
            self.log("첫 번째 결과를 확인했습니다. 품질 개선을 다시 실행합니다.")

            second_before = self._copy_tokens(window)
            self._trigger_rerun(window)
            self._wait_for_new_response(window, second_before, timeout_seconds)
            result = self._copy_latest_response(window)
            self.log("두 번째 개선 결과를 복사했습니다.")
            return result
