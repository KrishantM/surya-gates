"""The gate for applications. Works on tool calls from any model provider."""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .rules import CLASSES, PASS, Verdict, classify
from .session import Session, audit, digest, redact


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    id: str = ""
    shape: str = "plain"  # anthropic, openai_chat, openai_responses, mcp or plain


@dataclass(frozen=True)
class Request:
    """What an approver is asked to decide."""

    call: ToolCall
    verdict: Verdict

    @property
    def summary(self) -> str:
        return self.verdict.summary

    @property
    def detail(self) -> str:
        return redact(self.verdict.detail)

    @property
    def cls(self) -> str:
        return self.verdict.cls

    @property
    def trustable(self) -> bool:
        return self.verdict.trustable


@dataclass(frozen=True)
class Reply:
    """A human's answer. `decision` is yes, no, trust or reroute."""

    decision: str
    instruction: str = ""
    via: str = ""


@dataclass(frozen=True)
class Outcome:
    allowed: bool
    decision: str  # pass, trusted, approved, denied, refused or rerouted
    cls: str = ""
    summary: str = ""
    message: str = ""  # what to tell the model when the call doesn't run

    def __bool__(self) -> bool:
        return self.allowed


def _parse_args(value: Any) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("tool arguments are not a JSON object")


def tool_call(raw: Any, args: Optional[Mapping] = None) -> ToolCall:
    """Read a tool call in any provider's format.

    Takes Anthropic `tool_use` blocks, OpenAI Chat `tool_calls`, OpenAI Responses
    `function_call` items, MCP `tools/call` requests, SDK objects of those, plain
    `{"name", "args"}` dicts, or a name and arguments. Anything else raises
    ValueError, so a call Gates can't read never runs.
    """
    if isinstance(raw, ToolCall):
        return raw
    if isinstance(raw, str):
        return ToolCall(raw, _parse_args(args))
    if isinstance(raw, (tuple, list)) and len(raw) == 2 and isinstance(raw[0], str):
        return ToolCall(raw[0], _parse_args(raw[1]))
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, Mapping):
        raise ValueError(f"unrecognised tool call: {type(raw).__name__}")

    kind = raw.get("type")
    if kind == "tool_use" and raw.get("name"):
        return ToolCall(raw["name"], _parse_args(raw.get("input")), str(raw.get("id", "")), "anthropic")
    if isinstance(raw.get("function"), Mapping) and raw["function"].get("name"):
        fn = raw["function"]
        return ToolCall(fn["name"], _parse_args(fn.get("arguments")), str(raw.get("id", "")), "openai_chat")
    if kind == "function_call" and raw.get("name"):
        return ToolCall(raw["name"], _parse_args(raw.get("arguments")), str(raw.get("call_id", "")), "openai_responses")
    if raw.get("method") == "tools/call" and isinstance(raw.get("params"), Mapping) and raw["params"].get("name"):
        return ToolCall(raw["params"]["name"], _parse_args(raw["params"].get("arguments")), str(raw.get("id", "")), "mcp")
    if raw.get("name") and not kind:
        return ToolCall(raw["name"], _parse_args(raw.get("args", raw.get("arguments", raw.get("input")))), str(raw.get("id", "")))
    raise ValueError("unrecognised tool call")


def _text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, default=str)


