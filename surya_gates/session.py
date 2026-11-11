"""What Gates remembers: session trust, the audit log and the settings file."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

LOOP_WINDOW = 10
LOOP_THRESHOLD = 3
ONCE_SECONDS = 900
STALE_SESSION_SECONDS = 7 * 86400

SECRET_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|\b[0-9a-f]{32,}\b|\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}|\b\d{6,}:[A-Za-z0-9_-]{30,})"
    r"|((?:password|passwd|secret|token|api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&]+"
    r"|((?:--(?:password|token|api-key|secret)\s+|bearer\s+))\S+",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Hide anything shaped like a secret before it is logged or texted."""
    return SECRET_SHAPES.sub(lambda m: (m.group(2) or m.group(3) or "") + "[hidden]", text or "")


def home() -> Path:
    return Path(os.environ.get("SURYA_GATES_HOME") or Path.home() / ".surya-gates")


def _private(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    _private(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


@contextlib.contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[bool]:
    """Hold an exclusive lock. Yields False when `blocking` is off and it is taken."""
    _private(path.parent)
    with open(path, "a+", encoding="utf-8") as handle:
        got = True
        if fcntl:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except OSError:
                got = False
        try:
            yield got
        finally:
            if fcntl and got:
                fcntl.flock(handle, fcntl.LOCK_UN)


def config_path() -> Path:
    return home() / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    write_private(config_path(), json.dumps(config, indent=1))


def digest(name: str, args: object) -> str:
    blob = json.dumps({"t": name, "a": args}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


READ_ONLY_SHELL = re.compile(r"^\s*(?:ls|cat|head|tail|grep|rg|find|wc|pwd|which|echo|stat|diff|git\s+(?:status|log|diff|show|branch))\b")
EDIT_TOOLS = {"write", "edit", "multiedit", "notebookedit", "apply_patch"}
POLLING = re.compile(r"\bsleep\s+\d")


def changes_something(name: str, args: object) -> bool:
    """Would this call mean a repeat of an earlier call is no longer the same attempt?"""
    low = name.lower()
    if low in EDIT_TOOLS:
        return True
    if low in ("bash", "shell", "shell_command", "exec_command", "unified_exec") and isinstance(args, dict):
        return not READ_ONLY_SHELL.match(str(args.get("command") or args.get("cmd") or ""))
    return False


class Session:
    """State for one run of an agent. Kept in memory, or in a file when `path` is set."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.data = self._blank()

    @staticmethod
    def _blank() -> dict:
        return {"trusted": [], "recent": [], "once": {}, "pending": None}

    @classmethod
    def named(cls, session_id: str) -> "Session":
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "default")[:120]
        return cls(home() / "sessions" / f"{safe}.json")

    @contextlib.contextmanager
    def edit(self) -> Iterator["Session"]:
        """Load, change and save as one step, because tools can run in parallel."""
        if self.path is None:
            yield self
            return
        with file_lock(self.path.with_suffix(".lock")):
            self.load()
            yield self
            write_private(self.path, json.dumps(self.data))

    def load(self) -> "Session":
        if self.path is None:
            return self
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            loaded = {}
        # A corrupt file raises, and the hook then fails closed.
        self.data = {**self._blank(), **loaded}
        return self

    def forget(self) -> None:
        self.data = self._blank()
        if self.path is not None:
            with contextlib.suppress(OSError):
                self.path.unlink()
            with contextlib.suppress(OSError):
                self.path.with_suffix(".lock").unlink()

    @property
    def trusted(self) -> list:
        return list(self.data["trusted"])

    def trust(self, cls: str) -> None:
        if cls not in self.data["trusted"]:
            self.data["trusted"].append(cls)

    def untrust(self, cls: Optional[str] = None) -> None:
        self.data["trusted"] = [] if cls is None else [c for c in self.data["trusted"] if c != cls]

    def approve_once(self, key: str) -> None:
        self.data["once"][key] = time.time() + ONCE_SECONDS

    def take_once(self, key: str) -> bool:
        expires = self.data["once"].pop(key, 0)
        return expires > time.time()

    def set_pending(self, key: str, summary: str, cls: str) -> None:
        self.data["pending"] = {"key": key, "summary": summary, "cls": cls, "at": time.time()}

    def take_pending(self) -> Optional[dict]:
        pending, self.data["pending"] = self.data["pending"], None
        return pending

    def loop_hit(self, key: str, name: str, args: object) -> bool:
        """True when this exact call has repeated with nothing changed in between."""
        recent = self.data["recent"]
        if isinstance(args, dict) and POLLING.search(str(args.get("command") or "")):
            return False  # deliberately waiting on something is not a stuck loop
        if changes_something(name, args) and recent and recent[-1] != key:
            recent.clear()
        recent.append(key)
        del recent[:-LOOP_WINDOW]
        if recent.count(key) < LOOP_THRESHOLD:
            return False
        recent.clear()  # so a yes isn't followed by the same question on the very next call
        return True


def audit(event: str, session: str, tool: str, cls: str, summary: str, detail: str = "", via: str = "") -> None:
    """Add to the log. Everything that ran on a standing yes ends up here."""
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "session": (session or "")[:8], "tool": tool, "class": cls, "summary": redact(summary), "detail": redact(detail)[:300]}
    if via:
        entry["via"] = via
    path = home() / "audit.jsonl"
    _private(path.parent)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def read_audit(limit: int = 20) -> list:
    try:
        lines = (home() / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines[-limit:] if line.strip()]


def clean_old_sessions() -> None:
    cutoff = time.time() - STALE_SESSION_SECONDS
    with contextlib.suppress(OSError):
        for item in (home() / "sessions").iterdir():
            with contextlib.suppress(OSError):
                if item.stat().st_mtime < cutoff:
                    item.unlink()
