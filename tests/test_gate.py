import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from surya_gates import Gate, Reply, tool_call, tool_result

ANTHROPIC = {"type": "tool_use", "id": "toolu_1", "name": "deploy_app", "input": {"env": "prod"}}
CHAT = {"id": "call_1", "type": "function", "function": {"name": "deploy_app", "arguments": '{"env": "prod"}'}}
RESPONSES = {"type": "function_call", "id": "fc_1", "call_id": "call_2", "name": "deploy_app", "arguments": '{"env": "prod"}'}
MCP = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "deploy_app", "arguments": {"env": "prod"}}}


class Dumps(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


class Normalise(unittest.TestCase):
    def test_every_provider_reads_the_same(self):
        for raw, shape, ident in ((ANTHROPIC, "anthropic", "toolu_1"), (CHAT, "openai_chat", "call_1"), (RESPONSES, "openai_responses", "call_2"), (MCP, "mcp", "7")):
            with self.subTest(shape=shape):
                call = tool_call(raw)
                self.assertEqual((call.name, call.args, call.shape, call.id), ("deploy_app", {"env": "prod"}, shape, ident))

    def test_plain_forms(self):
        self.assertEqual(tool_call("x", {"a": 1}).args, {"a": 1})
        self.assertEqual(tool_call(("x", {"a": 1})).args, {"a": 1})
        self.assertEqual(tool_call({"name": "x", "args": {"a": 1}}).args, {"a": 1})
        self.assertEqual(tool_call({"name": "x", "arguments": '{"a": 1}'}).args, {"a": 1})

    def test_sdk_objects(self):
        self.assertEqual(tool_call(Dumps(**ANTHROPIC)).shape, "anthropic")
        self.assertEqual(tool_call(Dumps(**RESPONSES)).shape, "openai_responses")

    def test_unreadable_calls_raise(self):
        for bad in (123, {}, {"type": "text", "text": "hi"}, {"type": "tool_use"}, {"function": {"name": "x", "arguments": "{not json"}}, {"function": {"name": "x", "arguments": "[1]"}}, {"name": "x", "args": 5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tool_call(bad)

    def test_results_match_the_provider(self):
        self.assertEqual(tool_result(tool_call(ANTHROPIC), "ok"), {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok", "is_error": False})
        self.assertEqual(tool_result(tool_call(CHAT), "ok"), {"role": "tool", "tool_call_id": "call_1", "content": "ok"})
        self.assertEqual(tool_result(tool_call(RESPONSES), "ok"), {"type": "function_call_output", "call_id": "call_2", "output": "ok"})
        self.assertEqual(tool_result(tool_call(MCP), "bad", True), {"content": [{"type": "text", "text": "bad"}], "isError": True})
        self.assertEqual(json.loads(tool_result(tool_call("x"), {"a": 1})["content"]), {"a": 1})


class Decisions(unittest.TestCase):
    def test_same_decision_for_claude_and_gpt(self):
        for raw in (ANTHROPIC, CHAT, RESPONSES, MCP):
            asked = []
            gate = Gate(approver=lambda r: asked.append(r) or False)
            self.assertFalse(gate.check(raw).allowed)
            self.assertEqual((asked[0].cls, asked[0].call.name), ("deploy", "deploy_app"))

    def test_harmless_passes_without_asking(self):
        gate = Gate(approver=lambda r: self.fail("asked"))
        self.assertEqual(gate.check("get_user", {"id": 1}).decision, "pass")

    def test_no_approver_means_no(self):
        outcome = Gate().check("deploy_app")
        self.assertFalse(outcome.allowed)
        self.assertIn("no approver", outcome.message)

    def test_approver_failures_mean_no(self):
        def boom(_):
            raise RuntimeError("down")

        for approver in (boom, lambda r: None, lambda r: "yes", lambda r: 1, lambda r: Reply("maybe")):
            self.assertFalse(Gate(approver=approver).check("deploy_app").allowed)

    def test_yes(self):
        self.assertEqual(Gate(approver=lambda r: True).check("deploy_app").decision, "approved")
        self.assertTrue(Gate(approver=lambda r: Reply("yes")).check("deploy_app"))

    def test_reroute_tells_the_model_what_the_human_said(self):
        outcome = Gate(approver=lambda r: Reply("reroute", "use staging")).check("deploy_app")
        self.assertEqual((outcome.allowed, outcome.decision), (False, "rerouted"))
        self.assertIn("use staging", outcome.message)

    def test_trust_stops_asking_for_that_kind_only(self):
        asked = []
        gate = Gate(approver=lambda r: asked.append(r.cls) or Reply("trust"))
        gate.check("deploy_app")
        self.assertEqual(gate.check("deploy_other").decision, "trusted")
        gate.check("delete_thing")
        self.assertEqual(asked, ["deploy", "delete"])

    def test_some_kinds_can_never_be_trusted(self):
        asked = []
        gate = Gate(approver=lambda r: asked.append(1) or Reply("trust"))
        self.assertFalse(gate.check("send_email").allowed)
        self.assertEqual(gate.trusted, [])
        with self.assertRaises(ValueError):
            Gate(trust=("send",))

    def test_your_own_classes(self):
        gate = Gate(classes={"refund_order": "money", "delete_thing": None}, approver=lambda r: False)
        self.assertEqual(gate.check("refund_order").cls, "money")
        self.assertEqual(gate.check("delete_thing").decision, "pass")
        with self.assertRaises(ValueError):
            Gate(classes={"x": "nonsense"})

    def test_a_loop_stops_even_when_harmless(self):
        gate = Gate(approver=lambda r: False)
        results = [gate.check("get_user", {"id": 1}) for _ in range(3)]
        self.assertEqual([r.decision for r in results], ["pass", "pass", "denied"])
        self.assertEqual(results[2].cls, "loop")
        self.assertEqual(Gate(detect_loops=False).check("get_user").decision, "pass")

    def test_a_loop_breaks_through_trust(self):
        gate = Gate(trust=("deploy",), approver=lambda r: False)
        self.assertEqual([gate.check("deploy_app").decision for _ in range(3)], ["trusted", "trusted", "denied"])

    def test_shell_inside_an_app(self):
        gate = Gate(approver=lambda r: False)
        self.assertEqual(gate.check("bash", {"command": "git push"}).cls, "publish")

    def test_run(self):
        ran = []
        tools = {"deploy_app": lambda env: ran.append(env) or "done", "bad": lambda: 1 / 0}
        denied = Gate(approver=lambda r: False).run(ANTHROPIC, tools)
        self.assertTrue(denied["is_error"])
        self.assertEqual(ran, [])
        self.assertEqual(Gate(approver=lambda r: True).run(CHAT, tools), {"role": "tool", "tool_call_id": "call_1", "content": "done"})
        self.assertEqual(ran, ["prod"])
        self.assertIn("ZeroDivisionError", Gate().run(("bad", {}), tools)["content"])
        self.assertIn("Unknown tool", Gate().run(("nope", {}), tools)["content"])

    def test_async(self):
        async def approve(_):
            return Reply("yes")

        async def tool(env):
            return f"to {env}"

        gate = Gate(approver=approve)
        self.assertEqual(asyncio.run(gate.arun(RESPONSES, {"deploy_app": tool})), {"type": "function_call_output", "call_id": "call_2", "output": "to prod"})

    def test_async_approver_called_synchronously_is_a_no(self):
        async def approve(_):
            return True

        outcome = Gate(approver=approve).check("deploy_app")
        self.assertFalse(outcome.allowed)
        self.assertIn("acheck", outcome.message)

    def test_claude_sdk_hook_shape(self):
        hook = Gate(approver=lambda r: False).claude_sdk_hook()
        out = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "git push"}}, "id", None))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})), {})

    def test_record_writes_the_audit_log(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"SURYA_GATES_HOME": home}):
            Gate(approver=lambda r: True, record=True).check("deploy_app")
            self.assertIn('"approved"', Path(home, "audit.jsonl").read_text())
