"""What counts as consequential. Pure functions, no state and no provider in here.

A verdict is one of three actions:
  pass    nothing consequential, carry on
  stop    a human has to say yes
  refuse  no human yes would make sense, so nobody is asked
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

# name: (label, can it be trusted for a session)
CLASSES = {
    "deploy": ("Deploys and infrastructure changes", True),
    "publish": ("Pushing code and publishing packages", True),
    "delete": ("Deleting things", True),
    "install": ("Installing new packages", True),
    "outside": ("Writing outside the project", True),
    "external": ("Changing things through external tools", True),
    "send": ("Sending messages", False),
    "money": ("Spending money", False),
    "kill": ("Killing processes", False),
    "credentials": ("Touching credentials", False),
    "permissions": ("Changing the agent's own permissions", False),
    "loop": ("Breaking out of a retry loop", False),
}


@dataclass(frozen=True)
class Verdict:
    action: str
    cls: str = ""
    summary: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return CLASSES.get(self.cls, ("", False))[0]

    @property
    def trustable(self) -> bool:
        return CLASSES.get(self.cls, ("", False))[1]


PASS = Verdict("pass")
SHELL_TOOLS = {"bash", "shell", "local_shell", "shell_command", "exec_command", "unified_exec", "run_command", "run_shell", "terminal", "powershell"}
WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "str_replace_editor", "write_file", "edit_file"}
PATH_KEYS = ("file_path", "notebook_path", "path", "filename")
_F = re.IGNORECASE


def _rx(*parts: str) -> "re.Pattern[str]":
    return re.compile("|".join(parts), _F)


GIT = r"\bgit(?:\s+-[cC]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+"
# Where a command name can begin, so `grep kill x.log` doesn't look like a kill.
_WORD_START = r"(?:^\s*|[;&|(`'\"\n]\s*|\b(?:sudo|xargs|then|do|else|time|exec|nohup|command|env)\s+(?:-\S+\s+)*|\b\w+=\S*\s+)(?:[\w.~-]*/)*"

# Commands that run their quoted argument, so the quoted text counts.
RUNS_QUOTED = _rx(r"\b(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*-\w*c\b", r"\beval\b", r"\bssh\b", r"\b(?:python3?|node|ruby|perl|php)\s+-\w*[ce]\b", r"\b(?:ba|z|da)?sh\s*<<", r"\b(?:powershell|pwsh)\b", r"\bcmd(?:\.exe)?\s+/c\b")

SELF = _rx(r"\.surya-gates\b", r"[/\\]plugins[/\\]\S*surya-gates", r"\bsurya[-_]gates[\\\"']*\s+(?:text|hook|install|uninstall)\b", r"\b(?:claude|codex)\b.*\bgates\s+(?:trust|untrust|yes|no|text)\b", r"\bplugin\s+(?:disable|uninstall|remove)\s+surya-gates\b", r"\bdisableAllHooks\b", r"\bSURYA_GATES_HOME\b")

CREDENTIAL_PATH = (
    r"(?:^|[\s/\\\"'=:])\.env(?:\.(?!example\b|sample\b|template\b|dist\b)[\w-]+)?(?=$|[\s\"';|&>)])",
    r"[/\\]\.ssh[/\\]", r"\bid_(?:rsa|ed25519|ecdsa|dsa)\b", r"[/\\]\.aws[/\\]credentials\b", r"[/\\]\.kube[/\\]config\b",
    r"[/\\]\.codex[/\\]auth\.json\b", r"\.credentials\.json\b", r"(?:^|[/\\\s\"'])\.(?:netrc|pgpass|npmrc|pypirc)\b",
    r"[/\\]\.docker[/\\]config\.json\b", r"[/\\]\.config[/\\]gh[/\\]hosts\.yml\b", r"[\w.-]+\.(?:pem|p12|pfx|keystore)\b",
    r"\bservice[-_]account[\w.-]*\.json\b", r"\bsecrets?\.(?:ya?ml|json|toml)\b",
)
CREDENTIAL_FILE = _rx(*CREDENTIAL_PATH)
CREDENTIAL_CMD = _rx(
    *CREDENTIAL_PATH,
    r"\bsecurity\s+(?:find|dump)-[\w-]*(?:password|keychain)", r"\bgh\s+auth\s+token\b", r"\baws\s+(?:configure\s+get|secretsmanager\s+get-secret-value|sts\s+get-session-token)\b",
    r"\baws\s+ssm\s+get-parameter.*--with-decryption", _WORD_START + r"printenv\b", _WORD_START + r"env\s*(?:$|[|>;&])", r"\bvault\s+(?:read|kv\s+get)\b", r"\bop\s+(?:read|item\s+get)\b",
    r"\bdoppler\s+secrets\b", r"\bheroku\s+config\b", r"\bvercel\s+env\s+pull\b", r"\bkubectl\s+get\s+secrets?\b",
)
KILL = _rx(_WORD_START + r"(?:sudo\s+)?(?:kill|pkill|killall|taskkill)\b", r"\bstop-process\b", r"\bsystemctl\s+(?:stop|restart|kill|disable)\b", r"\bservice\s+\S+\s+(?:stop|restart)\b", r"\blaunchctl\s+(?:unload|bootout|kill|remove)\b", r"\bdocker\s+(?:kill|stop)\b", _WORD_START + r"(?:sudo\s+)?(?:shutdown|reboot|halt)\b")
SEND_URL = _rx(r"\bhooks\.slack\.com\b", r"\bslack\.com/api/(?:chat\.|files\.upload)", r"\bdiscord(?:app)?\.com/api/(?:webhooks|channels)", r"\bapi\.(?:sendgrid\.com|mailgun\.net|postmarkapp\.com|resend\.com|twilio\.com|brevo\.com)\b", r"\bapi\.telegram\.org/bot\S*/send", r"\bgraph\.microsoft\.com\S*sendMail", r"\bgmail\.googleapis\.com\S*send")
SEND_CMD = _rx(_WORD_START + r"(?:mail|mailx|sendmail|mutt|msmtp)\b", r"\btwilio\s+api\S*\s+.*messages", r"\bgh\s+(?:issue|pr)\s+(?:comment|create|review)\b", r"\baws\s+(?:ses|sesv2|sns)\s+(?:send|publish)", r"\bslack\s+chat\b", _WORD_START + r"imsg\s+send\b")
SEND_RAW = _rx(r"\bosascript\b.*\b(?:Messages|Mail)\b.*\bsend\b", r"\bsmtplib\b", r"\bsend_email\b")
MONEY = _rx(r"\bstripe\s+(?:charges|payment_intents|transfers|payouts|refunds|invoices|subscriptions)\s+(?:create|confirm|capture|pay)\b", r"\bapi\.stripe\.com\S*(?:.*\s-(?:X\s*POST|d)\b|.*--data)", r"(?:\s-(?:X\s*POST|d)\b|--data).*\bapi\.stripe\.com\b", r"\bvercel\s+domains\s+buy\b", r"\baws\s+ec2\s+purchase-", r"\baws\s+route53domains\s+register-domain\b", r"\bgcloud\s+domains\s+registrations\s+register\b")
SQL_CLIENT = _rx(r"\b(?:psql|mysql|mariadb|sqlite3|mongosh|cqlsh|clickhouse(?:-client)?|snowsql|duckdb)\b", r"\bbq\s+query\b")
SQL_DANGER = _rx(r"\bdrop\s+(?:table|database|schema|index|view)\b", r"\btruncate\s+(?:table\s+)?\w", r"\bdelete\s+from\b", r"\balter\s+table\s+\S+\s+drop\b")
RM = re.compile(_WORD_START + r"(?:sudo\s+)?rm\s+(?:[^;&|]*\s)?-\w*[rRf]", re.IGNORECASE)
DELETE = _rx(
    GIT + r"clean\s+(?:-\w+\s+)*-\w*f", GIT + r"reset\s+.*--hard\b", GIT + r"checkout\s+(?:--\s+)?\.(?:\s|$)", GIT + r"restore\s+(?:--[\w-]+\s+)*\.(?:\s|$)", GIT + r"branch\s+.*-D\b", GIT + r"stash\s+(?:drop|clear)\b", GIT + r"filter-(?:branch|repo)\b",
    GIT + r"push\s+.*(?:--force\b|\s-f\b|--delete\b|\s:\S)", r"\bfind\b.*\s-(?:delete|exec\s+rm)\b", _WORD_START + r"(?:shred|mkfs(?:\.\w+)?|dropdb)\b", r"\bdd\s+.*\bof=/dev/", r"\bremove-item\b.*-recurse", r"\brmdir\s+/s\b",
    r"\bredis-cli\b.*\bflush(?:all|db)\b", r"\baws\s+s3\s+(?:rm|rb)\b", r"\bgsutil\s+(?:-\w+\s+)*(?:rm|rb)\b", r"\baws\s+\S+\s+(?:delete|terminate|deregister)-", r"\bgcloud\s+.*\bdelete\b", r"\baz\s+.*\bdelete\b", r"\bkubectl\s+delete\b", r"\bhelm\s+(?:uninstall|delete)\b",
    r"\bterraform\s+destroy\b", r"\btofu\s+destroy\b", r"\bpulumi\s+destroy\b", r"\bcdk\s+destroy\b", r"\bvercel\s+(?:rm|remove)\b", r"\bfly(?:ctl)?\s+(?:apps\s+destroy|destroy|volumes\s+destroy)\b", r"\bheroku\s+apps:destroy\b", r"\bgh\s+(?:repo|release|issue)\s+delete\b", r"\bwrangler\s+delete\b",
    r"\bsupabase\s+db\s+reset\b", r"\bprisma\s+migrate\s+reset\b", r"\brails\s+db:(?:drop|reset)\b", r"\bdocker\s+(?:system|volume|image|container|network)\s+prune\b", r"\bdocker\s+(?:rm|rmi)\b", r"\bdocker[\s-]compose\s+down\s+.*-v\b",
)
DEPLOY = _rx(
    r"\bvercel\b.*\s--prod\b", r"\bvercel\s+(?:promote|rollback)\b", r"\bnetlify\s+deploy\b.*--prod\b", r"\bfly(?:ctl)?\s+deploy\b", GIT + r"push\s+heroku\b", r"\bheroku\s+(?:container:release|releases:rollback)\b",
    r"\bkubectl\s+(?:apply|create|replace|patch|rollout\s+(?:restart|undo)|scale|set|drain)\b", r"\bhelm\s+(?:install|upgrade|rollback)\b", r"\b(?:terraform|tofu)\s+(?:apply|import|state\s+(?:rm|mv))\b", r"\bpulumi\s+up\b", r"\bcdk\s+deploy\b", r"\bsam\s+deploy\b", r"\b(?:serverless|sls)\s+deploy\b",
    r"\bwrangler\s+(?:deploy|publish|secret\s+put|d1\s+migrations\s+apply)\b", r"\bfirebase\s+deploy\b", r"\bgcloud\s+(?:app|run|functions)\s+.*\bdeploy\b", r"\baws\s+cloudformation\s+(?:deploy|create-stack|update-stack)\b", r"\baws\s+ecs\s+update-service\b", r"\baws\s+lambda\s+update-function-code\b", r"\baz\s+(?:webapp|deployment)\b.*\b(?:deploy|create)\b",
    r"\bdocker\s+push\b", r"\beas\s+(?:submit|update)\b", r"\bfastlane\s+(?:deliver|pilot|release)\b", r"\bansible-playbook\b", r"\bprisma\s+migrate\s+deploy\b", r"\bsupabase\s+db\s+push\b", r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?deploy\b", r"\bmake\s+(?:deploy|release)\b", _WORD_START + r"deploy(?:\.sh|\.py)?(?:\s|$)",
)
PUBLISH = _rx(GIT + r"push\b", r"\b(?:npm|pnpm)\s+publish\b", r"\byarn\s+(?:npm\s+)?publish\b", r"\btwine\s+upload\b", r"\b(?:poetry|uv)\s+publish\b", r"\bcargo\s+publish\b", r"\bgem\s+push\b", r"\bgh\s+release\s+create\b", r"\bgh\s+pr\s+merge\b", r"\bgh\s+repo\s+(?:create|edit|rename)\b", r"\bgh\s+workflow\s+run\b", r"\bgh\s+api\s+.*(?:-X\s*|--method\s+)(?:POST|PUT|PATCH|DELETE)\b")
INSTALL_CMD = _rx(r"\b(?:npm|pnpm|bun)\s+(?:i|install|add)\s+(?:--?[\w-]+\s+)*[^-\s]", r"\byarn\s+add\s", r"\bpip3?\s+install\s+(?!-r\b|-e\b|--requirement\b|--editable\b|\.(?:\s|$)|--upgrade\s+pip\b)", r"\buv\s+(?:pip\s+install\s+(?!-r\b|-e\b)|add\s)", r"\b(?:brew|apt|apt-get|dnf|yum|pacman|winget|choco|scoop)\s+(?:install|add|upgrade|-S)\b", r"\b(?:cargo|go|gem)\s+install\b", r"\bdotnet\s+tool\s+install\b", r"\bnpx\s+(?:-y|--yes)\b")
INSTALL_RAW = _rx(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", r"\b(?:iwr|invoke-webrequest)\b[^|]*\|\s*(?:iex|invoke-expression)\b")
PERMISSIONS = _rx(r"[/\\]?\.claude[/\\]settings(?:\.local)?\.json\b", r"[/\\]?\.codex[/\\](?:config\.toml|hooks\.json)\b")
PERMISSION_READ = _rx(r"^\s*(?:cat|head|tail|less|more|jq|grep|rg|wc|ls|stat|diff)\b[^>|;&]*$")
HTTP_WRITE = _rx(r"\bcurl\b.*(?:\s-X\s*(?:POST|PUT|PATCH|DELETE)\b|\s--request\s+(?:POST|PUT|PATCH|DELETE)\b|\s-d\s|\s--data\b|\s-F\s|\s--form\b)", r"\bwget\b.*--post-")
LOCAL_URL = _rx(r"(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?(?:[/\s\"']|$)")
REBUILDABLE = re.compile(r"^(?:\./)?(?:[\w.-]+/)*(?:node_modules|dist|build|\.next|\.nuxt|\.turbo|\.cache|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|coverage|\.venv|venv)/?$|^(?:/private)?/tmp/|^\$TMPDIR/")
SHELL_VERBS = {"run", "exec", "execute", "shell", "bash", "terminal"}
READ_VERBS = {"get", "list", "search", "read", "fetch", "find", "query", "describe", "view", "show", "status", "check", "count", "lookup", "browse", "recall", "explain", "preview", "ping", "health"}
SECRET_WORDS = {"secret", "secrets", "password", "passwords", "credential", "credentials", "token", "tokens", "apikey"}
SEND_VERBS = {"send", "reply", "forward", "email", "mail", "sms", "tweet", "dm", "notify", "invite", "broadcast"}
MONEY_WORDS = {"pay", "payment", "payments", "payout", "payouts", "charge", "refund", "purchase", "buy", "checkout"}
DELETE_VERBS = {"delete", "remove", "drop", "destroy", "purge", "wipe", "truncate", "erase", "revoke"}
DEPLOY_VERBS = {"deploy", "rollout", "promote", "rollback"}
PUBLISH_VERBS = {"publish", "release", "merge", "push"}
EXTERNAL_VERBS = {"create", "update", "write", "edit", "put", "set", "add", "insert", "upload", "patch", "modify", "rename", "move", "comment", "close", "assign", "approve", "cancel", "start", "stop", "restart"}


def strip_quoted(command: str) -> str:
    """Blank out heredoc bodies and quoted strings, so a commit message can't trip a rule."""
    command = re.sub(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\b", " ", command, flags=re.S)
    out, quote, i = [], "", 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = ""
                out.append(c)
            i += 1
            continue
        if c == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
        out.append(c)
        i += 1
    return "".join(out)


def _only_rebuildable(text: str) -> bool:
    """True when every `rm` here targets something a build recreates, or a temp path."""
    for segment in re.split(r"(?:&&|\|\||[;|\n])", text):
        if not RM.search(segment):
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        at = next((i for i, w in enumerate(words) if w.rsplit("/", 1)[-1] == "rm"), None)
        if at is None:
            return False
        targets = [w for w in words[at + 1:] if not w.startswith("-")]
        if not targets or not all(REBUILDABLE.search(t) and ".." not in t for t in targets):
            return False
    return True


def classify_shell(command: str) -> Verdict:
    raw = command.strip()
    if not raw:
        return PASS
    runs_quoted = bool(RUNS_QUOTED.search(raw))
    text = raw if runs_quoted else strip_quoted(raw)

    def stop(cls: str, summary: str) -> Verdict:
        return Verdict("stop", cls, summary, raw)

    if SELF.search(raw):
        return Verdict("refuse", "permissions", "Change Surya Gates' own state", raw)
    if CREDENTIAL_CMD.search(raw):
        return stop("credentials", "Read a credential")
    if KILL.search(text):
        return stop("kill", "Kill a process")
    if SEND_URL.search(raw) or SEND_CMD.search(text) or SEND_RAW.search(raw):
        return stop("send", "Send a message")
    if MONEY.search(raw):
        return stop("money", "Spend money")
    if (RM.search(text) and (runs_quoted or not _only_rebuildable(text))) or DELETE.search(text) or (SQL_CLIENT.search(raw) and SQL_DANGER.search(raw)):
        return stop("delete", "Delete something")
    if DEPLOY.search(text):
        return stop("deploy", "Deploy or change infrastructure")
    if PUBLISH.search(text) and "--dry-run" not in text:
        return stop("publish", "Push or publish code")
    if INSTALL_CMD.search(text) or INSTALL_RAW.search(raw):
        return stop("install", "Install a new package")
    if PERMISSIONS.search(raw) and not PERMISSION_READ.search(raw):
        return stop("permissions", "Change the agent's permissions")
    if HTTP_WRITE.search(raw) and not LOCAL_URL.search(raw):
        return stop("external", "Send data to an external service")
    return PASS


def name_words(name: str) -> list:
    """`mcp__slack__chat_postMessage` becomes ['chat', 'post', 'message']."""
    last = name.split("__")[-1]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", last)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def classify_name(name: str, args: Mapping) -> Verdict:
    """Classify a tool by what its name says it does."""
    words = name_words(name)
    if not words:
        return PASS
    first, have = words[0], set(words)
    detail = _short(args)

    def stop(cls: str, summary: str) -> Verdict:
        return Verdict("stop", cls, f"{summary}: {name}", detail)

    if have & SECRET_WORDS or {"api", "key"} <= have:
        return stop("credentials", "Use a credential")
    if first in READ_VERBS:
        return PASS
    if have & SEND_VERBS or {"post", "message"} <= have:
        return stop("send", "Send a message")
    if have & MONEY_WORDS or {"place", "order"} <= have or ("transfer" in have and have & {"funds", "money", "balance", "usd"}):
        return stop("money", "Spend money")
    if first in DELETE_VERBS:
        return stop("delete", "Delete something")
    if first in DEPLOY_VERBS:
        return stop("deploy", "Deploy")
    if first in PUBLISH_VERBS:
        return stop("publish", "Publish")
    if first in EXTERNAL_VERBS:
        return stop("external", "Change something")
    return PASS


def _short(args: Mapping) -> str:
    return ", ".join(f"{k}={v}" for k, v in list(args.items())[:6])[:400] if isinstance(args, Mapping) else str(args)[:400]


def _shell_text(args: Mapping) -> str:
    value = args.get("command") or args.get("cmd") or args.get("script") or ""
    return " ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_memory_path(path: str) -> bool:
    return bool(re.search(r"[/\\]\.claude[/\\](?:projects[/\\][^/\\]+[/\\]memory|plans|todos)[/\\]", path))


def classify_path(tool: str, path: str, writing: bool, cwd: Optional[str], writable: Iterable[str]) -> Verdict:
    if re.search(r"(?:^|[/\\])\.surya-gates(?:[/\\]|$)|[/\\]plugins[/\\].*surya-gates", path):
        return Verdict("refuse", "permissions", "Change Surya Gates' own state", path)
    if CREDENTIAL_FILE.search(path):
        return Verdict("stop", "credentials", f"{'Write' if writing else 'Open'} a file that holds a credential", path)
    if not writing:
        return PASS
    if PERMISSIONS.search(path):
        return Verdict("stop", "permissions", "Change the agent's permissions", path)
    if cwd and not _is_memory_path(path):
        roots = [Path(cwd).resolve()] + [Path(w).expanduser().resolve() for w in writable if w]
        target = Path(path).expanduser()
        target = (Path(cwd) / target if not target.is_absolute() else target).resolve()
        if not _inside(target, roots):
            return Verdict("stop", "outside", "Write outside the project", str(target))
    return PASS


PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.M)


