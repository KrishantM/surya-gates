"""Optional approvals away from the keyboard: ntfy, iMessage, Telegram or Twilio SMS.

Every channel is polled, so nothing needs a public webhook.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from .session import file_lock, home, load_config, redact, write_private

DEFAULT_WAIT = 1800
MAX_WAIT = 82800  # stays under the 24 hour limit the hooks are installed with
POLL_SECONDS = 3
MAX_WRONG = 5
INBOX_KEEP_SECONDS = 2 * 86400
# A reply needs the one-time code, so an early timestamp can't match an old reply.
CLOCK_SLACK = 300
MAX_DETAIL = 240


class ChannelError(Exception):
    """Sending or receiving failed."""


@dataclass(frozen=True)
class Message:
    id: str
    text: str
    at: float


def _http(request: urllib.request.Request, timeout: float = 20) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the base URL is http(s) by construction
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        raise ChannelError(f"the service answered HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ChannelError(f"could not reach the service ({type(error).__name__})") from None


def _base(url: str) -> str:
    if not re.match(r"^https?://", url):
        raise ChannelError("base_url must be http or https")
    return url.rstrip("/")


class Telegram:
    """A bot you message privately. No carrier registration, free."""

    name = "telegram"

    def __init__(self, token: str, chat_id: object, base_url: str = "https://api.telegram.org") -> None:
        self.token, self.chat_id, self.base = token, str(chat_id), _base(base_url)
        self.confirm = lambda: None

    def _url(self, method: str) -> str:
        return f"{self.base}/bot{self.token}/{method}"

    def _call(self, method: str, body: dict) -> dict:
        request = urllib.request.Request(self._url(method), json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
        answer = _http(request)
        if not answer.get("ok"):
            raise ChannelError("Telegram refused the request")
        return answer

    def send(self, text: str) -> None:
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text})

    def updates(self, offset: Optional[int]) -> list:
        body: dict = {"timeout": 0, "allowed_updates": ["message"]}
        if offset is not None:
            body["offset"] = offset
        return self._call("getUpdates", body).get("result", [])

    def fetch(self, since: float) -> list:
        """New messages from the verified person only."""
        state = home() / "telegram-offset"
        try:
            offset: Optional[int] = int(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            offset = None
        found, last = [], None
        for update in self.updates(offset):
            last = update.get("update_id", last)
            message = update.get("message") or {}
            chat, sender = message.get("chat") or {}, message.get("from") or {}
            if str(sender.get("id")) == self.chat_id and str(chat.get("id")) == self.chat_id and chat.get("type") == "private" and message.get("text"):
                found.append(Message(f"tg{update.get('update_id')}", message["text"], float(message.get("date", 0))))
        # Only confirmed once the messages are saved, so a crash can't lose a reply.
        self.confirm = (lambda: write_private(state, str(last + 1))) if last is not None else (lambda: None)
        return found


class Twilio:
    """Real SMS. US numbers need A2P 10DLC registration, which Twilio charges for."""

    name = "twilio"

    def __init__(self, sid: str, token: str, from_number: str, to_number: str, base_url: str = "https://api.twilio.com") -> None:
        self.sid, self.token, self.from_number, self.to_number, self.base = sid, token, from_number, to_number, _base(base_url)
        self.confirm = lambda: None

    def _headers(self) -> dict:
        pair = base64.b64encode(f"{self.sid}:{self.token}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {pair}"}

    @property
    def _messages(self) -> str:
        return f"{self.base}/2010-04-01/Accounts/{self.sid}/Messages.json"

    def send(self, text: str) -> None:
        body = urllib.parse.urlencode({"To": self.to_number, "From": self.from_number, "Body": text}).encode("utf-8")
        _http(urllib.request.Request(self._messages, body, self._headers()))

    def fetch(self, since: float) -> list:
        day = time.strftime("%Y-%m-%d", time.gmtime(since - 86400))
        query = urllib.parse.urlencode({"To": self.from_number, "From": self.to_number, "DateSent>": day, "PageSize": 50})
        answer = _http(urllib.request.Request(f"{self._messages}?{query}", headers=self._headers()))
        found = []
        for item in answer.get("messages", []):
            if item.get("from") != self.to_number or not str(item.get("direction", "")).startswith("inbound"):
                continue
            try:
                at = parsedate_to_datetime(item.get("date_sent") or item.get("date_created")).timestamp()
            except (TypeError, ValueError):
                continue
            found.append(Message(str(item.get("sid")), str(item.get("body", "")), at))
        return found


class Ntfy:
    """Push notifications with Yes, No and Trust buttons. Free, no account.

    Questions go to the topic your phone subscribes to. The buttons post the answer
    to a second topic, so your phone isn't notified about its own answers.
    """

    name = "ntfy"
    buttons = True

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str = "") -> None:
        if not re.fullmatch(r"[-_A-Za-z0-9]{1,58}", topic or ""):
            raise ChannelError("the ntfy topic may use letters, digits, - and _ only")
        self.topic, self.base, self.token = topic, _base(server), token
        self.reply_topic = f"{topic}-reply"
        self.confirm = lambda: None

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def publish(self, text: str, actions: Optional[list] = None, priority: int = 4) -> None:
        body: dict = {"topic": self.topic, "title": "Surya Gates", "message": text, "priority": priority, "tags": ["lock"]}
        if actions:
            body["actions"] = actions
        _http(urllib.request.Request(self.base + "/", json.dumps(body).encode("utf-8"), self._headers()))

    def button(self, label: str, answer: str) -> dict:
        action = {"action": "http", "label": label, "url": f"{self.base}/{self.reply_topic}", "method": "POST", "body": answer, "clear": True}
        if self.token:
            action["headers"] = {"Authorization": f"Bearer {self.token}"}
        return action

    def send(self, text: str) -> None:
        self.publish(text, priority=3)

    def send_question(self, text: str, code: str, trustable: bool) -> None:
        actions = [self.button("Yes", f"YES {code}"), self.button("No", f"NO {code}")]
        if trustable:
            actions.append(self.button("Trust", f"TRUST {code}"))
        self.publish(text, actions)

    def fetch(self, since: float) -> list:
        query = urllib.parse.urlencode({"poll": "1", "since": str(max(0, int(since) - CLOCK_SLACK))})
        request = urllib.request.Request(f"{self.base}/{self.reply_topic}/json?{query}", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - http(s) by construction
                lines = response.read().decode("utf-8").splitlines()
        except urllib.error.HTTPError as error:
            error.close()
            raise ChannelError(f"the service answered HTTP {error.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ChannelError(f"could not reach the service ({type(error).__name__})") from None
        found = []
        for line in lines:
            with contextlib.suppress(ValueError, TypeError):
                item = json.loads(line)
                if item.get("event") == "message" and item.get("topic") == self.reply_topic:
                    found.append(Message(f"ntfy{item['id']}", str(item.get("message", "")), float(item.get("time", 0))))
        return found


APPLE_EPOCH = 978307200
SEND_IMESSAGE = """on run argv
    tell application "Messages"
        set gatesAccount to 1st account whose service type = iMessage
        send (item 1 of argv) to participant (item 2 of argv) of gatesAccount
    end tell
