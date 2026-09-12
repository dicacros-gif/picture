"""macOS account CLI discovery and interactive login for the shared blog engine.

Finder does not inherit a terminal's PATH. Discover installed executables without
executing shell profiles, shell wrappers or commands restored from settings.
Generation and account checks remain the shared engine's shell-free, stdin-only
calls with API-key environment variables removed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
import uuid

from blog_cli_bridge import API_ENV_KEYS, BlogCliBridge, BlogCliError, PROVIDER_NAMES, _child_environment
from blog_preferences import atomic_json_write


SYSTEM_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
MACHO_MAGIC = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
JS_SUFFIXES = {".js", ".mjs", ".cjs"}


def mac_path_entries(*, home=None, environ=None, system_dirs=None) -> list[Path]:
    """Use explicit installation directories, never source .zshrc/.bash_profile."""
    home = Path(home) if home is not None else Path.home()
    environ = os.environ if environ is None else environ
    supplied = [Path(value) for value in environ.get("PATH", "").split(os.pathsep) if value.strip()]
    extras = [Path(value) for value in (SYSTEM_BIN_DIRS if system_dirs is None else system_dirs)]
    extras += [home / ".local/bin", home / ".npm-global/bin", home / ".volta/bin", home / ".agy/bin"]
    try:
        versions = list((home / ".nvm/versions/node").glob("*/bin"))
        versions.sort(key=lambda path: tuple(int(value) for value in re.findall(r"\d+", path.parent.name)), reverse=True)
        extras += versions
    except OSError:
        pass
    result, seen = [], set()
    for path in [*supplied, *extras]:
        try:
            if not path.is_absolute() or not path.is_dir():
                continue
            resolved = path.resolve()
            if str(resolved) not in seen:
                seen.add(str(resolved))
                result.append(resolved)
        except OSError:
            continue
    return result


def configure_mac_environment(*, home=None, environ=None, system_dirs=None) -> str:
    """Call once at backend startup so inherited generation subprocesses see Node."""
    environment = os.environ if environ is None else environ
    value = os.pathsep.join(str(path) for path in mac_path_entries(home=home, environ=environment, system_dirs=system_dirs))
    environment["PATH"] = value
    return value


def _gui_executable(path: Path) -> bool:
    parts = [part.casefold() for part in path.parts]
    return any(parts[index:index + 2] == ["contents", "macos"] for index in range(len(parts) - 1))


def _native_executable(path: Path) -> bool:
    try:
        if not path.is_file() or not os.access(path, os.X_OK) or _gui_executable(path.resolve()):
            return False
        with path.open("rb") as stream:
            return stream.read(4) in MACHO_MAGIC
    except (OSError, ValueError):
        return False


def _launcher_for(path: Path, node: str = "") -> list[str]:
    try:
        target = path.resolve(strict=True)
        if not target.is_file() or _gui_executable(target):
            return []
        if _native_executable(target):
            return [str(target)]
        # npm creates symlinks to its JavaScript bin entry. Run that through a
        # discovered real Node executable rather than relying on a shell shim.
        with target.open("rb") as stream:
            first_line = stream.readline(256).decode("utf-8", errors="replace").strip()
        if node and (target.suffix.lower() in JS_SUFFIXES or re.fullmatch(r"#!\s*/usr/bin/env\s+node", first_line)):
            return [node, str(target)]
    except (OSError, ValueError):
        pass
    return []


def _package_launcher(root: Path, package_name: str, command: str, node: str) -> list[str]:
    package = root / "node_modules" / package_name
    try:
        value = json.loads((package / "package.json").read_text(encoding="utf-8"))
        entry = value["bin"]
        entry = entry if isinstance(entry, str) else entry[command]
        if not isinstance(entry, str):
            return []
        target = (package / entry).resolve(strict=True)
        if target.is_relative_to(package.resolve()):
            return _launcher_for(target, node)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return []


def _launcher_signature(launcher: list[str]) -> str:
    digest = hashlib.sha256()
    try:
        for value in launcher:
            path = Path(value)
            stat = path.stat()
            digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
        return digest.hexdigest()
    except OSError:
        return ""


def discover_mac_clis(data_dir: Path, *, home=None, environ=None, search_dirs=None, application_dirs=None) -> dict[str, dict]:
    home = Path(home) if home is not None else Path.home()
    environ = os.environ if environ is None else environ
    directories = list(search_dirs) if search_dirs is not None else mac_path_entries(home=home, environ=environ)
    directories = [Path(value) for value in directories]
    apps = [Path(value) for value in application_dirs] if application_dirs is not None else [Path("/Applications"), home / "Applications"]
    node = next((str((path / "node").resolve()) for path in directories if _native_executable(path / "node")), "")
    commands = {"chatgpt": "codex", "claude": "claude", "antigravity": "agy"}
    packages = {"chatgpt": "@openai/codex", "claude": "@anthropic-ai/claude-code"}
    result = {}
    for provider, command in commands.items():
        override = environ.get(f"PICTURE_CLEANER_{provider.upper()}_CLI", "")
        candidates = ([Path(override)] if override and Path(override).is_absolute() else [])
        candidates += [Path(data_dir) / "cli-tools" / command, *(path / command for path in directories)]
        if provider == "chatgpt":
            # Only the separately bundled CLI resource is eligible. Contents/MacOS
            # is the GUI and is rejected even when a PATH symlink points there.
            candidates += [path / "Codex.app/Contents/Resources/codex" for path in apps]
        elif provider == "claude":
            candidates += [home / ".claude/local/claude", home / ".claude/bin/claude"]
        launcher = next((found for path in candidates if (found := _launcher_for(path, node))), [])
        if not launcher and provider in packages:
            roots = [Path(data_dir) / "cli-tools", home / ".npm-global/lib", home / ".claude/local"]
            for directory in directories:
                roots += [directory, directory.parent / "lib"]
            launcher = next((found for root in roots if (found := _package_launcher(root, packages[provider], command, node))), [])
        signature = _launcher_signature(launcher)
        if not signature:
            launcher = []  # An update removed the file during discovery.
        result[provider] = {"id": provider, "name": PROVIDER_NAMES[provider], "installed": bool(launcher),
                            "path": launcher[-1] if launcher else "", "launcher": launcher, "signature": signature,
                            "text_status": "not_checked" if launcher else "not_installed",
                            "image_status": "not_supported" if provider == "claude" else ("not_checked" if launcher else "not_installed"),
                            "text_available": False, "image_available": False,
                            "message": "" if launcher else "macOS CLI를 찾지 못했습니다. 설치 후 다시 확인하세요."}
    return result


class MacLoginSession:
    """Track the CLI's exit marker; /usr/bin/open exiting is NOT a login result."""
    def __init__(self, process, marker: Path, script: Path):
        self.process, self.marker, self.script = process, marker, script
        self.pid = process.pid
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            value = self.marker.read_text(encoding="ascii").strip()
            if re.fullmatch(r"\d{1,3}", value) and 0 <= int(value) <= 255:
                self.returncode = int(value)
        except OSError:
            pass
        opener_code = self.process.poll()
        if self.returncode is None and opener_code not in {None, 0}:
            self.returncode = opener_code
        return self.returncode

    def wait(self, timeout=None):
        started = time.monotonic()
        while self.poll() is None:
            if timeout is not None and time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(["Terminal", str(self.script)], timeout)
            time.sleep(0.1)
        return self.returncode


