"""Owned subprocess cleanup; never invokes a real account or model."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
import blog_cli_bridge as bridge


class MacProcessCleanupTests(unittest.TestCase):
    def test_timeout_kills_owned_group_even_if_launcher_already_exited(self):
        child = Mock(pid=71234, returncode=0)
        child.poll.return_value = 0
        child.communicate.return_value = (b"", b"")
        with patch.object(bridge.sys, "platform", "darwin"), \
                patch.object(bridge.signal, "SIGKILL", 9, create=True), \
                patch.object(bridge.subprocess, "Popen", return_value=child) as spawn, \
                patch.object(bridge.os, "killpg", create=True) as killpg, \
                patch.object(bridge.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(bridge.BlogCliError) as error:
                bridge._run(["fake-native-cli"], Path.cwd(), timeout=1)
        self.assertEqual(error.exception.code, "timeout")
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(child.pid, 9)
        child.communicate.assert_called_once_with(timeout=2)

    def test_stuck_pipe_cleanup_remains_bounded(self):
        child = Mock(pid=71234)
        child.poll.return_value = None
        child.communicate.side_effect = subprocess.TimeoutExpired(["fake-native-cli"], 2)
        child.wait.side_effect = subprocess.TimeoutExpired(["fake-native-cli"], 2)
        with patch.object(bridge.sys, "platform", "darwin"), \
                patch.object(bridge.signal, "SIGKILL", 9, create=True), \
                patch.object(bridge.subprocess, "Popen", return_value=child), \
                patch.object(bridge.os, "killpg", create=True), \
                patch.object(bridge.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(bridge.BlogCliError) as error:
                bridge._run(["fake-native-cli"], Path.cwd(), timeout=1)
        self.assertEqual(error.exception.code, "timeout")
        child.wait.assert_called_once_with(timeout=2)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close.assert_called_once()

    @unittest.skipUnless(sys.platform == "darwin", "Actual subprocess tree runs on the Mac build host")
    def test_native_launcher_exit_and_backend_cancel_reap_descendant(self):
        for mode in ("timeout", "cancel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "descendant-done"
                # Exits its parent immediately, retaining inherited stdout in
                # the child just like a Node CLI launcher/native binary chain.
                grandchild = "import pathlib,time;time.sleep(2);pathlib.Path(%r).touch()" % str(marker)
                launcher = "import subprocess,sys;subprocess.Popen([sys.executable,'-c',%r])" % grandchild
                cancel = threading.Event()
                timer = threading.Timer(0.3, cancel.set) if mode == "cancel" else None
                if timer:
                    timer.start()
                started = time.monotonic()
                try:
                    with self.assertRaises(bridge.BlogCliError) as error:
                        bridge._run([sys.executable, "-c", launcher], Path(directory),
                                    timeout=0.4 if mode == "timeout" else 5, cancel_event=cancel)
                    self.assertEqual(error.exception.code, mode if mode == "timeout" else "cancelled")
                    self.assertLess(time.monotonic() - started, 1.5)
                    time.sleep(2)
                    self.assertFalse(marker.exists(), "Native descendant survived the request cleanup")
                finally:
                    if timer:
                        timer.cancel()


if __name__ == "__main__":
    unittest.main()