end run
"""


def _handle(value: str) -> str:
    """Phone numbers as Messages stores them, emails in lower case."""
    value = value.strip()
    if "@" in value:
        return value.lower()
    digits = re.sub(r"[^\d+]", "", value)
    return digits


def _body_text(blob: Optional[bytes]) -> str:
    """The text inside an attributedBody, where newer macOS keeps most messages."""
    if not blob:
        return ""
    start = blob.find(b"NSString")
    if start < 0:
        return ""
    plus = blob.find(b"+", start + 8, start + 20)
    if plus < 0:
        return ""
    at = plus + 1
    size = blob[at]
    at += 1
    if size == 0x81:
        size = int.from_bytes(blob[at:at + 2], "little")
        at += 2
    elif size == 0x82:
        size = int.from_bytes(blob[at:at + 4], "little")
        at += 4
    return blob[at:at + size].decode("utf-8", "replace")


class IMessage:
    """Texts from this Mac's Messages app. Free, macOS only.

    Reading replies needs Full Disk Access for the app that runs your agent, and
    sending needs permission to control Messages. Replies are read from the
    Messages database, read only.
    """

    name = "imessage"

    def __init__(self, to: str, db: str = "~/Library/Messages/chat.db") -> None:
        if not to:
            raise ChannelError("say which phone number or Apple ID to text")
        self.to = _handle(to)
        self.db = Path(db).expanduser()
        self.confirm = lambda: None

    def send(self, text: str) -> None:
        if sys.platform != "darwin":
            raise ChannelError("iMessage works on a Mac only")
        try:
            done = subprocess.run(["osascript", "-", text, self.to], input=SEND_IMESSAGE, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ChannelError(f"Messages didn't answer ({type(error).__name__})") from None
        if done.returncode != 0:
            if "-1743" in done.stderr or "not allowed" in done.stderr.lower():
                raise ChannelError("this app isn't allowed to control Messages. Allow it in System Settings, Privacy & Security, Automation")
            raise ChannelError(f"Messages refused to send ({' '.join(done.stderr.split())[:160]})")

    def fetch(self, since: float) -> list:
        after = (since - CLOCK_SLACK - APPLE_EPOCH) * 1_000_000_000
        try:
            with contextlib.closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                rows = db.execute(
                    """SELECT m.guid, m.text, m.attributedBody, m.date FROM message m
                       JOIN chat_message_join j ON j.message_id = m.ROWID
                       JOIN chat c ON c.ROWID = j.chat_id
                       LEFT JOIN handle h ON h.ROWID = m.handle_id
                       WHERE (c.chat_identifier = ? OR h.id = ?) AND m.date >= ?
                       ORDER BY m.ROWID DESC LIMIT 200""",
                    (self.to, self.to, after),
                ).fetchall()
        except sqlite3.Error:
            raise ChannelError("can't read Messages. Give Full Disk Access to the app that runs your agent, in System Settings, Privacy & Security") from None
        found = []
        for guid, text, body, date in rows:
            seconds = date / 1_000_000_000 if date > 10**12 else date
            found.append(Message(f"im{guid}", text or _body_text(body), seconds + APPLE_EPOCH))
        return found


def build_channel(config: dict):
    """The configured channel. Raises ChannelError if settings are missing."""
    text = config.get("text") or {}
    kind = text.get("channel")
    try:
        if kind == "telegram":
            return Telegram(text["token"], text["chat_id"], text.get("base_url", "https://api.telegram.org"))
        if kind == "twilio":
            return Twilio(text["sid"], text["token"], text["from"], text["to"], text.get("base_url", "https://api.twilio.com"))
        if kind == "ntfy":
            return Ntfy(text["topic"], text.get("server", "https://ntfy.sh"), text.get("token", ""))
        if kind == "imessage":
            return IMessage(text["to"], text.get("db", "~/Library/Messages/chat.db"))
    except KeyError as missing:
        raise ChannelError(f"text setup is missing {missing}") from None
    raise ChannelError("no text channel is set up")


def guarded(config: dict) -> list:
    """Text an agent's tool call must never contain, or it could answer its own gate."""
    text = config.get("text") or {}
    words = [str(text.get(key) or "") for key in ("token", "topic", "sid")]
    if text.get("channel") == "imessage":
        words.append("Library/Messages")
    return [w for w in words if len(w) >= 8]


