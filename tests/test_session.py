import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from surya_gates.session import Session, audit, changes_something, digest, read_audit, redact


class Secrets(unittest.TestCase):
    def test_redacts(self):
        for text in ("sk-abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwxyz1234", "AKIAABCDEFGHIJKLMNOP", "xoxb-1234567890-abc", "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ1234567", "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghij"):
            with self.subTest(text=text):
                self.assertNotIn(text, redact(f"curl -H {text} x"))

    def test_keeps_the_name_hides_the_value(self):
        self.assertEqual(redact("PASSWORD=hunter2 run"), "PASSWORD=[hidden] run")
        self.assertEqual(redact('{"api_key": "abc"}'), '{"api_key": "[hidden]"}')
        self.assertEqual(redact("x --token abc y"), "x --token [hidden] y")
        self.assertNotIn("abc", redact("Bearer abc"))

    def test_leaves_ordinary_text(self):
        self.assertEqual(redact("git push origin main"), "git push origin main")


class SessionState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"SURYA_GATES_HOME": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saved_privately(self):
        session = Session.named("a/b:c")
        with session.edit():
            session.trust("deploy")
        self.assertEqual(Session.named("a/b:c").load().trusted, ["deploy"])
        self.assertEqual(stat.S_IMODE(session.path.stat().st_mode), 0o600)

    def test_corrupt_state_raises_instead_of_resetting(self):
        session = Session.named("x")
        session.path.parent.mkdir(parents=True, exist_ok=True)
        session.path.write_text("{broken")
        with self.assertRaises(ValueError):
            session.load()

    def test_forget(self):
        session = Session.named("x")
        with session.edit():
            session.trust("deploy")
        session.forget()
        self.assertEqual(Session.named("x").load().trusted, [])

    def test_once_is_once(self):
        session = Session()
        session.approve_once("k")
        self.assertTrue(session.take_once("k"))
        self.assertFalse(session.take_once("k"))

    def test_once_expires(self):
        session = Session()
        with mock.patch("surya_gates.session.time.time", return_value=1000):
            session.approve_once("k")
        with mock.patch("surya_gates.session.time.time", return_value=999999):
            self.assertFalse(session.take_once("k"))

    def test_audit_hides_secrets_and_is_private(self):
        audit("asked", "session-id-long", "Bash", "deploy", "Deploy", "TOKEN=abc123 fly deploy")
        row = read_audit()[0]
        self.assertEqual(row["detail"], "TOKEN=[hidden] fly deploy")
        self.assertEqual(row["session"], "session-")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir.name, "audit.jsonl")).st_mode), 0o600)


class Loops(unittest.TestCase):
    def run_calls(self, calls):
        session, hits = Session(), []
        for name, args in calls:
            hits.append(session.loop_hit(digest(name, args), name, args))
        return hits

    def test_three_the_same(self):
        read = ("Read", {"file_path": "a"})
        self.assertEqual(self.run_calls([read] * 3), [False, False, True])

    def test_clears_after_a_hit(self):
        read = ("Read", {"file_path": "a"})
        self.assertEqual(self.run_calls([read] * 4), [False, False, True, False])

    def test_test_edit_test_is_not_a_loop(self):
        test = ("Bash", {"command": "npm test"})
        edit = lambda n: ("Edit", {"file_path": "a", "new_string": str(n)})  # noqa: E731
        self.assertFalse(any(self.run_calls([test, edit(1), test, edit(2), test, edit(3), test, edit(4), test])))

    def test_running_the_same_command_again_and_again_is(self):
        test = ("Bash", {"command": "npm test"})
        self.assertTrue(self.run_calls([test] * 3)[2])

    def test_waiting_is_not_a_loop(self):
        wait = ("Bash", {"command": "sleep 5; curl localhost/health"})
        self.assertFalse(any(self.run_calls([wait] * 8)))

    def test_outside_the_window(self):
        a = ("Read", {"file_path": "a"})
        fillers = [("Read", {"file_path": str(i)}) for i in range(10)]
        self.assertFalse(any(self.run_calls([a, a, *fillers, a])))

    def test_changes_something(self):
        self.assertTrue(changes_something("Edit", {}))
        self.assertFalse(changes_something("Bash", {"command": "git status"}))
        self.assertTrue(changes_something("Bash", {"command": "make"}))
        self.assertFalse(changes_something("Read", {}))
