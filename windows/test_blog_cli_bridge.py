from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

import blog_cli_bridge as cli


def events(*items):
    return "\n".join(json.dumps(item) for item in items)


class BlogCliBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="blog-cli-test-")
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.bridge = cli.BlogCliBridge(self.root, lambda message: None)

    def provider(self, provider="chatgpt"):
        return {"id": provider, "name": cli.PROVIDER_NAMES[provider], "installed": True,
                "path": sys.executable, "launcher": [sys.executable], "signature": "test",
                "text_status": "not_checked", "image_status": "not_checked",
                "text_available": False, "image_available": False, "message": ""}

    def image(self, name="image.png", root=None):
        path = (root or self.root) / name
        Image.effect_noise((1024, 768), 24).convert("RGB").save(path)
        return path

    def test_api_environment_removed_without_changing_parent(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-forward", "ANTHROPIC_API_KEY": "private",
                                     "GEMINI_API_KEY": "private", "ANTHROPIC_AUTH_TOKEN": "private",
                                     "CODEX_API_KEY": "private", "OPENAI_BASE_URL": "https://invalid.test",
                                     "CLAUDE_CODE_USE_VERTEX": "1", "CODEX_HOME": str(self.root),
                                     "SOME_OTHER_SETTING": "keep"}):
            result = cli._child_environment()
            self.assertFalse(cli.API_ENV_KEYS.intersection(result))
            self.assertEqual(result["CODEX_HOME"], str(self.root))
            self.assertEqual(result["SOME_OTHER_SETTING"], "keep")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "do-not-forward")

    def test_antigravity_login_console_clears_stale_auth_failure_without_claiming_success(self):
        with patch.object(cli, "_discover", side_effect=lambda _path: {"antigravity": self.provider("antigravity")}):
            self.bridge._record("antigravity", "auth", cli.BlogCliError("authentication_required", "old login failed"))
            self.assertEqual(self.bridge.status()["antigravity"]["text_status"], "authentication_required")
            self.bridge.login_console_closed("antigravity")
            refreshed = cli.BlogCliBridge(self.root, lambda _: None).status()["antigravity"]
            self.assertEqual(refreshed["auth_status"], "not_checked")
            self.assertEqual(refreshed["text_status"], "not_checked")
            self.assertFalse(refreshed["auth_available"])
            self.assertFalse(refreshed["text_available"])

    def test_login_console_returns_handle_for_monitor_or_legacy_pid_without_shell(self):
        with patch.object(self.bridge, "login_command", return_value=[sys.executable, "login"]), \
             patch.object(cli.subprocess, "Popen") as start:
            start.return_value.pid = 123
            self.assertIs(self.bridge.open_login("claude", return_process=True), start.return_value)
            self.assertEqual(self.bridge.open_login("claude"), 123)
        self.assertFalse(start.call_args.kwargs["shell"])
        self.assertFalse(cli.API_ENV_KEYS.intersection(start.call_args.kwargs["env"]))

    def test_runner_preserves_stdin_shell_characters_and_unicode(self):
        value = '한글 prompt $(whoami) & | > "quoted" `literal`\nnext line'
        code, output, error = cli._run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                                       self.root, stdin=value.encode(), timeout=10)
        self.assertEqual((code, output, error), (0, value, ""))

    def test_runner_cancelled_before_launch(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(cli.subprocess, "Popen") as start, self.assertRaises(cli.BlogCliError) as result:
            cli._run([sys.executable], self.root, cancel_event=cancel)
        self.assertEqual(result.exception.code, "cancelled")
        start.assert_not_called()

    def test_runner_timeout_is_bounded(self):
        started = time.monotonic()
        with self.assertRaises(cli.BlogCliError) as result:
            cli._run([sys.executable, "-c", "import time; time.sleep(20)"], self.root, timeout=0.3)
        self.assertEqual(result.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 5)

    def test_runner_never_launches_cmd_wrapper(self):
        with self.assertRaises(cli.BlogCliError) as result:
            cli._run(["untrusted.cmd", "& whoami"], self.root)
        self.assertEqual(result.exception.code, "unsafe_launcher")

    def test_npm_entry_rejects_manifest_traversal(self):
        package = self.root / "node_modules/@openai/codex"
        package.mkdir(parents=True)
        (self.root / "outside.js").write_text("do not run", encoding="utf-8")
        (package / "package.json").write_text(json.dumps({"bin": {"codex": "../../../outside.js"}}), encoding="utf-8")
        self.assertEqual(cli._npm_launcher(self.root, "@openai/codex", "codex", "node.exe"), [])

    def test_npm_entry_uses_node_without_shell(self):
        package = self.root / "node_modules/@openai/codex"
        package.mkdir(parents=True)
        entry = package / "cli.js"
        entry.write_text("", encoding="utf-8")
        (package / "package.json").write_text(json.dumps({"bin": {"codex": "cli.js"}}), encoding="utf-8")
        self.assertEqual(cli._npm_launcher(self.root, "@openai/codex", "codex", "node.exe"), ["node.exe", str(entry)])

    def discovery_file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test executable; must never launch")
        return path

    def discover_chatgpt(self, located=None, override="", node=None):
        environment = {"LOCALAPPDATA": str(self.root / "localapp"), "APPDATA": str(self.root / "appdata"),
                       "PICTURE_CLEANER_CHATGPT_CLI": str(override)}
        with patch.dict(os.environ, environment), patch.object(Path, "home", return_value=self.root / "home"), \
             patch.object(cli.shutil, "which", side_effect=lambda name: {"codex": str(located) if located else None,
                                                                        "node": node}.get(name)), \
             patch.object(cli.subprocess, "Popen") as start:
            result = cli._discover(self.root)["chatgpt"]
            start.assert_not_called()
            return result

    def test_discovery_prefers_native_codex_over_desktop_path(self):
        gui = self.discovery_file("WindowsApps/OpenAI.Codex_26.901_x64/app/Codex.exe")
        native = self.discovery_file("localapp/OpenAI/Codex/bin/native-version/codex.exe")
        result = self.discover_chatgpt(gui)
        self.assertEqual(result["launcher"], [str(native.resolve())])
        self.assertTrue(result["installed"])

    def test_discovery_orders_native_hash_versions_by_file_modification_time(self):
        older = self.discovery_file("localapp/OpenAI/Codex/bin/zzz-old/codex.exe")
        newer = self.discovery_file("localapp/OpenAI/Codex/bin/aaa-new/codex.exe")
        os.utime(older, ns=(1_000_000_000, 1_000_000_000))
        os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
        other = self.discovery_file("custom-path/codex.exe")
        self.assertEqual(self.discover_chatgpt(other)["launcher"], [str(newer.resolve())])

    def test_discovery_preserves_native_override_but_rejects_desktop_override(self):
        gui = self.discovery_file("WindowsApps/OpenAI.Codex_26.901_x64/app/Codex.exe")
        native = self.discovery_file("localapp/OpenAI/Codex/bin/version/codex.exe")
        override = self.discovery_file("chosen-cli/codex.exe")
        self.assertEqual(self.discover_chatgpt(gui, override)["launcher"], [str(override.resolve())])
        self.assertEqual(self.discover_chatgpt(gui, gui)["launcher"], [str(native.resolve())])

    def test_discovery_desktop_or_alias_alone_never_counts_as_cli(self):
        for relative in ("WindowsApps/OpenAI.Codex_26.901_x64/app/Codex.exe",
                         "localapp/Microsoft/WindowsApps/codex.exe"):
            with self.subTest(relative=relative):
                gui = self.discovery_file(relative)
                result = self.discover_chatgpt(gui, gui)
                self.assertFalse(result["installed"])
                self.assertEqual(result["launcher"], [])

    def test_discovery_desktop_path_still_allows_npm_native_launcher_fallback(self):
        gui = self.discovery_file("WindowsApps/OpenAI.Codex_26.901_x64/app/Codex.exe")
        entry = self.discovery_file("appdata/npm/node_modules/@openai/codex/cli.js")
        (entry.parent / "package.json").write_text(json.dumps({"bin": {"codex": "cli.js"}}), encoding="utf-8")
        result = self.discover_chatgpt(gui, node="node.exe")
        self.assertEqual(result["launcher"], ["node.exe", str(entry.resolve())])

    def test_codex_uses_last_completed_message(self):
        response = cli._parse_response("chatgpt", events(
            {"type": "thread.started", "thread_id": str(uuid.uuid4())},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Progress"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"title":"완성"}'}},
            {"type": "turn.completed"}), "", 0)
        self.assertEqual(response.answer, '{"title":"완성"}')
        self.assertFalse(response.image_tool_succeeded)

    def test_codex_failed_turn_never_returns_prior_answer(self):
        with self.assertRaises(cli.BlogCliError) as result:
            cli._parse_response("chatgpt", events(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Partial draft"}},
                {"type": "turn.failed", "error": {"message": "quota exceeded"}}), "", 0)
        self.assertEqual(result.exception.code, "quota_limited")

    def test_antigravity_uses_generation_tool_path(self):
        conversation = str(uuid.uuid4())
        response = cli._parse_response("antigravity", events(
            {"event": "init", "conversation_id": conversation},
            {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_name": "generate_image",
                "tool_info": {"output": "Generated image is saved at C:/own/current-image.png.\n"}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": '{"image_path":"invented.png"}'}}), "", 0)
        self.assertTrue(response.image_tool_succeeded)
        self.assertEqual(response.files, ["C:/own/current-image.png"])
        self.assertEqual(response.roots[0].name, conversation)

    def test_generation_tool_error_is_not_success(self):
        response = cli._parse_response("antigravity", events(
            {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_name": "generate_image",
                "tool_info": {"error": "denied", "output": "Generated image is saved at fake.png.\n"}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "No image available"}}), "", 0)
        self.assertFalse(response.image_tool_succeeded)

    def test_permission_failure_is_meaningful(self):
        with self.assertRaises(cli.BlogCliError) as result:
            cli._parse_response("antigravity", events({"event": "result", "result": {"status": "ERROR", "error": "headless mode cannot prompt, auto-denied"}}), "", 1)
        self.assertEqual(result.exception.code, "permission_required")

    def test_claude_result_schema(self):
        response = cli._parse_response("claude", events({"type": "assistant", "message": {"content": [{"type": "text", "text": "Partial"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Final review"}), "", 0)
        self.assertEqual(response.answer, "Final review")

    def test_malformed_conversation_id_is_rejected(self):
        with self.assertRaises(cli.BlogCliError):
            cli._parse_response("antigravity", events({"event": "init", "conversation_id": "../../elsewhere"},
                {"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}), "", 0)

    def test_valid_image_freshness_and_root(self):
        path = self.image()
        width, height, digest = cli._validated_image(path, [self.root], time.time())
        self.assertEqual((width, height), (1024, 768))
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaises(cli.BlogCliError):
            cli._validated_image(path, [self.root / "other"], time.time())
        os.utime(path, (time.time() - 60, time.time() - 60))
        with self.assertRaises(cli.BlogCliError):
            cli._validated_image(path, [self.root], time.time())

    def test_invalid_image_cannot_pass_by_extension(self):
        path = self.root / "invalid.png"
        path.write_bytes(b"not an image" * 1000)
        with self.assertRaises(cli.BlogCliError):
            cli._validated_image(path, [self.root], time.time())

    def test_low_resolution_image_rejected(self):
        path = self.root / "small.png"
        Image.effect_noise((256, 256), 32).save(path)
        with self.assertRaises(cli.BlogCliError):
            cli._validated_image(path, [self.root], time.time())

    def test_codex_only_reads_this_conversation_native_evidence(self):
        conversation, other = str(uuid.uuid4()), str(uuid.uuid4())
        started = time.time()
        sessions = self.root / "sessions" / time.strftime("%Y/%m/%d")
        sessions.mkdir(parents=True)
        images = self.root / "generated_images" / conversation
        images.mkdir(parents=True)
        artifact = self.image(root=images)
        native = events(
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "image-1",
             "input": "const result = await tools.image_gen__imagegen({prompt:'test'}); generatedImage(result);"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "image-1",
             "output": [{"type": "input_text", "text": 'Script completed data:image/png;base64,xyz Generated images are saved'}]}})
        (sessions / f"rollout-{other}.jsonl").write_text(native, encoding="utf-8")
        response = cli._Response(conversation_id=conversation, roots=[images])
        with patch.object(cli, "_codex_home", return_value=self.root):
            cli._codex_image_evidence(response, started)
            self.assertFalse(response.image_tool_succeeded)
            (sessions / f"rollout-{conversation}.jsonl").write_text(native, encoding="utf-8")
            cli._codex_image_evidence(response, started)
        self.assertTrue(response.image_tool_succeeded)
        self.assertEqual(response.files, [str(artifact)])

    def test_codex_invented_image_path_has_no_tool_evidence(self):
        conversation = str(uuid.uuid4())
        directory = self.root / "sessions" / time.strftime("%Y/%m/%d")
        directory.mkdir(parents=True)
        (directory / f"rollout-{conversation}.jsonl").write_text(events({"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "image generated successfully fake.png"}]}}), encoding="utf-8")
        response = cli._Response(conversation_id=conversation)
        with patch.object(cli, "_codex_home", return_value=self.root):
            cli._codex_image_evidence(response, time.time())
        self.assertFalse(response.image_tool_succeeded)

    def test_generate_requires_tool_evidence_before_artifact(self):
        with patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(self.bridge, "_request", return_value=cli._Response(answer='{"image_path":"fake.png"}')), \
             patch.object(self.bridge, "_record"), self.assertRaises(cli.BlogCliError) as result:
            self.bridge.generate_image("antigravity", "A scene", self.root / "out")
        self.assertEqual(result.exception.code, "image_unavailable")

    def test_generate_validates_and_copies_actual_artifact(self):
        source_bytes = []
        def generate(provider, prompt, workspace, **kwargs):
            path = self.image(root=workspace)
            source_bytes.append(path.read_bytes())
            return cli._Response(answer="generated", files=[str(path)], image_tool_succeeded=True)
        with patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(self.bridge, "_request", side_effect=generate), patch.object(self.bridge, "_record"):
            result = self.bridge.generate_image("antigravity", "A scene", self.root / "out")
        self.assertEqual(Path(result["path"]).read_bytes(), source_bytes[0])
        self.assertEqual(result["sha256"], hashlib.sha256(source_bytes[0]).hexdigest())
        self.assertTrue(result["native_tool_verified"])

    def test_codex_request_has_stdin_images_and_read_only_policy(self):
        artifact = self.image()
        captured = {}
        def run(command, cwd, **kwargs):
            captured.update(command=command, stdin=kwargs["stdin"])
            return 0, events({"type": "item.completed", "item": {"type": "agent_message", "text": "review"}}, {"type": "turn.completed"}), ""
        with patch.object(self.bridge, "_provider", return_value=self.provider()), patch.object(self.bridge, "_require_account_login"), patch.object(cli, "_run", side_effect=run):
            self.bridge._request("chatgpt", "special & text", self.root, model="", images=[artifact], image=False, timeout=30)
        command = captured["command"]
        self.assertIn("--image", command)
        self.assertIn(str(artifact), command)
        self.assertIn("read-only", command)
        self.assertEqual(captured["stdin"], b"special & text")
        self.assertNotIn("special & text", command)
        self.assertFalse(any("dangerously" in arg for arg in command))

    def test_claude_text_allows_only_read_only_research_tools(self):
        with patch.object(self.bridge, "_provider", return_value=self.provider("claude")), patch.object(self.bridge, "_require_account_login"), \
             patch.object(cli, "_run", return_value=(0, events({"type": "result", "subtype": "success", "result": "review"}), "")) as runner:
            self.bridge._request("claude", "verify sources", self.root, model="", images=None, image=False, timeout=30)
        args = runner.call_args.args[0]
        self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertFalse(any("skip-permissions" in arg or "bypass" in arg for arg in args))

    def test_antigravity_vision_requires_all_native_image_reads(self):
        artifact = self.image()
        with patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(cli, "_run", return_value=(0, events({"event": "result", "result": {"status": "SUCCESS", "response": "looks fine"}}), "")), \
             self.assertRaises(cli.BlogCliError) as result:
            self.bridge._request("antigravity", "review", self.root, model="", images=[artifact], image=False, timeout=30)
        self.assertEqual(result.exception.code, "vision_unavailable")

    def test_codex_api_auth_rejected(self):
        with patch.object(cli, "_run", return_value=(0, "", "Logged in using an API key")), self.assertRaises(cli.BlogCliError) as result:
            self.bridge._require_account_login("chatgpt", ["codex.exe"], self.root)
        self.assertEqual(result.exception.code, "authentication_required")

    def test_claude_oauth_only_and_no_credential_output(self):
        with patch.object(cli, "_run", return_value=(0, json.dumps({"loggedIn": True, "authMethod": "api_key", "apiKey": "never-print"}), "")), self.assertRaises(cli.BlogCliError) as result:
            self.bridge._require_account_login("claude", ["claude.exe"], self.root)
        self.assertNotIn("never-print", str(result.exception))

    def test_capability_cache_does_not_restore_changed_executable_or_launcher(self):
        current = {"chatgpt": self.provider()}
        self.bridge._observations = {"chatgpt": {**self.provider(), "signature": "old", "image_status": "available",
                                               "image_available": True, "launcher": ["evil.cmd"]}}
        with patch.object(cli, "_discover", return_value=current):
            result = self.bridge.status()
        self.assertFalse(result["chatgpt"]["image_available"])
        self.assertEqual(result["chatgpt"]["launcher"], [sys.executable])

    def test_model_argument_control_characters_rejected(self):
        with patch.object(self.bridge, "_provider", return_value=self.provider()), self.assertRaises(cli.BlogCliError) as result:
            self.bridge._request("chatgpt", "hello", self.root, model="model\n--dangerously-bypass-approvals-and-sandbox", images=None, image=False, timeout=30)
        self.assertEqual(result.exception.code, "invalid_model")

    def test_recent_auth_failure_overrides_historical_capabilities(self):
        self.bridge._observations = {"chatgpt": {**self.provider(), "text_status": "available", "text_available": True,
            "image_status": "available", "image_available": True, "auth_status": "authentication_required"}}
        with patch.object(cli, "_discover", return_value={"chatgpt": self.provider()}):
            status = self.bridge.status()["chatgpt"]
        self.assertEqual(status["text_status"], "authentication_required")
        self.assertFalse(status["text_available"])
        self.assertEqual(status["image_status"], "authentication_required")
        self.assertFalse(status["image_available"])

    def test_research_hosts_reject_local_credentials_and_wildcards(self):
        self.assertEqual(cli._prompt_public_hosts('Read https://www.nts.go.kr/page and https://support.apple.com/a'),
                         {"nts.go.kr", "support.apple.com"})
        for url in ("file:///C:/secret", "https://127.0.0.1/", "https://[::1]/", "http://localhost/",
                    "https://work.internal/", "https://user:secret@nts.go.kr/", "https://*.go.kr/",
                    "https://nts.go.kr:9999/", "https://0x7f000001/"):
            with self.subTest(url=url):
                self.assertEqual(cli._public_read_host(url), "")

    def test_scoped_research_retries_only_new_public_host_and_cleans_own_project(self):
        project_dir = self.root / ".gemini/config/projects"
        project_dir.mkdir(parents=True)
        existing = project_dir / "default-cli-project.json"
        existing.write_text('{"id":"default-cli-project"}', encoding="utf-8")
        observed, requests = [], []
        conversation_id = str(uuid.uuid4())
        def run(args, cwd, **kwargs):
            project_id = args[args.index("--project") + 1]
            value = json.loads((project_dir / f"{project_id}.json").read_text(encoding="utf-8"))
            observed.append(value["permissionGrants"]["permissionGrants"])
            requests.append((list(args), kwargs["stdin"], kwargs["timeout"]))
            self.assertEqual(cwd, self.root)
            self.assertFalse(any("skip-permissions" in arg or "bypass" in arg for arg in args))
            if len(observed) == 1:
                return 0, events({"event": "step_update", "step_update": {"state": "ERROR", "tool_name": "read_url_content",
                    "tool_info": {"parameters": {"Url": "https://www.nts.go.kr/primary-source"}}}},
                    {"event": "result", "result": {"conversation_id": conversation_id, "status": "SUCCESS", "response": "", "denied_actions": [{"action": "read_url"}]}}), ""
            return 0, events({"event": "init", "conversation_id": conversation_id},
                             {"event": "result", "result": {"conversation_id": conversation_id, "status": "SUCCESS", "response": "verified"}}), ""
        with patch.object(cli.Path, "home", return_value=self.root), patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(cli, "_run", side_effect=run):
            result = self.bridge._request("antigravity", "Discover primary sources", self.root, model="", images=None, image=False, timeout=30)
        self.assertEqual(result.answer, "verified")
        self.assertEqual(observed, [{"allow": [], "deny": [], "ask": []}, {"allow": ["read_url(nts.go.kr)"], "deny": [], "ask": []}])
        self.assertEqual(list(project_dir.iterdir()), [existing])
        self.assertEqual(existing.read_text(encoding="utf-8"), '{"id":"default-cli-project"}')
        self.assertNotIn("--conversation", requests[0][0])
        self.assertEqual(requests[1][0][-2:], ["--conversation", conversation_id])
        self.assertNotIn("--continue", requests[1][0])
        self.assertLessEqual(requests[1][2], requests[0][2])
        self.assertNotEqual(requests[0][1], requests[1][1])
        resume_text = json.loads(requests[1][1])["message"]["content"][0]["text"]
        self.assertIn("original requested format", resume_text)
        self.assertIn("previously verified research", resume_text)
        self.assertIn("nts.go.kr", resume_text)

    def test_resume_requires_own_valid_consistent_emitted_id(self):
        valid = str(uuid.uuid4())
        for identity_events in ([], [{"event": "init", "conversation_id": "--continue"}],
                                [{"event": "init", "conversation_id": valid}, {"event": "init", "conversation_id": str(uuid.uuid4())}]):
            output = events(*identity_events, {"event": "step_update", "step_update": {"state": "ERROR", "tool_name": "read_url_content",
                "tool_info": {"parameters": {"Url": "https://nts.go.kr/page"}}}},
                {"event": "result", "result": {"status": "SUCCESS", "response": "", "denied_actions": [{"action": "read_url"}]}})
            with self.subTest(identity_events=identity_events), patch.object(cli.Path, "home", return_value=self.root), \
                 patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
                 patch.object(cli, "_run", return_value=(0, output, "")) as runner, self.assertRaises(cli.BlogCliError) as result:
                self.bridge._request("antigravity", "Research", self.root, model="", images=None, image=False, timeout=30)
            self.assertEqual(result.exception.code, "invalid_response")
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(list((self.root / ".gemini/config/projects").iterdir()), [])

    def test_resume_rejects_cli_silently_starting_another_conversation(self):
        conversation_id = str(uuid.uuid4())
        first = events({"event": "init", "conversation_id": conversation_id},
            {"event": "step_update", "step_update": {"state": "ERROR", "tool_name": "read_url_content",
                "tool_info": {"parameters": {"Url": "https://nts.go.kr/page"}}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "", "denied_actions": [{"action": "read_url"}]}})
        second = events({"event": "init", "conversation_id": str(uuid.uuid4())},
                        {"event": "result", "result": {"status": "SUCCESS", "response": "unrelated answer"}})
        with patch.object(cli.Path, "home", return_value=self.root), \
             patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(cli, "_run", side_effect=[(0, first, ""), (0, second, "")]), self.assertRaises(cli.BlogCliError) as result:
            self.bridge._request("antigravity", "Research", self.root, model="", images=None, image=False, timeout=30)
        self.assertEqual(result.exception.code, "invalid_response")

    def test_multiple_continuations_keep_one_id_and_one_total_deadline(self):
        conversation_id, requests = str(uuid.uuid4()), []
        def run(args, cwd, **kwargs):
            requests.append((list(args), kwargs["timeout"]))
            url = f"https://source{len(requests)}.go.kr/page"
            return 0, events({"event": "init", "conversation_id": conversation_id},
                {"event": "step_update", "step_update": {"state": "ERROR", "tool_name": "read_url_content",
                    "tool_info": {"parameters": {"Url": url}}}},
                {"event": "result", "result": {"status": "SUCCESS", "response": "", "denied_actions": [{"action": "read_url"}]}}), ""
        with patch.object(cli.Path, "home", return_value=self.root), \
             patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(cli.time, "monotonic", side_effect=[100, 101, 251, 501, 701]), \
             patch.object(cli, "_run", side_effect=run), self.assertRaises(cli.BlogCliError) as result:
            self.bridge._request("antigravity", "Research", self.root, model="", images=None, image=False, timeout=600)
        self.assertEqual(result.exception.code, "timeout")
        self.assertEqual([timeout for _, timeout in requests], [599, 449, 199])
        for args, _ in requests[1:]:
            self.assertEqual(args.count("--conversation"), 1)
            self.assertEqual(args[-1], conversation_id)
        self.assertEqual(list((self.root / ".gemini/config/projects").iterdir()), [])

    def test_existing_or_non_web_denial_is_not_retried_or_bypassed(self):
        for action, url in (("read_url", "https://nts.go.kr/page"), ("run_command", "https://other.go.kr/page")):
            output = events({"event": "step_update", "step_update": {"state": "ERROR", "tool_name": "read_url_content",
                "tool_info": {"parameters": {"Url": url}}}}, {"event": "result", "result": {"status": "SUCCESS",
                    "response": "partial text", "denied_actions": [{"action": action}]}})
            with self.subTest(action=action), patch.object(cli.Path, "home", return_value=self.root), \
                 patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
                 patch.object(cli, "_run", return_value=(0, output, "")) as runner, self.assertRaises(cli.BlogCliError) as result:
                self.bridge._request("antigravity", "Verify https://nts.go.kr/page", self.root, model="", images=None, image=False, timeout=30)
            self.assertEqual(result.exception.code, "permission_required")
            self.assertIn(action, str(result.exception))
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(list((self.root / ".gemini/config/projects").iterdir()), [])

    def test_command_denial_continues_native_research_without_shell_permission(self):
        conversation = str(uuid.uuid4())
        first = events({"event": "init", "conversation_id": conversation},
            {"event": "result", "result": {"status": "SUCCESS", "response": "partial",
             "denied_actions": [{"action": "command"}]}})
        second = events({"event": "init", "conversation_id": conversation},
            {"event": "result", "result": {"status": "SUCCESS", "response": "verified"}})
        with patch.object(cli.Path, "home", return_value=self.root), \
             patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(cli, "_run", side_effect=[(0, first, ""), (0, second, "")]) as runner:
            result = self.bridge._request("antigravity", "Research public facts", self.root,
                model="", images=None, image=False, timeout=30)
        self.assertEqual(result.answer, "verified")
        args = runner.call_args_list[1].args[0]
        self.assertEqual(args[-2:], ["--conversation", conversation])
        self.assertFalse(any("permission" in arg or "yolo" in arg for arg in args))
        self.assertIn(b"Do not retry command", runner.call_args_list[1].kwargs['stdin'])

    def test_text_requests_provide_material_scope_without_local_discovery(self):
        for provider in cli.PROVIDER_NAMES:
            with self.subTest(provider=provider), \
                 patch.object(self.bridge, "_provider", return_value=self.provider(provider)), \
                 patch.object(self.bridge, "_record"), \
                 patch.object(self.bridge, "_request", return_value=cli._Response(answer="reviewed")) as request:
                result = self.bridge.run_text(provider, "원고와 검토 자료", model="chosen-model", timeout=123)
                self.assertEqual(result, "reviewed")
                instruction = request.call_args.args[1]
                self.assertIn("All supplied article text and review materials are included in this message", instruction)
                self.assertIn("do not list or explore the working directory, its parents, application-data", instruction)
                self.assertIn("do not read local files", instruction)
                self.assertIn("public primary sources using native web search and page-reading tools", instruction)
                self.assertTrue(instruction.endswith("원고와 검토 자료"))
                self.assertEqual(request.call_args.kwargs["model"], "chosen-model")
                self.assertEqual(request.call_args.kwargs["timeout"], 123)

    def test_image_review_keeps_selected_attachment_reads(self):
        selected = self.image()
        with patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(self.bridge, "_record"), \
             patch.object(self.bridge, "_request", return_value=cli._Response(answer="viewed")) as request:
            self.assertEqual(self.bridge.run_text("antigravity", "이미지 검수", images=[selected]), "viewed")
        instruction = request.call_args.args[1]
        self.assertIn("inspect the actual attached images", instruction)
        self.assertIn("Local reads are limited to the explicitly selected images", instruction)
        self.assertNotIn("do not read local files", instruction)
        self.assertEqual(request.call_args.kwargs["images"], [selected])

    def test_local_directory_read_denial_is_not_retried_or_granted(self):
        conversation = str(uuid.uuid4())
        output = events({"event": "init", "conversation_id": conversation},
            {"event": "step_update", "step_update": {"state": "ERROR", "tool_name": "list_dir",
                "tool_info": {"parameters": {"DirectoryPath": str(self.root.parent)}}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "partial",
                "denied_actions": [{"action": "read_file"}]}})
        with patch.object(cli.Path, "home", return_value=self.root), \
             patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(cli, "_run", return_value=(0, output, "")) as runner, \
             patch.object(cli, "_write_public_read_project", wraps=cli._write_public_read_project) as write_project, \
             self.assertRaises(cli.BlogCliError) as result:
            self.bridge._request("antigravity", "Verify https://source.go.kr/page", self.root,
                model="", images=None, image=False, timeout=30)
        self.assertEqual(result.exception.code, "permission_required")
        self.assertIn("read_file", str(result.exception))
        self.assertEqual(runner.call_count, 1)
        self.assertNotIn("--conversation", runner.call_args.args[0])
        self.assertEqual(write_project.call_count, 1)
        self.assertEqual(write_project.call_args.args[2], {"source.go.kr"})

    def test_scoped_project_is_removed_when_child_is_cancelled(self):
        with patch.object(cli.Path, "home", return_value=self.root), \
             patch.object(self.bridge, "_provider", return_value=self.provider("antigravity")), \
             patch.object(cli, "_run", side_effect=cli.BlogCliError("cancelled", "cancelled")), self.assertRaises(cli.BlogCliError):
            self.bridge._request("antigravity", "Research", self.root, model="", images=None, image=False, timeout=30)
        self.assertEqual(list((self.root / ".gemini/config/projects").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
