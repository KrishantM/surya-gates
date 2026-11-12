"""Command line: what a person runs in their own terminal, and the hook entry point."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .rules import CLASSES, classify
from .session import home, load_config, read_audit, save_config

PACKAGE = Path(__file__).resolve().parent
INSTALLED = home() / "runtime"
LAUNCHER = """#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from surya_gates.cli import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
"""
# Hooks get this long, and Gates always answers before it.
HOOK_TIMEOUT = 86400


def _secret(variable: str, prompt: str) -> str:
    return os.environ.get(variable) or getpass.getpass(f"{prompt}: ").strip()


def _line(variable: str, prompt: str) -> str:
    return os.environ.get(variable) or input(f"{prompt}: ").strip()


def setup_telegram() -> dict:
    from .channels import ChannelError, Telegram

    print("1. In Telegram, message @BotFather, send /newbot, and copy the token it gives you.")
    token = _secret("SURYA_GATES_TELEGRAM_TOKEN", "Bot token (hidden)")
    base = os.environ.get("SURYA_GATES_TELEGRAM_BASE", "https://api.telegram.org")
    bot = Telegram(token, "", base)
    print("2. Now open your bot in Telegram and send it any message. Waiting up to 2 minutes...")
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            updates = bot.updates(None)
        except ChannelError as error:
            print(f"Telegram didn't accept that: {error}")
            return {}
        for update in reversed(updates):
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("type") == "private" and (message.get("from") or {}).get("id") == chat.get("id"):
                return {"channel": "telegram", "token": token, "chat_id": str(chat["id"]), "base_url": base}
        time.sleep(2)
    print("No message arrived.")
    return {}


def setup_twilio() -> dict:
    print("You need a Twilio account and a number that can send and receive SMS.")
    print("US numbers need A2P 10DLC or toll-free verification before messages are delivered.")
    return {
        "channel": "twilio",
        "sid": _line("SURYA_GATES_TWILIO_SID", "Account SID"),
        "token": _secret("SURYA_GATES_TWILIO_TOKEN", "Auth token (hidden)"),
        "from": _line("SURYA_GATES_TWILIO_FROM", "Your Twilio number, like +15551234567"),
        "to": _line("SURYA_GATES_TWILIO_TO", "Your phone number, like +15551234567"),
        "base_url": os.environ.get("SURYA_GATES_TWILIO_BASE", "https://api.twilio.com"),
    }


def _wait_for_yes(channel, code: str, seconds: int = 180) -> bool:
    from .channels import ChannelError, parse_reply

    started = time.time()
    while time.time() < started + seconds:
        try:
            for message in channel.fetch(started):
                parsed = parse_reply(message.text)
                if parsed and parsed[0] == "yes" and parsed[1] == code:
                    return True
        except ChannelError as error:
            print(f"Couldn't check for your answer: {error}")
            return False
        time.sleep(2)
    print("No answer arrived.")
    return False


def setup_ntfy() -> dict:
    import secrets

    from .channels import ChannelError, Ntfy, new_topic

    server = os.environ.get("SURYA_GATES_NTFY_SERVER", "https://ntfy.sh")
    topic = os.environ.get("SURYA_GATES_NTFY_TOPIC") or new_topic()
    token = os.environ.get("SURYA_GATES_NTFY_TOKEN", "")
    try:
        channel = Ntfy(topic, server, token)
    except ChannelError as error:
        print(error)
        return {}
    print("1. Install the ntfy app on your phone.")
    print(f"2. Subscribe to this topic{'' if server == 'https://ntfy.sh' else f' on {server}'}:\n\n   {topic}\n")
    print("   Keep it private. Anyone who knows it can read your approvals and answer them.")
    _line("SURYA_GATES_NTFY_READY", "3. Press Enter once you've subscribed")
    code = str(secrets.randbelow(9000) + 1000)
    try:
        channel.publish("Tap Connect to receive Surya Gates approvals here.", [channel.button("Connect", f"YES {code}")])
    except ChannelError as error:
        print(f"ntfy didn't accept that: {error}")
        return {}
    print("4. Tap Connect on the notification. On iPhone, open it in the ntfy app to see the button. Waiting up to 3 minutes...")
    if not _wait_for_yes(channel, code):
        return {}
    return {"channel": "ntfy", "topic": topic, "server": server, "token": token}


def setup_imessage() -> dict:
    import secrets

    from .channels import ChannelError, IMessage

    if sys.platform != "darwin":
        print("iMessage works on a Mac only. Try ntfy or Telegram.")
        return {}
    print("Gates texts you from this Mac's Messages app and reads your replies.")
    print("The app that runs your agent (Terminal, iTerm, Claude, your editor) needs Full Disk Access")
    print("to read replies, and permission to control Messages. macOS asks for the second one.")
    channel = IMessage(_line("SURYA_GATES_IMESSAGE_TO", "Your iPhone's number or Apple ID email"), os.environ.get("SURYA_GATES_IMESSAGE_DB", "~/Library/Messages/chat.db"))
    try:
        channel.fetch(time.time())
    except ChannelError as error:
        print(f"Not yet: {error}.")
        return {}
    code = str(secrets.randbelow(9000) + 1000)
    try:
        channel.send(f"Surya Gates wants to text you approvals here. Reply YES {code} to connect.")
    except ChannelError as error:
        print(f"Not yet: {error}.")
        return {}
    print(f"A text is on its way. Reply YES {code} from your phone. Waiting up to 3 minutes...")
    if not _wait_for_yes(channel, code):
        return {}
    return {"channel": "imessage", "to": channel.to}


SETUPS = {"telegram": setup_telegram, "twilio": setup_twilio, "ntfy": setup_ntfy, "imessage": setup_imessage}


def cmd_text(args: argparse.Namespace) -> int:
    from .channels import ChannelError, build_channel

    config = load_config()
    text = dict(config.get("text") or {})
    if args.action == "setup":
        if args.channel not in SETUPS:
            print(f"Choose one of: {', '.join(SETUPS)}")
            return 1
        chosen = SETUPS[args.channel]()
        if not chosen:
            return 1
        text = {**chosen, "enabled": True, "wait_seconds": text.get("wait_seconds", 1800)}
        config["text"] = text
        save_config(config)
        try:
            build_channel(config).send("Surya Gates is connected. Approvals will arrive here. Type `gates text off` in your agent to stop them.")
        except ChannelError as error:
            print(f"Saved, but the test message failed: {error}")
            return 1
        print("Connected, and a test message is on its way. Texting is on.")
        return 0
    if args.action in ("on", "off"):
        if args.action == "on" and not text.get("channel"):
            print("No text channel is set up. Run: surya-gates text setup ntfy (or imessage, telegram, twilio)")
            return 1
        text["enabled"] = args.action == "on"
        config["text"] = text
        save_config(config)
        print(f"Texting is {args.action}.")
        return 0
    if args.action == "wait":
        if not args.channel.isdigit():
            print("Say how many seconds: surya-gates text wait 3600")
            return 1
        text["wait_seconds"] = int(args.channel)
        config["text"] = text
        save_config(config)
        print(f"Gates will wait {text['wait_seconds']} seconds for an answer.")
        return 0
    if args.action == "test":
        try:
            build_channel(config).send("Surya Gates test message.")
        except ChannelError as error:
            print(f"Failed: {error}")
            return 1
        print("Sent.")
        return 0
    return 1


def cmd_check(args: argparse.Namespace) -> int:
    """For any language: a tool call as JSON on stdin, a verdict as JSON on stdout."""
    from .gate import tool_call

    try:
        call = tool_call(json.loads(sys.stdin.read()))
    except ValueError as error:
        print(json.dumps({"action": "stop", "class": "", "summary": f"Could not read this tool call: {error}", "trustable": False}))
        return 0
    verdict = classify(call.name, call.args, args.cwd, args.writable or (), by_name=True)
    print(json.dumps({"action": verdict.action, "class": verdict.cls, "summary": verdict.summary, "trustable": verdict.trustable}))
    return 0


def cmd_classes(_args: argparse.Namespace) -> int:
    for name, (label, trustable) in CLASSES.items():
        print(f"{name:<13} {'can be trusted' if trustable else 'always asks   '}  {label}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    from .channels import text_enabled

    config = load_config()
    text = config.get("text") or {}
    print(f"Surya Gates {__version__}")
    print(f"Texting: {'on' if text_enabled(config) else 'off'} ({text.get('channel') or 'not set up'})")
    print(f"State: {home()}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    for row in read_audit(args.n):
        print(f"{row['at'][:19]}  {row['event']:<9} {row['class'] or '-':<12} {row['summary']}" + (f"  ({row['via']})" if row.get("via") else ""))
    return 0


def _codex_hooks() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


def _codex_entry(event: str) -> dict:
    command = f'python3 "{INSTALLED / "scripts" / "surya-gates"}" hook codex {event} || exit 2'
    return {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]}


def _is_ours(entry: dict) -> bool:
    return any("surya-gates" in str(h.get("command", "")) for h in entry.get("hooks", []))


def cmd_install(args: argparse.Namespace) -> int:
    if args.agent == "claude":
        print("In Claude Code, run:\n  /plugin marketplace add KrishantM/surya-gates\n  /plugin install surya-gates@surya-gates")
        return 0
    # A stable copy, so Codex keeps working if this checkout moves.
    if PACKAGE.parent.resolve() != INSTALLED.resolve():
        shutil.copytree(PACKAGE, INSTALLED / "surya_gates", ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    launcher = INSTALLED / "scripts" / "surya-gates"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)
    path = _codex_hooks()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except ValueError:
        print(f"{path} isn't valid JSON, so it was left alone.")
        return 1
    if path.exists():
        shutil.copy2(path, path.with_name(f"hooks.json.backup-{int(time.time())}"))
    hooks = data.setdefault("hooks", {})
    for event, tag in (("PreToolUse", "pre"), ("UserPromptSubmit", "prompt")):
        hooks[event] = [e for e in hooks.get(event, []) if not _is_ours(e)] + [_codex_entry(tag)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Added to {path}.\nCodex won't run new hooks until you approve them. Start Codex, type /hooks and trust the Surya Gates entries.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    path = _codex_hooks()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("Nothing to remove.")
        return 0
    shutil.copy2(path, path.with_name(f"hooks.json.backup-{int(time.time())}"))
    hooks = data.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not _is_ours(e)]
        if not hooks[event]:
            del hooks[event]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Removed from {path}.")
    return 0


def console() -> None:
    sys.exit(main(sys.argv[1:]))


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="surya-gates", description="Approval gates for AI agents.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    hook = commands.add_parser("hook", help="used by the agent's hooks")
    hook.add_argument("agent")
    hook.add_argument("event")

    text = commands.add_parser("text", help="approve by text")
    text.add_argument("action", choices=["setup", "on", "off", "test", "wait"])
    text.add_argument("channel", nargs="?", default="ntfy", help="ntfy, imessage, telegram or twilio for setup, seconds for wait")

    check = commands.add_parser("check", help="judge a tool call: JSON in, JSON out")
    check.add_argument("--cwd")
    check.add_argument("--writable", action="append")

    commands.add_parser("classes", help="list the kinds of action")
    commands.add_parser("status", help="show settings")
    log = commands.add_parser("log", help="show recent decisions")
    log.add_argument("-n", type=int, default=20)
    install = commands.add_parser("install", help="add Gates to an agent")
    install.add_argument("agent", choices=["claude", "codex"])
    uninstall = commands.add_parser("uninstall", help="remove Gates from Codex")
    uninstall.add_argument("agent", choices=["codex"])

    args = parser.parse_args(argv)
    if args.command == "hook":
        from .hook import main as hook_main

        return hook_main(args.agent, args.event)
    return {"text": cmd_text, "check": cmd_check, "classes": cmd_classes, "status": cmd_status, "log": cmd_log, "install": cmd_install, "uninstall": cmd_uninstall}[args.command](args)
