"""Local stand-ins for Telegram, Twilio and ntfy over real HTTP, and for Messages on disk."""

import base64
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAT = 5550001
ME = "+15550000001"
HUMAN = "+15550000002"


class _Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.sent = []
        self.on_send = None

    def code(self, index=-1):
        return re.search(r"YES (\d{4})", self.sent[index]).group(1)

    def wait_for_send(self, count=1, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if len(self.sent) >= count:
                return
            time.sleep(0.01)
        raise AssertionError("nothing was sent")

    def record(self, text):
        with self.lock:
            self.sent.append(text)
        if self.on_send:
            threading.Thread(target=self.on_send, args=(text,), daemon=True).start()


class _Server(_Recorder):
    def __init__(self):
        super().__init__()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, code, payload, raw=None):
                body = raw.encode() if raw is not None else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_GET(self):
                outer.handle(self, "GET")

            def do_POST(self):
                outer.handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeTelegram(_Server):
    TOKEN = "123:fake"

    def __init__(self):
        self.updates, self.next_id = [], 100
        super().__init__()

    def reply(self, text, sender=CHAT, chat=CHAT, chat_type="private"):
        with self.lock:
            self.next_id += 1
            self.updates.append({"update_id": self.next_id, "message": {"message_id": self.next_id, "date": int(time.time()), "text": text, "from": {"id": sender}, "chat": {"id": chat, "type": chat_type}}})

    def handle(self, request, method):
        data = json.loads(request.body() or b"{}")
        if f"/bot{self.TOKEN}/" not in request.path:
            return request.respond(401, {"ok": False})
        if request.path.endswith("/sendMessage"):
            self.record(data["text"])
            return request.respond(200, {"ok": True, "result": {}})
        if request.path.endswith("/getUpdates"):
            offset = data.get("offset", 0)
            with self.lock:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
                result = list(self.updates)
            return request.respond(200, {"ok": True, "result": result})
        request.respond(404, {"ok": False})


