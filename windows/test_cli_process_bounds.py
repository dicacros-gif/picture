import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import blog_cli_bridge as cli


class CliProcessBoundTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='blog-child-bounds-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def child(self, alive=False):
        child = Mock()
        child.pid, child.returncode = 123456, None if alive else 0
        child.stdin = child.stdout = child.stderr = None
        child.poll.return_value = None if alive else 0
        child.communicate.return_value = (b'answer', b'')
        child.wait.return_value = 0
        return child

    def test_prompt_uses_exact_private_file_bytes_and_closes_after_request(self):
        child = self.child()
        captured = []
        payload = ('한글 $(not-a-command) "quoted"\n' * 10000).encode()
        def launch(_command, **kwargs):
            stream = kwargs['stdin']
            self.assertTrue(stream.seekable())
            self.assertEqual(stream.read(), payload)
            stream.seek(0)
            captured.append(stream)
            self.assertFalse(kwargs['shell'])
            self.assertEqual(kwargs['cwd'], str(self.root))
            return child
        with patch.object(cli.subprocess, 'Popen', side_effect=launch):
            self.assertEqual(cli._run([sys.executable], self.root, stdin=payload), (0, 'answer', ''))
        self.assertTrue(captured[0].closed)
        self.assertTrue(all('input' not in call.kwargs and call.kwargs['timeout'] <= 2
                            for call in child.communicate.call_args_list))

    def test_prompt_file_closes_when_child_cannot_launch(self):
        captured = []
        def launch(_command, **kwargs):
            captured.append(kwargs['stdin'])
            raise OSError('synthetic launch failure')
        with patch.object(cli.subprocess, 'Popen', side_effect=launch), self.assertRaises(cli.BlogCliError) as failure:
            cli._run([sys.executable], self.root, stdin=b'prompt')
        self.assertEqual(failure.exception.code, 'launch_failed')
        self.assertTrue(captured[0].closed)

    def test_timeout_does_not_kill_reused_pid_of_exited_launcher(self):
        child = self.child()
        child.communicate.side_effect = subprocess.TimeoutExpired('owned-child', 2)
        with patch.object(cli.subprocess, 'Popen', return_value=child), \
             patch.object(cli.subprocess, 'run') as taskkill, \
             patch.object(cli.time, 'monotonic', side_effect=[0, 2]), \
             self.assertRaises(cli.BlogCliError) as failure:
            cli._run([sys.executable], self.root, timeout=1)
        self.assertEqual(failure.exception.code, 'timeout')
        taskkill.assert_not_called()
        child.kill.assert_not_called()
        child.communicate.assert_called_once_with(timeout=2)
        child.wait.assert_called_once_with(timeout=2)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows owned process-tree cleanup')
    def test_failed_taskkill_and_drain_remain_bounded_and_keep_original_timeout(self):
        child = self.child(alive=True)
        child.kill.side_effect = lambda: setattr(child.poll, 'return_value', 0)
        child.communicate.side_effect = subprocess.TimeoutExpired('owned-child', 2)
        child.wait.side_effect = subprocess.TimeoutExpired('owned-child', 2)
        with patch.object(cli.subprocess, 'Popen', return_value=child), \
             patch.object(cli.subprocess, 'run', side_effect=subprocess.TimeoutExpired('taskkill', 2)) as taskkill, \
             patch.object(cli.time, 'monotonic', side_effect=[0, 2]), \
             self.assertRaises(cli.BlogCliError) as failure:
            cli._run([sys.executable], self.root, timeout=1)
        self.assertEqual(failure.exception.code, 'timeout')
        self.assertEqual(taskkill.call_args.args[0], ['taskkill.exe', '/PID', str(child.pid), '/T', '/F'])
        self.assertEqual(taskkill.call_args.kwargs['timeout'], 2)
        self.assertFalse(taskkill.call_args.kwargs['shell'])
        child.kill.assert_called_once()
        child.communicate.assert_called_once_with(timeout=2)
        child.wait.assert_called_once_with(timeout=2)

    def test_pipe_close_cannot_wait_on_a_descendant_reader_lock(self):
        release = threading.Event()
        entered = threading.Event()
        class LockedPipe:
            def close(self):
                entered.set()
                release.wait(2)
        child = self.child()
        child.stdout = LockedPipe()
        child.communicate.side_effect = subprocess.TimeoutExpired('owned-child', 2)
        try:
            started = time.monotonic()
            cli._bounded_child_drain(child)
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(entered.wait(.5))
        finally:
            release.set()

    def test_large_unread_prompt_does_not_block_timeout_before_wait_loop(self):
        # This process belongs solely to this test; no installed CLI is invoked.
        started = time.monotonic()
        with self.assertRaises(cli.BlogCliError) as failure:
            cli._run([sys.executable, '-c', 'import time; time.sleep(20)'], self.root,
                     stdin=b'x' * 1_000_000, timeout=.15)
        self.assertEqual(failure.exception.code, 'timeout')
        self.assertLess(time.monotonic() - started, 5)


if __name__ == '__main__':
    unittest.main()