def text_enabled(config: dict) -> bool:
    return bool((config.get("text") or {}).get("enabled")) and bool((config.get("text") or {}).get("channel"))


def wait_seconds(config: dict) -> int:
    try:
        value = int((config.get("text") or {}).get("wait_seconds", DEFAULT_WAIT))
    except (TypeError, ValueError):
        value = DEFAULT_WAIT
    return max(5, min(value, MAX_WAIT))


class Inbox:
    """A shared record of replies. Several requests may wait at once, across sessions.

    Whoever holds the lock fetches and everyone reads the file, because Telegram
    allows one reader at a time and forgets what it has confirmed.
    """

    def __init__(self, channel) -> None:
        self.channel = channel
        self.path = home() / "inbox.jsonl"

    def read(self) -> list:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(ValueError, KeyError):
                row = json.loads(line)
                out.append(Message(row["id"], row["text"], row["at"]))
        return out

    def refresh(self, since: float) -> None:
        with file_lock(home() / "inbox.lock", blocking=False) as got:
            if not got:
                return
            fresh = self.channel.fetch(since)
            now = time.time()
            kept = [m for m in self.read() if now - m.at < INBOX_KEEP_SECONDS]
            known = {m.id for m in kept}
            merged = kept + [m for m in fresh if m.id not in known]
            if len(merged) != len(kept) or len(kept) != len(self.read()):
                write_private(self.path, "".join(json.dumps(m.__dict__) + "\n" for m in merged))
            self.channel.confirm()


YES_WORDS = {"yes": "yes", "y": "yes", "approve": "yes", "ok": "yes", "no": "no", "n": "no", "deny": "no", "trust": "trust"}
_WORDS = "|".join(YES_WORDS)
_WORD_THEN_CODE = re.compile(rf"\s*({_WORDS})[\s:,.-]*(\d{{4}})\W*", re.I)
_CODE_THEN_WORD = re.compile(rf"\s*(\d{{4}})[\s:,.-]+({_WORDS})\W*", re.I)
_CODE_THEN_TEXT = re.compile(r"\s*(\d{4})[\s:,.-]+(.+?)\s*", re.S)


