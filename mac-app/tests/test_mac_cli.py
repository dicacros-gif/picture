"""No real CLI calls or account reads; fake Mac installs exercise discovery."""
import copy
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "windows"))
sys.path.insert(0, str(ROOT / "backend"))

import blog_cli_bridge
import mac_cli


class MacCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.data = self.root / "Blog data"
        self.apps = self.root / "Applications"

    def native(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe" + b"mock native executable")
        path.chmod(0o755)
        return path

    def discover(self, **kwargs):
        return mac_cli.discover_mac_clis(self.data, home=self.home, environ={},
                                         search_dirs=[self.bin], application_dirs=[self.apps], **kwargs)

    def test_finder_path_expands_install_locations_without_shell(self):
        paths = [self.home / ".local/bin", self.home / ".npm-global/bin", self.home / ".nvm/versions/node/v9.0.0/bin",
                 self.home / ".nvm/versions/node/v22.4.1/bin"]
        for path in paths:
            path.mkdir(parents=True)
        env = {"PATH": os.pathsep.join([str(self.bin), ".", "relative", str(self.bin)])}
        with patch.object(mac_cli.subprocess, "Popen") as spawn:
            value = mac_cli.configure_mac_environment(home=self.home, environ=env, system_dirs=[])
        spawn.assert_not_called()
        result = value.split(os.pathsep)
        self.assertEqual(result[0], str(self.bin.resolve()))
        self.assertEqual(result.count(str(self.bin.resolve())), 1)
        self.assertNotIn(".", result)
        self.assertIn(str(paths[0].resolve()), result)
        self.assertIn(str(paths[1].resolve()), result)
        self.assertLess(result.index(str(paths[3].resolve())), result.index(str(paths[2].resolve())))
        self.assertEqual(env["PATH"], value)

    def test_native_clis_discovered_and_not_assumed_authenticated(self):
        for command in ("codex", "claude", "agy"):
            self.native(self.bin / command)
        records = self.discover()
        for record in records.values():
            self.assertTrue(record["installed"])
            self.assertFalse(record["text_available"])
            self.assertFalse(record["image_available"])
            self.assertEqual(record["text_status"], "not_checked")
        self.assertEqual(records["claude"]["image_status"], "not_supported")

    def test_gui_binary_and_wrapper_rejected_bundled_resource_accepted(self):
        gui = self.native(self.apps / "Codex.app/Contents/MacOS/Codex")
        (self.bin / "codex").write_text("#!/bin/sh\nopen -a Codex\n", encoding="utf-8")
        records = mac_cli.discover_mac_clis(self.data, home=self.home,
                                            environ={"PICTURE_CLEANER_CHATGPT_CLI": str(gui)},
                                            search_dirs=[self.bin], application_dirs=[self.apps])
        self.assertFalse(records["chatgpt"]["installed"])
        cli = self.native(self.apps / "Codex.app/Contents/Resources/codex")
        self.assertEqual(self.discover()["chatgpt"]["launcher"], [str(cli.resolve())])

    def test_missing_files_and_saved_launcher_are_never_used(self):
        self.data.mkdir()
        (self.data / "blog-cli-capabilities.json").write_text(json.dumps({"chatgpt": {
            "path": "/usr/bin/open", "launcher": ["/bin/sh", "-c", "touch injected"],
            "installed": True, "text_available": True}}), encoding="utf-8")
        bridge = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
        with patch.object(mac_cli, "discover_mac_clis", return_value=self.discover()), patch.object(mac_cli.subprocess, "Popen") as spawn:
            record = bridge.status()["chatgpt"]
        self.assertFalse(record["installed"])
        self.assertFalse(record["text_available"])
        self.assertEqual(record["launcher"], [])
        spawn.assert_not_called()

    def test_npm_javascript_uses_real_node_and_rejects_package_escape(self):
        node = self.native(self.bin / "node")
        package = self.bin.parent / "lib/node_modules/@openai/codex"
        package.mkdir(parents=True)
        script = package / "bin/codex.js"
        script.parent.mkdir()
        script.write_text("#!/usr/bin/env node\n// test", encoding="utf-8")
        manifest = package / "package.json"
        manifest.write_text(json.dumps({"bin": {"codex": "bin/codex.js"}}), encoding="utf-8")
        self.assertEqual(self.discover()["chatgpt"]["launcher"], [str(node.resolve()), str(script.resolve())])
        escape = package.parent / "escape.js"
        escape.write_text("// wrong package", encoding="utf-8")
        manifest.write_text(json.dumps({"bin": {"codex": "../escape.js"}}), encoding="utf-8")
        self.assertFalse(self.discover()["chatgpt"]["installed"])

    def test_symlinkless_npm_js_bin_and_native_runtime_required(self):
        cli = self.bin / "codex"
        cli.write_text("#!/usr/bin/env node\n// npm entry", encoding="utf-8")
        self.assertFalse(self.discover()["chatgpt"]["installed"])
        node = self.native(self.bin / "node")
        self.assertEqual(self.discover()["chatgpt"]["launcher"], [str(node.resolve()), str(cli.resolve())])

    def test_observations_restored_only_for_same_executable_signature(self):
        binary = self.native(self.bin / "codex")
        bridge = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
        discovered = self.discover()
        observation = {**discovered["chatgpt"], "auth_status": "available", "text_status": "available", "text_available": True,
                       "launcher": ["evil saved command"]}
        bridge._observations["chatgpt"] = observation
        with patch.object(mac_cli, "discover_mac_clis", return_value=copy.deepcopy(discovered)):
            current = bridge.status()["chatgpt"]
        self.assertTrue(current["text_available"])
        self.assertEqual(current["launcher"], [str(binary.resolve())])
        binary.write_bytes(binary.read_bytes() + b"update")
        with patch.object(mac_cli, "discover_mac_clis", return_value=self.discover()):
            current = bridge.status()["chatgpt"]
        self.assertFalse(current["text_available"])
        self.assertEqual(current["text_status"], "not_checked")

    def test_auth_failure_overrides_old_capability_success(self):
        self.native(self.bin / "codex")
        bridge = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
        records = self.discover()
        bridge._observations["chatgpt"] = {**records["chatgpt"], "auth_status": "authentication_required",
                                            "text_status": "available", "text_available": True, "image_available": True}
        with patch.object(mac_cli, "discover_mac_clis", return_value=records):
            current = bridge.status()["chatgpt"]
        self.assertFalse(current["text_available"])
        self.assertFalse(current["image_available"])

    def test_login_script_quotes_paths_strips_api_keys_and_waits_for_actual_command(self):
        binary = self.native(self.bin / "codex odd'$(wrong)")
        bridge = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
        opener = Mock(pid=44)
        opener.poll.return_value = 0
        with patch.object(bridge, "login_command", return_value=[str(binary), "login"]), \
                patch.object(mac_cli, "mac_path_entries", return_value=[self.bin]), \
                patch.object(mac_cli.subprocess, "Popen", return_value=opener) as spawn, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-copy-this-secret"}):
            session = bridge.open_login("chatgpt", return_process=True, device_auth=True)
        contents = session.script.read_text(encoding="utf-8")
        self.assertIn(shlex.quote(str(binary)) + " login --device-auth", contents)
        self.assertIn("cd " + shlex.quote(str(session.script.parent)), contents)
        self.assertIn("unset ", contents)
        self.assertNotIn("do-not-copy-this-secret", contents)
        self.assertNotIn("--with-api-key", contents)
        self.assertNotIn("OPENAI_API_KEY", spawn.call_args.kwargs["env"])
        self.assertEqual(spawn.call_args.args[0], ["/usr/bin/open", "-a", "Terminal", str(session.script)])
        self.assertFalse(spawn.call_args.kwargs["shell"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(session.script.stat().st_mode), 0o700)
        self.assertIsNone(session.poll(), "Launch Services exits before the login process")
        session.marker.write_text("0\n", encoding="ascii")
        self.assertEqual(session.poll(), 0)
        self.assertEqual(bridge._observations, {}, "A completed login must still be checked")

    def test_login_opener_failure_and_timeout_are_not_success(self):
        process = Mock(pid=2)
        process.poll.return_value = 1
        session = mac_cli.MacLoginSession(process, self.root / "never.exit", self.root / "script.command")
        self.assertEqual(session.poll(), 1)
        process.poll.return_value = 0
        session = mac_cli.MacLoginSession(process, self.root / "never.exit", self.root / "script.command")
        with self.assertRaises(subprocess.TimeoutExpired):
            session.wait(timeout=0)

    def test_inherited_account_check_does_not_run_generation_or_copy_credentials(self):
        self.native(self.bin / "codex")
        bridge = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
        records = self.discover()
        with patch.object(mac_cli, "discover_mac_clis", side_effect=lambda directory: copy.deepcopy(records)), \
                patch.object(blog_cli_bridge, "_run", return_value=(0, "Logged in using ChatGPT", "")) as run:
            result = bridge.check_accounts()
        self.assertEqual(run.call_args.args[0][-2:], ["login", "status"])
        self.assertTrue(result["chatgpt"]["auth_available"])
        self.assertFalse(result["chatgpt"]["text_available"])
        self.assertNotIn("launcher", json.loads((self.data / "blog-cli-capabilities.json").read_text())["chatgpt"])

    def test_api_key_login_is_not_accepted_as_subscription_authentication(self):
        self.native(self.bin / "codex")
        bridge = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
        records = self.discover()
        with patch.object(mac_cli, "discover_mac_clis", side_effect=lambda directory: copy.deepcopy(records)), \
                patch.object(blog_cli_bridge, "_run", return_value=(0, "Logged in using an API key", "")):
            result = bridge.check_accounts()
        self.assertEqual(result["chatgpt"]["auth_status"], "authentication_required")
        self.assertFalse(result["chatgpt"]["auth_available"])

    def test_login_launch_exception_removes_the_temporary_command(self):
        bridge = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
        with patch.object(bridge, "login_command", return_value=["/opt/homebrew/bin/codex", "login"]), \
                patch.object(mac_cli, "mac_path_entries", return_value=[self.bin]), \
                patch.object(mac_cli.subprocess, "Popen", side_effect=OSError("Terminal unavailable")):
            with self.assertRaises(mac_cli.BlogCliError) as error:
                bridge.open_login("chatgpt")
        self.assertEqual(error.exception.code, "launch_failed")
        self.assertEqual(list((self.data / "blog-cli-login").glob("*.command")), [])
        self.assertEqual(list((self.data / "blog-cli-login").glob("*.pending.json")), [])

    def test_agy_login_receipt_survives_backend_restart_and_is_consumed_once(self):
        self.native(self.bin / "agy")
        records = self.discover()
        process = Mock(pid=2048)
        process.poll.return_value = 0
        with patch.object(mac_cli, "discover_mac_clis", side_effect=lambda directory: copy.deepcopy(records)), \
                patch.object(mac_cli, "mac_path_entries", return_value=[self.bin]), \
                patch.object(mac_cli.subprocess, "Popen", return_value=process), \
                patch.object(blog_cli_bridge, "_run", return_value=(0, "", "")) as run:
            original = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
            auth_error = blog_cli_bridge.BlogCliError("authentication_required", "Please log in")
            original._record("antigravity", "auth", auth_error)
            session = original.open_login("antigravity", return_process=True)
            pending = session.script.with_suffix(".pending.json")
            self.assertEqual(json.loads(pending.read_text(encoding="utf-8")),
                             {"provider": "antigravity", "marker": session.marker.name})
            session.marker.write_text("0\n", encoding="ascii")
            resumed = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
            result = resumed.check_accounts()["antigravity"]
            self.assertEqual(result["auth_status"], "not_checked")
            self.assertFalse(result["auth_available"], "CLI exit zero is not authenticated-account evidence")
            self.assertFalse(pending.exists())
            self.assertTrue(session.marker.exists(), "Only pending ownership receipt is consumed")
            # An actual later auth failure cannot be cleared by the retained
            # successful marker when the user presses recheck again.
            resumed._record("antigravity", "auth", auth_error)
            another = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
            self.assertEqual(another.check_status()["antigravity"]["auth_status"], "authentication_required")
        self.assertTrue(all(call.args[0][-1] == "--help" for call in run.call_args_list))

    def test_open_or_failed_agy_login_does_not_reset_authentication_failure(self):
        self.native(self.bin / "agy")
        records = self.discover()
        process = Mock(pid=2048)
        process.poll.return_value = 0
        with patch.object(mac_cli, "discover_mac_clis", side_effect=lambda directory: copy.deepcopy(records)), \
                patch.object(mac_cli, "mac_path_entries", return_value=[self.bin]), \
                patch.object(mac_cli.subprocess, "Popen", return_value=process), \
                patch.object(blog_cli_bridge, "_run", return_value=(0, "", "")):
            original = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
            original._record("antigravity", "auth", blog_cli_bridge.BlogCliError("authentication_required", "Please log in"))
            session = original.open_login("antigravity", return_process=True)
            pending = session.script.with_suffix(".pending.json")
            self.assertEqual(mac_cli.MacBlogCliBridge(self.data, lambda value: None).check_accounts()
                             ["antigravity"]["auth_status"], "authentication_required")
            self.assertTrue(pending.exists(), "The still-open console must remain pending")
            session.marker.write_text("1\n", encoding="ascii")
            self.assertEqual(mac_cli.MacBlogCliBridge(self.data, lambda value: None).check_accounts()
                             ["antigravity"]["auth_status"], "authentication_required")
            self.assertFalse(pending.exists())

    def test_login_receipt_cannot_point_outside_its_own_workspace(self):
        self.native(self.bin / "agy")
        records = self.discover()
        process = Mock(pid=2048)
        with patch.object(mac_cli, "discover_mac_clis", side_effect=lambda directory: copy.deepcopy(records)), \
                patch.object(mac_cli, "mac_path_entries", return_value=[self.bin]), \
                patch.object(mac_cli.subprocess, "Popen", return_value=process), \
                patch.object(blog_cli_bridge, "_run", return_value=(0, "", "")):
            original = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
            original._record("antigravity", "auth", blog_cli_bridge.BlogCliError("authentication_required", "Please log in"))
            session = original.open_login("antigravity", return_process=True)
            (self.data / "unrelated.exit").write_text("0\n", encoding="ascii")
            pending = session.script.with_suffix(".pending.json")
            pending.write_text(json.dumps({"provider": "antigravity", "marker": "../unrelated.exit"}), encoding="utf-8")
            resumed = mac_cli.MacBlogCliBridge(self.data, lambda value: None)
            self.assertEqual(resumed.check_accounts()["antigravity"]["auth_status"], "authentication_required")
            self.assertTrue(pending.exists())

    def test_inherits_existing_text_image_validation_and_permissions(self):
        self.assertIs(mac_cli.MacBlogCliBridge.run_text, blog_cli_bridge.BlogCliBridge.run_text)
        self.assertIs(mac_cli.MacBlogCliBridge.generate_image, blog_cli_bridge.BlogCliBridge.generate_image)
        self.assertIs(mac_cli.MacBlogCliBridge._request, blog_cli_bridge.BlogCliBridge._request)


if __name__ == "__main__":
    unittest.main()
