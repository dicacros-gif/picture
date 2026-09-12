"""Bounded local diagnostics for existing UI messages; no request/response capture."""
from __future__ import annotations

from contextlib import contextmanager
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import threading
import traceback


def redact_diagnostic(value) -> str:
    text = str(value)
    # Header contents and URL credentials can contain spaces or several cookies.
    text = re.sub(r"(?im)(\b(?:authorization|set-cookie|cookie)\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)([?&](?:code|access_token|refresh_token|id_token|token|api_key|key|signature|sig)=)[^&#\s]+",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", text)
    names = r"api[_ -]?key|access[_ -]?token|refresh[_ -]?token|id[_ -]?token|token|password|passwd|client[_ -]?secret|secret|authorization|cookie|set-cookie|NID_AUT|NID_SES|NID_JKL|비밀번호"
    text = re.sub(rf'''(?i)(["']?\b(?:{names})\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
                  r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}", "[REDACTED]", text)
    text = re.sub(r"\bAIza[A-Za-z0-9_-]{20,}", "[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text[:32768] + ("\n[진단 메시지 길이 제한]" if len(text) > 32768 else "")


class _RaisingRotatingHandler(RotatingFileHandler):
    def emit(self, record):
        try:
            super().emit(record)
        finally:
            # Windows users/tests may move or inspect logs while the app is
            # open. Do not retain a file handle between individual records.
            if self.stream is not None:
                try:
                    self.stream.close()
                finally:
                    self.stream = None

    def handleError(self, record):
        # Let the owning diagnostics object report one UI warning instead of
        # logging's repeated, potentially unredacted stderr error reports.
        raise


class BlogDiagnostics:
    def __init__(self, path: str | Path, *, max_bytes=2 * 1024 * 1024, backup_count=2):
        self.path = Path(path)
        self.max_bytes, self.backup_count = max_bytes, backup_count
        self._lock = threading.RLock()
        self._handler = None
        self._disabled = self._closed = False

    def write(self, message, *, level=logging.INFO) -> str | None:
        safe = redact_diagnostic(message)
        with self._lock:
            if self._disabled or self._closed:
                return None
            try:
                if self._handler is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._handler = _RaisingRotatingHandler(self.path, maxBytes=self.max_bytes,
                        backupCount=self.backup_count, encoding="utf-8", delay=True)
                    self._handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                                               datefmt="%Y-%m-%d %H:%M:%S"))
                record = logging.LogRecord("Blog", level, "", 0, safe, (), None)
                self._handler.handle(record)
            except Exception as exc:
                self._disabled = True
                self._close_handler()
                return "진단 로그를 저장하지 못했습니다. 화면 로그는 계속 표시합니다: " + redact_diagnostic(exc)
        return None

    def exception(self, label, exc_type, exc_value, exc_traceback) -> str | None:
        # format_exception does not capture frame locals, CLI prompts or responses.
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        return self.write(f"{label}\n{detail}", level=logging.ERROR)

    def _close_handler(self):
        if self._handler is not None:
            try:
                self._handler.close()
            except Exception:
                pass
            self._handler = None

    def close(self):
        with self._lock:
            self._closed = True
            self._close_handler()


@contextmanager
def capture_thread_exceptions(report):
    """Install only around the real app mainloop and restore the previous hook."""
    previous = threading.excepthook
    def hook(arguments):
        name = getattr(arguments.thread, "name", "worker")
        report(f"백그라운드 작업 예외 · {name}", arguments.exc_type, arguments.exc_value, arguments.exc_traceback)
    threading.excepthook = hook
    try:
        yield
    finally:
        if threading.excepthook is hook:
            threading.excepthook = previous
