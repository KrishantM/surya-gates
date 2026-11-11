import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from surya_gates.channels import MAX_WAIT
from tests.fakes import CHAT, HUMAN, FakeMessages, FakeNtfy, FakeTelegram

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "surya-gates"
PUSH = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}
DEPLOY = {"tool_name": "Bash", "tool_input": {"command": "fly deploy"}}
LS = {"tool_name": "Bash", "tool_input": {"command": "ls"}}


class Base(unittest.TestCase):
    agent = "claude"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name)
        self.cwd = str(self.home / "project")
        os.makedirs(self.cwd)
        self.env = {**os.environ, "SURYA_GATES_HOME": str(self.home / "state"), "CODEX_HOME": str(self.home / "codex")}

    def hook(self, event, payload, agent=None, raw=None):
        body = raw if raw is not None else json.dumps({"session_id": "s1", "cwd": self.cwd, "hook_event_name": event, **payload})
        result = subprocess.run([sys.executable, str(SCRIPT), "hook", agent or self.agent, event], input=body, capture_output=True, text=True, env=self.env, timeout=30)
        out = json.loads(result.stdout) if result.stdout.strip() else None
        return result.returncode, out, result.stderr

    def pre(self, payload):
        return self.hook("pre", payload)

    def say(self, prompt):
        return self.hook("prompt", {"prompt": prompt})

    def decision(self, payload):
        code, out, _ = self.pre(payload)
        self.assertEqual(code, 0)
        return out["hookSpecificOutput"]["permissionDecision"] if out and "hookSpecificOutput" in out else out

    def audit(self):
        path = self.home / "state" / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def text_on(self, **extra):
        self.server = FakeTelegram()
        self.addCleanup(self.server.close)
        config = {"text": {"channel": "telegram", "token": FakeTelegram.TOKEN, "chat_id": CHAT, "base_url": self.server.url, "enabled": True, "poll_seconds": 0.02, "wait_seconds": 5, **extra}}
        state = self.home / "state"
        state.mkdir(exist_ok=True)
        (state / "config.json").write_text(json.dumps(config))

    def reply_with(self, template):
        self.server.on_send = lambda text: "YES" in text and self.server.reply(template.format(code=self.server.code()))


