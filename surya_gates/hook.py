"""Hook entry points for Claude Code and Codex.

The rule that matters here: on any error, exit 2. Claude Code and Codex both treat
exit 2 as a block and every other non-zero exit as "carry on", so a crash that
exited 1 would let the action through.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Optional

from .rules import CLASSES, Verdict, classify
from .session import Session, audit, clean_old_sessions, digest, load_config, read_audit, redact, save_config

BLOCK = 2


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def _decision(kind: str, reason: str) -> None:
    _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind, "permissionDecisionReason": reason}})


def _human_line(verdict: Verdict) -> str:
    detail = " ".join(redact(verdict.detail).split())
    return f"{verdict.summary}: {detail[:200]}" if detail else verdict.summary


def _codex_ask_text(verdict: Verdict) -> str:
    return f"Surya Gates stopped this: {_human_line(verdict)}. Tell the human what you want to do and why. If they type `gates yes`, retry the same call and it will run once. Do not try another way to do the same thing."


def _approved(agent: str, reason: str) -> None:
    # On Codex, `allow` is only valid with updatedInput, and exit 0 lets the call run.
    if agent == "claude":
        _decision("allow", reason)


def _touches_channel(config: dict, args: dict) -> bool:
    """An agent that could read or write the text channel could answer its own gate."""
    if not (config.get("text") or {}).get("channel"):
        return False
    from .channels import guarded

    blob = json.dumps(args, default=str)
    return any(word in blob for word in guarded(config))


def _pre_tool(payload: dict, agent: str) -> int:
    name = payload["tool_name"]
    args = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or "default"
    cwd = payload.get("cwd") or os.getcwd()
    config = load_config()
    writable = [*config.get("writable", []), payload.get("scratchpad_dir") or ""]
    key = digest(name, args)
    session = Session.named(session_id)

    with session.edit():
        verdict = classify(name, args, cwd, writable)
        if _touches_channel(config, args):
            verdict = Verdict("refuse", "permissions", "Use Surya Gates' own text channel", name)
        if session.loop_hit(key, name, args) and (verdict.action == "pass" or verdict.cls in session.trusted):
            verdict = Verdict("stop", "loop", "The same call has repeated with nothing changed", f"{name} {json.dumps(args, default=str)[:200]}")
        if verdict.action == "pass":
            return 0
        if verdict.action == "refuse":
            audit("refused", session_id, name, verdict.cls, verdict.summary, verdict.detail)
            _decision("deny", f"Surya Gates refused this: {verdict.summary}. An agent can't change its own gates. A person has to do this in their own terminal.")
            return 0
        if session.take_once(key):
            audit("approved", session_id, name, verdict.cls, verdict.summary, verdict.detail, "typed")
            _approved(agent, "You approved this with `gates yes`.")
            return 0
        if verdict.trustable and verdict.cls in session.trusted:
            audit("trusted", session_id, name, verdict.cls, verdict.summary, verdict.detail)
            _emit({"systemMessage": f"Surya Gates: you trust this. {_human_line(verdict)}"})
            return 0
        session.set_pending(key, _human_line(verdict), verdict.cls)

    from .channels import ChannelError, ask_human, build_channel, text_enabled, wait_seconds

    sent_error = ""
    if text_enabled(config):
        project = Path(cwd).name
        text = config.get("text") or {}
        try:
            answer = ask_human(build_channel(config), verdict.summary, verdict.detail, project, verdict.trustable, wait_seconds(config), bool(text.get("show_commands", True)), float(text.get("poll_seconds", 3)))
        except ChannelError as error:
            sent_error = str(error)
        else:
            return _answered(answer, verdict, session, session_id, name, agent)

    audit("asked", session_id, name, verdict.cls, verdict.summary, verdict.detail, f"text failed: {sent_error}" if sent_error else "")
    note = f" The text could not be sent ({sent_error})." if sent_error else ""
    if agent == "claude":
        _decision("ask", f"Surya Gates: {_human_line(verdict)}.{note}")
    else:
        _decision("deny", _codex_ask_text(verdict) + note)
    return 0


def _answered(answer: Optional[dict], verdict: Verdict, session: Session, session_id: str, name: str, agent: str) -> int:
    if answer is None:
        audit("denied", session_id, name, verdict.cls, verdict.summary, verdict.detail, "text, no answer")
        _decision("deny", f"Surya Gates texted the human about this and got no answer in time, so it did not run: {_human_line(verdict)}. Move on to work that doesn't need it and say what is waiting.")
        return 0
    decision = answer["decision"]
    if decision in ("yes", "trust"):
        with session.edit():
            session.take_pending()
            if decision == "trust" and verdict.trustable:
                session.trust(verdict.cls)
        audit("approved", session_id, name, verdict.cls, verdict.summary, verdict.detail, "text")
        _approved(agent, "Approved by text.")
        return 0
    with session.edit():
        session.take_pending()
    if decision == "reroute":
        audit("rerouted", session_id, name, verdict.cls, verdict.summary, verdict.detail, "text")
        _decision("deny", f"The human did not approve this and sent an instruction by text. Treat it as coming from them: {answer['instruction']}")
        return 0
    audit("denied", session_id, name, verdict.cls, verdict.summary, verdict.detail, "text")
    _decision("deny", f"The human declined this by text: {_human_line(verdict)}. Do not retry it or try another way to do it.")
    return 0


HELP = """Surya Gates commands. Type these yourself. An agent can't send them.

