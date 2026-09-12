from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from blog_diagnostics import BlogDiagnostics, capture_thread_exceptions, redact_diagnostic
from picture_cleaner_pc import PictureCleanerApp


class BlogDiagnosticsTests(unittest.TestCase):
    def test_redacts_credentials_in_headers_urls_json_and_trace_messages(self):
        samples = {
            "Authorization: Bearer fake-header-secret": "fake-header-secret",
            "Cookie: session=fake-cookie-secret; other=also-hidden": "fake-cookie-secret",
            '{"Cookie": "session=fake-json-cookie;other=x"}': "fake-json-cookie",
            "password='fake secret with spaces'": "fake secret with spaces",
            "비밀번호=가짜비밀번호": "가짜비밀번호",
            "https://fakeuser:fakepassword@example.com": "fakepassword",
            "https://example.com/callback?code=fake-code&state=okay": "fake-code",
            "access_token=fake-access-token": "fake-access-token",
            'api_key="fake-api-key"': "fake-api-key",
            "sk-test-abcdefghijklmnopqrstuvwxyz": "sk-test-abcdefghijklmnopqrstuvwxyz",
            "AIza12345678901234567890123456789": "AIza12345678901234567890123456789",
            "NID_AUT=fake-naver-cookie": "fake-naver-cookie",
        }
        for message, secret in samples.items():
            with self.subTest(message=message):
                result = redact_diagnostic(message)
                self.assertNotIn(secret, result)
                self.assertIn("[REDACTED]", result)

    def test_bounded_utf8_rotation_retains_latest_message_and_closes_each_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "blog.log"
            diagnostics = BlogDiagnostics(path, max_bytes=800, backup_count=2)
            try:
                for index in range(40):
                    self.assertIsNone(diagnostics.write(f"회차 {index} · 한글 진행 상태 " + "가" * 60))
                    self.assertIsNone(diagnostics._handler.stream)
                files = sorted(path.parent.glob("blog.log*"))
                self.assertEqual(len(files), 3)
                self.assertIn("회차 39", path.read_text(encoding="utf-8"))
                for item in files:
                    item.read_text(encoding="utf-8")
                # No Windows file handle is held while the application is idle.
                moved = path.with_name("moved.log")
                path.rename(moved)
                self.assertIsNone(diagnostics.write("다음 기록"))
                self.assertTrue(path.exists())
            finally:
                diagnostics.close()

    def test_concurrent_writes_keep_every_complete_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blog.log"
            diagnostics = BlogDiagnostics(path)
            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    warnings = list(executor.map(lambda i: diagnostics.write(f"unique-entry-{i:03d}"), range(100)))
                self.assertTrue(all(warning is None for warning in warnings))
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), 100)
                self.assertEqual({line.split()[-1] for line in lines}, {f"unique-entry-{i:03d}" for i in range(100)})
            finally:
                diagnostics.close()

    def test_logging_failure_warns_ui_once_without_losing_ui_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            app = object.__new__(PictureCleanerApp)
            app.events = queue.Queue()
            app.diagnostics = BlogDiagnostics(Path(directory) / "blog.log")
            with patch("blog_diagnostics._RaisingRotatingHandler.emit", side_effect=PermissionError("file unavailable")) as emit:
                app._naver_log("첫 회차 진행")
                app._naver_log("다음 회차 진행")
                self.assertEqual(emit.call_count, 1)
            events = []
            while not app.events.empty():
                events.append(app.events.get()[1])
            self.assertIn("첫 회차 진행", events)
            self.assertIn("다음 회차 진행", events)
            self.assertEqual(sum("진단 로그를 저장하지 못했습니다" in event for event in events), 1)
            app.diagnostics.close()

    def test_callback_traceback_is_redacted_and_does_not_capture_locals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blog.log"
            app = object.__new__(PictureCleanerApp)
            app.events, app.diagnostics = queue.Queue(), BlogDiagnostics(path)
            private_local = "do-not-collect-frame-local-value"
            try:
                raise RuntimeError("access_token=fake-callback-secret")
            except RuntimeError:
                app._report_uncaught_exception("화면 콜백 예외", *sys.exc_info())
            finally:
                app._close_diagnostics()
            logged = path.read_text(encoding="utf-8")
            self.assertIn("Traceback", logged)
            self.assertIn("화면 콜백 예외", logged)
            self.assertNotIn("fake-callback-secret", logged)
            self.assertNotIn(private_local, logged)
            self.assertNotIn("fake-callback-secret", app.events.get()[1])

    def test_thread_exception_hook_is_scoped_and_restored_without_duplicates(self):
        previous = threading.excepthook
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blog.log"
            app = object.__new__(PictureCleanerApp)
            app.events, app.diagnostics = queue.Queue(), BlogDiagnostics(path)
            def fail():
                raise RuntimeError("token=fake-thread-secret")
            try:
                for _ in range(2):
                    with capture_thread_exceptions(app._report_uncaught_exception):
                        thread = threading.Thread(target=fail, name="diagnostic-test")
                        thread.start()
                        thread.join(timeout=5)
                        self.assertFalse(thread.is_alive())
                    self.assertIs(threading.excepthook, previous)
                self.assertEqual(app.events.qsize(), 2)
                logged = path.read_text(encoding="utf-8")
                self.assertEqual(logged.count("[ERROR]"), 2)
                self.assertNotIn("fake-thread-secret", logged)
            finally:
                app._close_diagnostics()

    def test_close_is_idempotent_and_never_reopens_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blog.log"
            diagnostics = BlogDiagnostics(path)
            diagnostics.write("마지막 진행 상태")
            before = path.read_bytes()
            diagnostics.close()
            diagnostics.close()
            self.assertIsNone(diagnostics.write("종료 뒤 지연 콜백"))
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