class ClaudeCode(Base):
    def test_harmless_is_silent(self):
        self.assertEqual(self.pre(LS), (0, None, ""))
        self.assertEqual(self.audit(), [])

    def test_consequential_asks_natively_when_text_is_off(self):
        code, out, _ = self.pre(PUSH)
        self.assertEqual(code, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("Push or publish code", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.audit()[0]["event"], "asked")

    def test_refused(self):
        out = self.pre({"tool_name": "Bash", "tool_input": {"command": "cat ~/.surya-gates/config.json"}})[1]
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.audit()[0]["event"], "refused")

    def test_read_of_a_credential(self):
        self.assertEqual(self.decision({"tool_name": "Read", "tool_input": {"file_path": f"{self.cwd}/.env"}}), "ask")

    def test_write_outside(self):
        self.assertEqual(self.decision({"tool_name": "Write", "tool_input": {"file_path": "/somewhere/else.txt"}}), "ask")
        self.assertEqual(self.pre({"tool_name": "Write", "tool_input": {"file_path": f"{self.cwd}/a.txt"}})[1], None)

    def test_scratchpad_is_writable(self):
        self.assertIsNone(self.pre({"tool_name": "Write", "tool_input": {"file_path": "/scratch/a.txt"}, "scratchpad_dir": "/scratch"})[1])

    def test_mcp(self):
        self.assertEqual(self.decision({"tool_name": "mcp__slack__chat_postMessage", "tool_input": {"text": "hi"}}), "ask")
        self.assertIsNone(self.pre({"tool_name": "mcp__slack__list_channels", "tool_input": {}})[1])

    def test_trust_then_it_tells_you(self):
        reason = self.say("gates trust publish")[1]["reason"]
        self.assertIn("Trusted", reason)
        code, out, _ = self.pre(PUSH)
        self.assertEqual(code, 0)
        self.assertIn("you trust this", out["systemMessage"])
        self.assertNotIn("hookSpecificOutput", out)
        self.assertEqual(self.audit()[-1]["event"], "trusted")
        self.assertEqual(self.decision(DEPLOY), "ask")

    def test_commands_never_reach_the_model(self):
        for prompt in ("gates status", "gates log", "gates help", "gates trust publish", "gates text off"):
            with self.subTest(prompt=prompt):
                out = self.say(prompt)[1]
                self.assertEqual(out["decision"], "block")
                self.assertTrue(out["hookSpecificOutput"]["suppressOriginalPrompt"])

    def test_some_kinds_cannot_be_trusted(self):
        for kind in ("send", "money", "kill", "credentials", "loop", "permissions"):
            self.assertIn("never", self.say(f"gates trust {kind}")[1]["reason"])
        self.assertIn("Unknown", self.say("gates trust everything")[1]["reason"])

    def test_ordinary_prompts_pass_through(self):
        for prompt in ("gates should be simple", "gates no longer work", "gates trust the user", "Do the gates yes", "gates text me"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.say(prompt), (0, None, ""))

    def test_untrust(self):
        self.say("gates trust publish")
        self.say("gates untrust publish")
        self.assertEqual(self.decision(PUSH), "ask")

    def test_yes_approves_that_exact_call_once(self):
        self.assertEqual(self.decision(PUSH), "ask")
        context = self.say("gates yes")[1]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Retry", context)
        self.assertEqual(self.decision(DEPLOY), "ask")  # a different call isn't covered
        self.say("gates no")
        self.assertEqual(self.decision(PUSH), "allow")
        self.assertEqual(self.decision(PUSH), "ask")

    def test_yes_with_nothing_waiting(self):
        self.assertIn("Nothing", self.say("gates yes")[1]["reason"])

    def test_no(self):
        self.decision(PUSH)
        self.assertIn("Do not retry", self.say("gates no")[1]["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Nothing", self.say("gates yes")[1]["reason"])

    def test_trust_is_per_session_and_clears_at_the_end(self):
        self.say("gates trust publish")
        self.assertEqual(self.hook("pre", {**PUSH, "session_id": "other"})[1]["hookSpecificOutput"]["permissionDecision"], "ask")
        self.hook("end", {})
        self.assertEqual(self.decision(PUSH), "ask")

    def test_loop(self):
        read = {"tool_name": "Read", "tool_input": {"file_path": f"{self.cwd}/a"}}
        self.assertIsNone(self.pre(read)[1])
        self.assertIsNone(self.pre(read)[1])
        self.assertEqual(self.decision(read), "ask")
        self.assertEqual(self.audit()[-1]["class"], "loop")

    def test_loop_cannot_be_trusted(self):
        self.assertIn("never", self.say("gates trust loop")[1]["reason"])

    def test_loop_breaks_through_trust(self):
        self.say("gates trust publish")
        self.assertIn("systemMessage", self.pre(PUSH)[1])
        self.assertIn("systemMessage", self.pre(PUSH)[1])
        self.assertEqual(self.decision(PUSH), "ask")
        self.assertEqual(self.audit()[-1]["class"], "loop")

    def test_status_and_log(self):
        self.decision(PUSH)
        status = self.say("gates status")[1]["reason"]
        self.assertIn("Texting: off", status)
        self.assertIn("Push or publish", status)
        self.assertIn("asked", self.say("gates log")[1]["reason"])

    def test_text_on_without_a_channel(self):
        self.assertIn("setup", self.say("gates text on")[1]["reason"])


class FailsClosed(Base):
    def test_bad_input_blocks(self):
        code, _, err = self.hook("pre", {}, raw="{not json")
        self.assertEqual(code, 2)
        self.assertIn("failed closed", err)

    def test_missing_tool_name_blocks(self):
        self.assertEqual(self.hook("pre", {"tool_input": {}})[0], 2)

    def test_corrupt_state_blocks(self):
        sessions = self.home / "state" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "s1.json").write_text("{broken")
        code, _, err = self.pre(LS)
        self.assertEqual(code, 2)
        self.assertIn("failed closed", err)

    def test_unknown_agent_or_event_blocks(self):
        self.assertEqual(self.hook("pre", PUSH, agent="other")[0], 2)
        self.assertEqual(self.hook("sideways", PUSH)[0], 2)

    def test_hooks_file_blocks_on_any_failure(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        pre = hooks["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "*")
        self.assertEqual(pre["onFailure"], "block")
        self.assertGreater(pre["timeout"], MAX_WAIT)
        self.assertTrue(pre["command"].endswith("|| exit 2"))
        self.assertTrue(hooks["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("|| exit 2"))

    def test_python_missing_blocks_through_the_wrapper(self):
        command = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        result = subprocess.run(["/bin/sh", "-c", command], env={"PATH": "/nonexistent", "CLAUDE_PLUGIN_ROOT": str(ROOT)}, input="{}", capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


class ByText(Base):
    def test_yes(self):
        self.text_on()
        self.reply_with("YES {code}")
        self.assertEqual(self.decision(PUSH), "allow")
        self.assertEqual(self.audit()[-1]["via"], "text")
        self.assertIn("git push origin main", self.server.sent[0])
        self.assertIn("Project: project", self.server.sent[0])

    def test_no(self):
        self.text_on()
        self.reply_with("NO {code}")
        out = self.pre(PUSH)[1]["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("declined", out["permissionDecisionReason"])

    def test_reroute(self):
        self.text_on()
        self.reply_with("{code} push to staging instead")
        out = self.pre(PUSH)[1]["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("push to staging instead", out["permissionDecisionReason"])
        self.assertEqual(self.audit()[-1]["event"], "rerouted")

    def test_trust(self):
        self.text_on()
        self.reply_with("TRUST {code}")
        self.assertEqual(self.decision(PUSH), "allow")
        self.assertIn("publish", self.say("gates status")[1]["reason"])
        self.assertIn("you trust this", self.pre({"tool_name": "Bash", "tool_input": {"command": "git push origin dev"}})[1]["systemMessage"])

    def test_no_answer_denies(self):
        self.text_on(wait_seconds=5)
        started = time.time()
        out = self.pre(PUSH)[1]["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("no answer", out["permissionDecisionReason"])
        self.assertGreater(time.time() - started, 4)
        self.assertEqual(self.audit()[-1]["via"], "text, no answer")

    def test_after_no_answer_gates_yes_still_works(self):
        self.text_on(wait_seconds=5)
        self.pre(PUSH)
        self.say("gates text off")
        self.say("gates yes")
        self.assertEqual(self.decision(PUSH), "allow")

    def test_send_failure_falls_back_to_asking(self):
        self.text_on()
        self.server.close()
        out = self.pre(PUSH)[1]["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("could not be sent", out["permissionDecisionReason"])

    def test_text_can_be_turned_off(self):
        self.text_on()
        self.assertIn("off", self.say("gates text off")[1]["reason"])
        self.assertEqual(self.decision(PUSH), "ask")
        self.assertEqual(self.server.sent, [])
        self.assertIn("on", self.say("gates text on")[1]["reason"])

    def test_secrets_never_reach_the_phone(self):
        self.text_on()
        self.reply_with("NO {code}")
        self.pre({"tool_name": "Bash", "tool_input": {"command": "API_KEY=abcdef123 git push"}})
        self.assertNotIn("abcdef123", self.server.sent[0])

    def test_commands_can_be_left_out(self):
        self.text_on(show_commands=False)
        self.reply_with("NO {code}")
        self.pre(PUSH)
        self.assertNotIn("git push", self.server.sent[0])


class ByNtfy(ByText):
    def text_on(self, **extra):
        self.server = FakeNtfy()
        self.addCleanup(self.server.close)
        config = {"text": {"channel": "ntfy", "topic": FakeNtfy.TOPIC, "server": self.server.url, "enabled": True, "poll_seconds": 0.02, "wait_seconds": 5, **extra}}
        state = self.home / "state"
        state.mkdir(exist_ok=True)
        (state / "config.json").write_text(json.dumps(config))

    def test_agent_cannot_answer_its_own_gate(self):
        self.text_on()
        reply = f"curl -d 'YES 1234' https://ntfy.sh/{FakeNtfy.TOPIC}-reply"
        out = self.pre({"tool_name": "Bash", "tool_input": {"command": reply}})[1]["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("own text channel", out["permissionDecisionReason"])
        self.assertNotIn(FakeNtfy.TOPIC, json.dumps(self.audit()))
        self.assertEqual(self.server.sent, [])


@unittest.skipUnless(sys.platform == "darwin", "iMessage is macOS only")
class ByIMessage(ByText):
    def text_on(self, **extra):
        self.server = FakeMessages()
        self.addCleanup(self.server.close)
        self.env["PATH"] = self.server.bin + os.pathsep + self.env["PATH"]
        config = {"text": {"channel": "imessage", "to": HUMAN, "db": self.server.db, "enabled": True, "poll_seconds": 0.02, "wait_seconds": 5, **extra}}
        state = self.home / "state"
        state.mkdir(exist_ok=True)
        (state / "config.json").write_text(json.dumps(config))

    def test_send_failure_falls_back_to_asking(self):
        self.text_on()
        os.remove(os.path.join(self.server.bin, "osascript"))
        self.env["PATH"] = self.server.bin
        out = self.pre(PUSH)[1]["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("could not be sent", out["permissionDecisionReason"])

    def test_agent_cannot_read_messages(self):
        self.text_on()
        for payload in ({"tool_name": "Read", "tool_input": {"file_path": "/Users/me/Library/Messages/chat.db"}}, {"tool_name": "Bash", "tool_input": {"command": "sqlite3 ~/Library/Messages/chat.db 'select text from message'"}}):
            with self.subTest(payload=payload):
                self.assertEqual(self.decision(payload), "deny")
        self.assertEqual(self.server.sent, [])


class GuardOff(Base):
    def test_messages_open_without_imessage(self):
        self.assertIsNone(self.pre({"tool_name": "Read", "tool_input": {"file_path": "/Users/me/Library/Messages/chat.db"}})[1])


class Codex(Base):
    agent = "codex"

    def test_never_emits_ask(self):
        for payload in (PUSH, DEPLOY):
            out = self.pre(payload)[1]["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("gates yes", out["permissionDecisionReason"])

    def test_harmless_is_silent(self):
        self.assertEqual(self.pre(LS), (0, None, ""))

    def test_gates_yes(self):
        self.pre(PUSH)
        self.assertIn("Retry", self.say("gates yes")[1]["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.pre(PUSH), (0, None, ""))
        self.assertEqual(self.pre(PUSH)[1]["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_prompt_commands_have_no_claude_only_fields(self):
        self.assertNotIn("hookSpecificOutput", self.say("gates status")[1])

    def test_apply_patch(self):
        patch = {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: /elsewhere/a\n+x\n*** End Patch"}}
        self.assertEqual(self.pre(patch)[1]["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_text_yes_is_a_silent_exit(self):
        self.text_on()
        self.reply_with("YES {code}")
        self.assertEqual(self.pre(PUSH), (0, None, ""))

    def test_text_no_denies(self):
        self.text_on()
        self.reply_with("NO {code}")
        self.assertEqual(self.pre(PUSH)[1]["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_crash_exits_2(self):
        self.assertEqual(self.hook("pre", {}, raw="nope")[0], 2)


class Cli(Base):
    def run_cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=self.env, timeout=30)

    def test_check(self):
        out = json.loads(self.run_cli("check", stdin=json.dumps({"type": "function_call", "call_id": "c", "name": "shell", "arguments": '{"command": "git push"}'})).stdout)
        self.assertEqual(out, {"action": "stop", "class": "publish", "summary": "Push or publish code", "trustable": True})

    def test_check_unreadable_call_stops(self):
        self.assertEqual(json.loads(self.run_cli("check", stdin="{}").stdout)["action"], "stop")

    def test_classes(self):
        self.assertIn("always asks", self.run_cli("classes").stdout)

    def test_install_codex_merges_and_backs_up(self):
        codex = self.home / "codex"
        codex.mkdir()
        (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "mine"}]}], "Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
        self.assertEqual(self.run_cli("install", "codex").returncode, 0)
        self.run_cli("install", "codex")  # twice is safe
        data = json.loads((codex / "hooks.json").read_text())["hooks"]
        self.assertEqual(len(data["PreToolUse"]), 2)
        self.assertEqual(data["PreToolUse"][0]["hooks"][0]["command"], "mine")
        self.assertIn("hook codex pre || exit 2", data["PreToolUse"][1]["hooks"][0]["command"])
        self.assertGreater(data["PreToolUse"][1]["hooks"][0]["timeout"], MAX_WAIT)
        self.assertIn("Stop", data)
        self.assertTrue(list(codex.glob("hooks.json.backup-*")))
        installed = data["PreToolUse"][1]["hooks"][0]["command"].split('"')[1]
        self.assertTrue(Path(installed).exists())
        result = subprocess.run(["python3", installed, "classes"], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(self.run_cli("uninstall", "codex").returncode, 0)
        data = json.loads((codex / "hooks.json").read_text())["hooks"]
        self.assertEqual(len(data["PreToolUse"]), 1)
        self.assertNotIn("UserPromptSubmit", data)

    def test_install_refuses_broken_json(self):
        codex = self.home / "codex"
        codex.mkdir()
        (codex / "hooks.json").write_text("{broken")
        self.assertEqual(self.run_cli("install", "codex").returncode, 1)
        self.assertEqual((codex / "hooks.json").read_text(), "{broken")

    def test_text_setup_telegram(self):
        server = FakeTelegram()
        self.addCleanup(server.close)
        server.reply("hello")
        env = {**self.env, "SURYA_GATES_TELEGRAM_TOKEN": FakeTelegram.TOKEN, "SURYA_GATES_TELEGRAM_BASE": server.url}
        result = subprocess.run([sys.executable, str(SCRIPT), "text", "setup", "telegram"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.home / "state" / "config.json").read_text())
        self.assertEqual(config["text"]["chat_id"], str(CHAT))
        self.assertTrue(config["text"]["enabled"])
        self.assertEqual((self.home / "state" / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("connected", server.sent[0])

    def test_text_on_off(self):
        self.assertEqual(self.run_cli("text", "on").returncode, 1)
        self.text_on()
        self.run_cli("text", "off")
        self.assertIn("Texting: off", self.run_cli("status").stdout)
        self.run_cli("text", "on")
        self.assertIn("Texting: on", self.run_cli("status").stdout)
        self.assertEqual(self.run_cli("text", "test").returncode, 0)
        self.assertIn("test message", self.server.sent[-1])

    def test_text_wait(self):
        self.assertEqual(self.run_cli("text", "wait").returncode, 1)
        self.assertIn("3600", self.run_cli("text", "wait", "3600").stdout)