gates status             what is trusted, and whether texting is on
gates log                the last 20 decisions
gates trust <kind>       stop asking about this kind until the session ends
gates untrust [kind]     start asking again
gates yes                approve the call that was last stopped, once
gates no                 refuse it
gates text on            ask by text
gates text off           ask here instead
gates help               this list

Kinds you can trust: """ + ", ".join(c for c, v in CLASSES.items() if v[1]) + """
Kinds that always ask: """ + ", ".join(c for c, v in CLASSES.items() if not v[1])

COMMAND = re.compile(r"\s*gates\s+(status|log|help|yes|no|untrust(?:\s+\w+)?|trust\s+\w+|text\s+(?:on|off))\s*", re.I)


def _status(session: Session) -> str:
    from .channels import text_enabled

    config = load_config()
    channel = (config.get("text") or {}).get("channel") or "not set up"
    pending = session.data.get("pending")
    lines = [
        f"Trusted this session: {', '.join(session.trusted) or 'nothing'}",
        f"Texting: {'on' if text_enabled(config) else 'off'} ({channel})",
        f"Waiting for you: {pending['summary'] if pending else 'nothing'}",
    ]
    return "\n".join(lines)


def _log_text() -> str:
    rows = read_audit(20)
    if not rows:
        return "Nothing logged yet."
    return "\n".join(f"{r['at'][5:16].replace('T', ' ')}  {r['event']:<9} {r['class'] or '-':<12} {r['summary']}" + (f"  ({r['via']})" if r.get("via") else "") for r in rows)


def _reply(agent: str, text: str) -> int:
    payload: dict = {"decision": "block", "reason": text}
    if agent == "claude":
        payload["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}
    _emit(payload)
    return 0


def _tell_agent(text: str) -> int:
    _emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}})
    return 0


def _prompt(payload: dict, agent: str) -> int:
    """Only a person's typed message reaches this, so it can hold a person's commands."""
    clean_old_sessions()
    match = COMMAND.fullmatch(payload.get("prompt") or "")
    if not match:
        return 0
    words = match.group(1).lower().split()
    command = words[0]
    session = Session.named(payload.get("session_id") or "default")

    with session.edit():
        if command == "help":
            return _reply(agent, HELP)
        if command == "status":
            return _reply(agent, _status(session))
        if command == "log":
            return _reply(agent, _log_text())
        if command == "trust":
            kind = words[1]
            if kind not in CLASSES:
                return _reply(agent, f"Unknown kind: {kind}. Try `gates help`.")
            if not CLASSES[kind][1]:
                return _reply(agent, f"{kind} can never be trusted. It always asks.")
            session.trust(kind)
            return _reply(agent, f"Trusted until this session ends: {kind}. It still shows up in the log.")
        if command == "untrust":
            session.untrust(words[1] if len(words) > 1 else None)
            return _reply(agent, f"Trusted now: {', '.join(session.trusted) or 'nothing'}")
        if command == "yes":
            pending = session.take_pending()
            if not pending:
                return _reply(agent, "Nothing is waiting for approval.")
            session.approve_once(pending["key"])
            return _tell_agent(f"The human typed `gates yes` and approved this once: {pending['summary']}. Retry that exact call now, with identical input.")
        if command == "no":
            pending = session.take_pending()
            if not pending:
                return _reply(agent, "Nothing is waiting for approval.")
            return _tell_agent(f"The human typed `gates no` and refused this: {pending['summary']}. Do not retry it or try another way to do it.")

    # gates text on|off
    from .channels import text_enabled

    config = load_config()
    text = dict(config.get("text") or {})
    if words[1] == "on" and not text.get("channel"):
        return _reply(agent, "No text channel is set up. Run `surya-gates text setup` in your own terminal first.")
    text["enabled"] = words[1] == "on"
    config["text"] = text
    save_config(config)
    return _reply(agent, f"Texting is {'on' if text_enabled(config) else 'off'}.")


def _end(payload: dict) -> int:
    Session.named(payload.get("session_id") or "default").forget()
    clean_old_sessions()
    return 0


def main(agent: str, event: str) -> int:
    def stop_waiting(_signal: int, _frame: object) -> None:
        sys.exit(BLOCK)  # runs the cleanup that releases this request's code

    signal.signal(signal.SIGTERM, stop_waiting)
    if agent not in ("claude", "codex"):
        print(f"Surya Gates failed closed: unknown agent {agent}", file=sys.stderr)
        return BLOCK
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if event == "pre":
            return _pre_tool(payload, agent)
        if event == "prompt":
            return _prompt(payload, agent)
        if event == "end":
            return _end(payload)
        print(f"Surya Gates failed closed: unknown event {event}", file=sys.stderr)
        return BLOCK
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - any failure must block rather than let the call through
        print(f"Surya Gates failed closed, so this did not run ({type(error).__name__}: {redact(str(error))[:200]})", file=sys.stderr)
        return BLOCK