def parse_reply(text: str) -> Optional[tuple]:
    """Returns (decision, code, instruction), or None if this isn't an answer."""
    match = _WORD_THEN_CODE.fullmatch(text)
    if match:
        return YES_WORDS[match.group(1).lower()], match.group(2), ""
    match = _CODE_THEN_WORD.fullmatch(text)
    if match:
        return YES_WORDS[match.group(2).lower()], match.group(1), ""
    match = _CODE_THEN_TEXT.fullmatch(text)
    if match:
        return "reroute", match.group(1), match.group(2)
    return None


def _pending_dir() -> Path:
    path = home() / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _claim_code(deadline: float) -> tuple:
    for _ in range(50):
        code = str(secrets.randbelow(9000) + 1000)
        path = _pending_dir() / code
        try:
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(deadline))
        return code, path
    raise ChannelError("too many requests are waiting")


def active_codes() -> set:
    now, codes = time.time(), set()
    for item in _pending_dir().iterdir():
        try:
            if float(item.read_text(encoding="utf-8")) > now:
                codes.add(item.name)
            else:
                item.unlink()
        except (OSError, ValueError):
            continue
    return codes


def question(summary: str, detail: str, project: str, code: str, trustable: bool, show_detail: bool = True, buttons: bool = False) -> str:
    lines = [f"Surya Gates: {summary}"]
    if show_detail and detail:
        clean = " ".join(redact(detail).split())
        lines.append(clean if len(clean) <= MAX_DETAIL else clean[:MAX_DETAIL - 3] + "...")
    if project:
        lines.append(f"Project: {project}")
    if buttons:
        lines.append(f"Request {code}. Tap Yes or No." + (" Trust allows this kind until the session ends." if trustable else ""))
    else:
        lines.append(f"Reply YES {code} or NO {code}, or {code} then what to do instead." + (f" TRUST {code} allows this kind until the session ends." if trustable else ""))
    return "\n".join(lines)


def ask_human(channel, summary: str, detail: str, project: str, trustable: bool, wait: int, show_detail: bool = True, poll_seconds: float = POLL_SECONDS) -> Optional[dict]:
    """Text a person and wait. Returns {decision, instruction}, or None on no answer.

    Raises ChannelError if the question can't be sent.
    """
    started = time.time()
    deadline = started + wait
    code, claim = _claim_code(deadline)
    inbox = Inbox(channel)
    def tell(text: str) -> None:
        with contextlib.suppress(ChannelError):
            channel.send(text)
    try:
        text = question(summary, detail, project, code, trustable, show_detail, getattr(channel, "buttons", False))
        if hasattr(channel, "send_question"):
            channel.send_question(text, code, trustable)
        else:
            channel.send(text)
        seen: set = set()
        wrong, trouble = 0, 0
        while time.time() < deadline:
            try:
                inbox.refresh(started)
                trouble = 0
            except ChannelError:
                trouble += 1  # keep trying until the deadline, the phone may come back
            active = active_codes()
            for message in inbox.read():
                if message.id in seen or message.at < started - CLOCK_SLACK:
                    continue
                seen.add(message.id)
                parsed = parse_reply(message.text)
                if parsed is None:
                    continue
                decision, reply_code, instruction = parsed
                if reply_code == code:
                    if decision == "trust" and not trustable:
                        tell(f"This kind can't be trusted. Reply YES {code} or NO {code}.")
                        continue
                    tell({"yes": f"Approved ({code}).", "trust": f"Approved and trusted for this session ({code}).", "no": f"Denied ({code}).", "reroute": f"Passed on to the agent ({code})."}[decision])
                    return {"decision": decision, "instruction": instruction}
                if reply_code not in active:
                    wrong += 1
                    if wrong >= MAX_WRONG:
                        tell(f"Too many wrong codes. Denied ({code}).")
                        return {"decision": "no", "instruction": ""}
            time.sleep(min(poll_seconds, max(0.0, deadline - time.time())))
        tell(f"No answer in time. Denied ({code}).")
        return None
    finally:
        with contextlib.suppress(OSError):
            claim.unlink()


def text_approver(wait: Optional[int] = None, poll_seconds: float = POLL_SECONDS):
    """An approver for `Gate` that uses the text channel set up on this machine."""
    from .gate import Reply, Request

    def approve(request: Request) -> Reply:
        config = load_config()
        answer = ask_human(build_channel(config), request.summary, request.detail, "", request.trustable, wait or wait_seconds(config), poll_seconds=poll_seconds)
        if answer is None:
            return Reply("no", via="text, no answer")
        return Reply(answer["decision"], answer["instruction"], via="text")

    return approve