class FakeTwilio(_Server):
    SID = "ACfake"
    TOKEN = "faketoken"

    def __init__(self):
        self.inbound = []
        super().__init__()

    def reply(self, text, sender=HUMAN):
        with self.lock:
            self.inbound.insert(0, {"sid": f"SM{len(self.inbound)}", "from": sender, "to": ME, "body": text, "direction": "inbound", "date_sent": formatdate(time.time(), usegmt=True)})

    def handle(self, request, method):
        expected = "Basic " + base64.b64encode(f"{self.SID}:{self.TOKEN}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            return request.body() and None or request.respond(401, {})
        parsed = urllib.parse.urlparse(request.path)
        if not parsed.path.endswith(f"/Accounts/{self.SID}/Messages.json"):
            return request.respond(404, {})
        if method == "POST":
            form = urllib.parse.parse_qs(request.body().decode())
            assert form["From"] == [ME] and form["To"] == [HUMAN]
            self.record(form["Body"][0])
            return request.respond(201, {"sid": "SMout"})
        query = urllib.parse.parse_qs(parsed.query)
        with self.lock:
            rows = [m for m in self.inbound if m["from"] == query["From"][0] and m["to"] == query["To"][0]]
        assert "DateSent>" in query
        request.respond(200, {"messages": rows})


class FakeNtfy(_Server):
    """ntfy's publish and poll API. A button tap is a POST to the reply topic."""

    TOPIC = "gates-test-topic-abc123"

    def __init__(self, token=""):
        self.token, self.replies, self.published = token, [], []
        super().__init__()

    def reply(self, text):
        with self.lock:
            self.replies.append({"id": f"m{len(self.replies)}", "time": int(time.time()), "event": "message", "topic": f"{self.TOPIC}-reply", "message": text})

    def handle(self, request, method):
        if self.token and request.headers.get("Authorization") != f"Bearer {self.token}":
            request.body()
            return request.respond(403, {"error": "forbidden"})
        parsed = urllib.parse.urlparse(request.path)
        if method == "POST" and parsed.path == "/":
            data = json.loads(request.body())
            assert data["topic"] == self.TOPIC
            self.published.append(data)
            self.record(data["message"] + "".join("\n" + a["body"] for a in data.get("actions") or []))
            return request.respond(200, {"id": "out", "time": int(time.time()), "event": "message", "topic": self.TOPIC})
        if method == "POST" and parsed.path == f"/{self.TOPIC}-reply":
            self.reply(request.body().decode())
            return request.respond(200, {"id": "in", "event": "message"})
        if method == "GET" and parsed.path == f"/{self.TOPIC}-reply/json":
            query = urllib.parse.parse_qs(parsed.query)
            assert query["poll"] == ["1"]
            since = int(query["since"][0])
            with self.lock:
                lines = [json.dumps(m) for m in self.replies if m["time"] >= since]
            return request.respond(200, None, raw="".join(line + "\n" for line in lines))
        request.respond(404, {})


APPLE_EPOCH = 978307200


def typedstream(text):
    """How newer macOS stores a message's text in attributedBody."""
    data = text.encode("utf-8")
    size = bytes([len(data)]) if len(data) < 0x80 else b"\x81" + len(data).to_bytes(2, "little")
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + size + data + b"\x86\x84\x02iI\x01"


SCHEMA = """
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE, text TEXT, attributedBody BLOB, handle_id INTEGER DEFAULT 0, date INTEGER, is_from_me INTEGER DEFAULT 0, service TEXT);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, chat_identifier TEXT, service_name TEXT);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
"""

OSASCRIPT = """#!{python}
import json, sqlite3, sys, time, uuid
sys.stdin.read()
text, to = sys.argv[2], sys.argv[3]
if to != {human!r}:
    sys.stderr.write("execution error: Can't get participant (-1728)")
    sys.exit(1)
sys.path.insert(0, {tests!r})
from fakes import add_message
add_message({db!r}, text, to, mine=True)
with open({log!r}, "a") as log:
    log.write(json.dumps(text) + "\\n")
"""


def add_message(db, text, chat, mine=False, sender=None, plain=False):
    with sqlite3.connect(db, timeout=5) as conn:
        row = conn.execute("SELECT ROWID FROM chat WHERE chat_identifier = ?", (chat,)).fetchone()
        chat_id = row[0] if row else conn.execute("INSERT INTO chat (chat_identifier, service_name) VALUES (?, 'iMessage')", (chat,)).lastrowid
        handle_id = 0
        if not mine:
            who = sender or chat
            row = conn.execute("SELECT ROWID FROM handle WHERE id = ?", (who,)).fetchone()
            handle_id = row[0] if row else conn.execute("INSERT INTO handle (id, service) VALUES (?, 'iMessage')", (who,)).lastrowid
        date = int((time.time() - APPLE_EPOCH) * 1_000_000_000)
        guid = f"G{time.time_ns()}{os.getpid()}"
        message = conn.execute(
            "INSERT INTO message (guid, text, attributedBody, handle_id, date, is_from_me, service) VALUES (?, ?, ?, ?, ?, ?, 'iMessage')",
            (guid, text if plain else None, None if plain else typedstream(text), handle_id, date, int(mine)),
        ).lastrowid
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (chat_id, message))


class FakeMessages(_Recorder):
    """A Messages database on disk and an osascript on PATH that writes to it."""

    def __init__(self):
        self.lock, self.on_send = threading.Lock(), None
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "chat.db")
        self.bin = os.path.join(self.dir, "bin")
        self.log = os.path.join(self.dir, "sent.jsonl")
        os.mkdir(self.bin)
        with sqlite3.connect(self.db) as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
        script = os.path.join(self.bin, "osascript")
        with open(script, "w") as handle:
            handle.write(OSASCRIPT.format(python=sys.executable, human=HUMAN, tests=os.path.dirname(os.path.abspath(__file__)), db=self.db, log=self.log))
        os.chmod(script, 0o755)
        open(self.log, "w").close()
        self.running = True
        threading.Thread(target=self._watch, daemon=True).start()

    @property
    def sent(self):
        """Read straight from the log, so a text counts as sent once osascript returns."""
        try:
            with open(self.log) as handle:
                return [json.loads(line) for line in handle.read().splitlines()]
        except OSError:
            return []

    def _watch(self):
        seen = 0
        while self.running:
            sent = self.sent
            for text in sent[seen:]:
                if self.on_send:
                    threading.Thread(target=self.on_send, args=(text,), daemon=True).start()
            seen = len(sent)
            time.sleep(0.01)

    def reply(self, text, sender=HUMAN, plain=False):
        add_message(self.db, text, sender, sender=sender, plain=plain)

    def close(self):
        self.running = False
        time.sleep(0.03)
        shutil.rmtree(self.dir, ignore_errors=True)