class MacBlogCliBridge(BlogCliBridge):
    def _login_workspace(self) -> Path:
        root = self.data_dir.resolve()
        workspace = root / "blog-cli-login"
        if workspace.is_symlink() or workspace.resolve().parent != root:
            raise BlogCliError("invalid_login", "CLI 로그인 자료 폴더가 앱 데이터 폴더 밖에 있습니다.")
        return workspace

    def _consume_login_completions(self):
        """Only our completed login scripts can reset an old agy auth failure.

        Status checks run in new backend processes, so the pending receipt is
        durable. Claiming it by rename makes each completion consumable once;
        a later real authentication failure cannot be reset by the old marker.
        """
        workspace = self._login_workspace()
        if not workspace.is_dir():
            return
        for pending in workspace.glob("*.pending.json"):
            try:
                if pending.is_symlink() or pending.stat().st_size > 1024:
                    continue
                value = json.loads(pending.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    continue
                provider = value.get("provider")
                stem = pending.name.removesuffix(".pending.json")
                if (not isinstance(provider, str) or provider not in PROVIDER_NAMES or not stem.startswith(provider + "-")
                        or value.get("marker") != stem + ".exit"):
                    continue
                marker = workspace / value["marker"]
                if marker.is_symlink() or marker.resolve().parent != workspace or marker.stat().st_size > 4:
                    continue
                code = marker.read_text(encoding="ascii").strip()
                if not re.fullmatch(r"\d{1,3}", code) or not 0 <= int(code) <= 255:
                    continue
                claimed = workspace / (stem + "." + uuid.uuid4().hex + ".consumed")
                pending.rename(claimed)
            except (OSError, ValueError, UnicodeError):
                continue  # Still open, partial marker write, or claimed by another check.
            try:
                if provider == "antigravity" and int(code) == 0:
                    self.login_console_closed(provider)
                    self.log("Antigravity 로그인 명령 종료 확인 · 다음 실제 CLI 요청에서 계정을 확인합니다.")
            finally:
                claimed.unlink(missing_ok=True)

    def check_accounts(self) -> dict[str, dict]:
        self._consume_login_completions()
        return super().check_accounts()

    check_status = check_accounts

    def status(self) -> dict[str, dict]:
        discovered = discover_mac_clis(self.data_dir)
        with self._lock:
            for provider, current in discovered.items():
                previous = self._observations.get(provider, {})
                if not (isinstance(previous, dict) and previous.get("path") == current["path"]
                        and previous.get("signature") == current["signature"] and current["installed"]):
                    continue
                for key in ("text_status", "image_status", "text_available", "image_available", "auth_status", "auth_available", "checked_at", "message"):
                    if key in previous:
                        current[key] = previous[key]
                if current.get("auth_status") == "authentication_required":
                    current["text_status"], current["text_available"] = "authentication_required", False
                    if provider != "claude":
                        current["image_status"], current["image_available"] = "authentication_required", False
                elif current.get("auth_status") == "available" and current["text_status"] == "authentication_required":
                    current["text_status"] = "login_verified"
                    if current["image_status"] == "authentication_required":
                        current["image_status"] = "not_checked"
        return discovered

    def open_login(self, provider: str, *, return_process=False, device_auth=False):
        """Explicit login only, in Terminal; never read/copy account credentials."""
        command = self.login_command(provider)
        if device_auth:
            if provider != "chatgpt":
                raise BlogCliError("invalid_login", "기기 코드 로그인은 ChatGPT CLI에서만 지원합니다.", provider=provider)
            command.append("--device-auth")
        workspace = self._login_workspace()
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, filename = tempfile.mkstemp(prefix=f"{provider}-", suffix=".command", dir=workspace)
        script = Path(filename)
        marker = script.with_suffix(".exit")
        pending = script.with_suffix(".pending.json")
        # Terminal can have a different environment from Finder. Reapply PATH and
        # remove API keys in the script itself as well as the launcher process.
        path_value = os.pathsep.join(str(path) for path in mac_path_entries())
        cleared = set(API_ENV_KEYS) | {key for key in os.environ if key.upper() in API_ENV_KEYS}
        cleared = sorted(key for key in cleared if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key))
        lines = ["#!/bin/sh", "umask 077", "unset " + " ".join(cleared), "export PATH=" + shlex.quote(path_value),
                 "cd " + shlex.quote(str(workspace)) + " || exit 1",
                 "printf '%s\\n' " + shlex.quote("본인 구독 계정으로 로그인한 뒤 Blog에서 로그인 상태를 다시 확인하세요."),
                 " ".join(shlex.quote(value) for value in command), "blog_login_exit=$?",
                 "printf '%s\\n' \"$blog_login_exit\" > " + shlex.quote(str(marker)),
                 "printf '%s\\n' " + shlex.quote("로그인 명령이 종료되었습니다. 이 창을 닫고 Blog에서 상태를 다시 확인하세요."),
                 "exit \"$blog_login_exit\"", ""]
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines))
            script.chmod(0o700)
            atomic_json_write(pending, {"provider": provider, "marker": marker.name})
            process = subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(script)], cwd=str(workspace),
                                       env=_child_environment(), shell=False,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            script.unlink(missing_ok=True)
            pending.unlink(missing_ok=True)
            raise BlogCliError("launch_failed", "Terminal 로그인 창을 열지 못했습니다.", provider=provider) from exc
        self.log(f"{PROVIDER_NAMES[provider]} Terminal 로그인 창을 열었습니다. 로그인 완료 후 상태를 다시 확인하세요.")
        session = MacLoginSession(process, marker, script)
        return session if return_process else session.pid