def tool_result(call: ToolCall, content: Any, is_error: bool = False) -> dict:
    """Build the result message in the same provider's format as the call."""
    text = _text(content)
    if call.shape == "anthropic":
        return {"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error}
    if call.shape == "openai_chat":
        return {"role": "tool", "tool_call_id": call.id, "content": text}
    if call.shape == "openai_responses":
        return {"type": "function_call_output", "call_id": call.id, "output": text}
    if call.shape == "mcp":
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    return {"content": text, "is_error": is_error}


def terminal_approver(request: Request) -> Reply:
    """Ask at the terminal. Useful while developing."""
    print(f"\nSurya Gates: {request.summary}\n  {request.detail}", file=sys.stderr)
    hint = "[y]es, [n]o, " + ("[t]rust this kind, " if request.trustable else "") + "or type what to do instead: "
    print(hint, end="", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline()
    except (KeyboardInterrupt, OSError):
        return Reply("no", via="terminal")
    if not answer:
        return Reply("no", via="terminal")
    answer = answer.strip()
    low = answer.lower()
    if low in ("y", "yes"):
        return Reply("yes", via="terminal")
    if low in ("t", "trust") and request.trustable:
        return Reply("trust", via="terminal")
    if low in ("", "n", "no", "t", "trust"):
        return Reply("no", via="terminal")
    return Reply("reroute", answer, via="terminal")


Approver = Callable[[Request], Any]


class Gate:
    """Decides whether a tool call may run, and asks a human when it must not run alone.

    approver   called with a Request, returns a Reply or a bool. May be async if
               you use `acheck` and `arun`. With no approver, every stop is a no.
    classes    your own tools: {"refund_order": "money", "get_user": None}.
               None means never gate. Tools not listed are judged by their names.
    trust      classes that may run without asking, for this Gate's lifetime.
    """

    def __init__(self, approver: Optional[Approver] = None, classes: Optional[Mapping[str, Optional[str]]] = None, trust: tuple = (), cwd: Optional[str] = None, writable: tuple = (), session_id: str = "app", detect_loops: bool = True, record: bool = False) -> None:
        for cls in [*(classes or {}).values(), *trust]:
            if cls is not None and cls not in CLASSES:
                raise ValueError(f"unknown class: {cls}")
        untrustable = [c for c in trust if not CLASSES[c][1]]
        if untrustable:
            raise ValueError(f"these can never be trusted: {', '.join(untrustable)}")
        self.approver = approver
        self.classes = dict(classes or {})
        self.cwd = cwd
        self.writable = tuple(writable)
        self.session_id = session_id
        self.detect_loops = detect_loops
        self.record = record
        self.session = Session()
        for cls in trust:
            self.session.trust(cls)

    @property
    def trusted(self) -> list:
        return self.session.trusted

    def _verdict(self, call: ToolCall) -> Verdict:
        if call.name in self.classes:
            cls = self.classes[call.name]
            if cls is None:
                return PASS
            return Verdict("stop", cls, f"{CLASSES[cls][0]}: {call.name}", json.dumps(call.args, default=str)[:400])
        return classify(call.name, call.args, self.cwd, self.writable, by_name=True)

    def _log(self, event: str, call: ToolCall, verdict: Verdict, via: str = "") -> None:
        if self.record:
            audit(event, self.session_id, call.name, verdict.cls, verdict.summary, verdict.detail, via)

    def _begin(self, call: ToolCall):
        verdict = self._verdict(call)
        if self.detect_loops and self.session.loop_hit(digest(call.name, call.args), call.name, call.args) and (verdict.action == "pass" or verdict.cls in self.session.trusted):
            verdict = Verdict("stop", "loop", f"The same call has repeated with nothing changed: {call.name}", json.dumps(call.args, default=str)[:400])
        if verdict.action == "pass":
            return Outcome(True, "pass"), None
        if verdict.action == "refuse":
            self._log("refused", call, verdict)
            return Outcome(False, "refused", verdict.cls, verdict.summary, f"Surya Gates refused this: {verdict.summary}."), None
        if verdict.trustable and verdict.cls in self.session.trusted:
            self._log("trusted", call, verdict)
            return Outcome(True, "trusted", verdict.cls, verdict.summary), None
        return None, Request(call, verdict)

    def _finish(self, request: Request, answer: Any) -> Outcome:
        verdict, call = request.verdict, request.call
        reply = Reply("yes") if answer is True else answer
        via = reply.via if isinstance(reply, Reply) else ""
        if isinstance(reply, Reply):
            if reply.decision == "yes" or (reply.decision == "trust" and verdict.trustable):
                if reply.decision == "trust":
                    self.session.trust(verdict.cls)
                self._log("approved", call, verdict, via)
                return Outcome(True, "approved", verdict.cls, verdict.summary)
            if reply.decision == "reroute" and reply.instruction:
                self._log("rerouted", call, verdict, via)
                return Outcome(False, "rerouted", verdict.cls, verdict.summary, f"The human did not approve this. Their instruction: {reply.instruction}")
        self._log("denied", call, verdict, via)
        return Outcome(False, "denied", verdict.cls, verdict.summary, f"A human declined this: {verdict.summary}. Do not retry it.")

    def _no_answer(self, request: Request, why: str) -> Outcome:
        self._log("denied", request.call, request.verdict, why[:80])
        return Outcome(False, "denied", request.cls, request.summary, f"Surya Gates could not get an approval, so this did not run: {why}")

    def check(self, raw: Any, args: Optional[Mapping] = None) -> Outcome:
        """Decide one call. A failure anywhere in asking counts as a no."""
        outcome, request = self._begin(tool_call(raw, args))
        if outcome is not None:
            return outcome
        if self.approver is None:
            return self._no_answer(request, "no approver is configured.")
        try:
            answer = self.approver(request)
        except Exception as error:  # noqa: BLE001 - any approver failure is a no
            return self._no_answer(request, f"the approver failed ({type(error).__name__}).")
        if inspect.isawaitable(answer):
            if inspect.iscoroutine(answer):
                answer.close()
            return self._no_answer(request, "the approver is async, so use acheck or arun.")
        return self._finish(request, answer)

    async def acheck(self, raw: Any, args: Optional[Mapping] = None) -> Outcome:
        outcome, request = self._begin(tool_call(raw, args))
        if outcome is not None:
            return outcome
        if self.approver is None:
            return self._no_answer(request, "no approver is configured.")
        try:
            answer = self.approver(request)
            if inspect.isawaitable(answer):
                answer = await answer
        except Exception as error:  # noqa: BLE001 - any approver failure is a no
            return self._no_answer(request, f"the approver failed ({type(error).__name__}).")
        return self._finish(request, answer)

    def run(self, raw: Any, tools: Mapping[str, Callable]) -> dict:
        """Check a call, run it if allowed, and return a result to give the model."""
        call = tool_call(raw)
        outcome = self.check(call)
        if not outcome.allowed:
            return tool_result(call, outcome.message, is_error=True)
        function = tools.get(call.name)
        if function is None:
            return tool_result(call, f"Unknown tool: {call.name}", is_error=True)
        try:
            return tool_result(call, function(**call.args))
        except Exception as error:  # noqa: BLE001 - reported to the model as a tool error
            return tool_result(call, f"{type(error).__name__}: {error}", is_error=True)

    async def arun(self, raw: Any, tools: Mapping[str, Callable]) -> dict:
        call = tool_call(raw)
        outcome = await self.acheck(call)
        if not outcome.allowed:
            return tool_result(call, outcome.message, is_error=True)
        function = tools.get(call.name)
        if function is None:
            return tool_result(call, f"Unknown tool: {call.name}", is_error=True)
        try:
            value = function(**call.args)
            if inspect.isawaitable(value):
                value = await value
            return tool_result(call, value)
        except Exception as error:  # noqa: BLE001 - reported to the model as a tool error
            return tool_result(call, f"{type(error).__name__}: {error}", is_error=True)

    def claude_sdk_hook(self) -> Callable:
        """A PreToolUse hook function for the Claude Agent SDK."""

        async def hook(input_data: Mapping, tool_use_id: Any = None, context: Any = None) -> dict:
            outcome = await self.acheck(input_data["tool_name"], input_data.get("tool_input"))
            if outcome.allowed:
                return {}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": outcome.message}}

        return hook