def classify_patch(patch: str, cwd: Optional[str], writable: Iterable[str]) -> Verdict:
    """Codex's apply_patch names its files inside the patch."""
    worst = PASS
    for action, path in PATCH_FILE.findall(patch):
        verdict = classify_path("apply_patch", path.strip(), True, cwd, writable)
        if verdict.action == "refuse" or verdict.cls in ("credentials", "permissions"):
            return verdict
        if verdict.action == "stop":
            worst = verdict
        elif action == "Delete" and worst.action == "pass":
            worst = Verdict("stop", "delete", "Delete a file", path.strip())
    return worst


def classify(name: str, args: Optional[Mapping] = None, cwd: Optional[str] = None, writable: Iterable[str] = (), by_name: Optional[bool] = None) -> Verdict:
    """Decide whether a tool call needs a human.

    `by_name` turns on classifying by the words in the tool's name. It defaults
    to on for MCP tools and is set on for tools an application defines.
    """
    args = args if isinstance(args, Mapping) else {}
    low = name.lower()
    if low in SHELL_TOOLS:
        return classify_shell(_shell_text(args))
    if low == "apply_patch":
        patch = str(args.get("command") or args.get("patch") or args.get("input") or "")
        return classify_patch(patch, cwd, writable)
    path = next((str(args[k]) for k in PATH_KEYS if args.get(k)), "")
    if low in WRITE_TOOLS:
        return classify_path(name, path, True, cwd, writable) if path else PASS
    if path:
        verdict = classify_path(name, path, False, cwd, writable)
        if verdict.action != "pass":
            return verdict
    if by_name if by_name is not None else name.startswith("mcp__"):
        # A tool that runs commands is a shell, whatever it is called.
        if _shell_text(args) and name_words(name)[:1] and name_words(name)[0] in SHELL_VERBS:
            return classify_shell(_shell_text(args))
        return classify_name(name, args)
    return PASS
