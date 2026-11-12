import json
import os
import sys
import tempfile
import urllib.request
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from surya_gates import Gate
from surya_gates.channels import ChannelError, IMessage, Ntfy, Telegram, Twilio, _body_text, _handle, active_codes, ask_human, build_channel, guarded, new_topic, parse_reply, question, text_approver, text_enabled, wait_seconds
from surya_gates.session import save_config
from tests.fakes import CHAT, HUMAN, ME, FakeMessages, FakeNtfy, FakeTelegram, FakeTwilio, typedstream


class Parsing(unittest.TestCase):
    def test_answers(self):
        cases = {"YES 4821": ("yes", "4821", ""), "yes 4821": ("yes", "4821", ""), "Y 4821": ("yes", "4821", ""), "ok: 4821.": ("yes", "4821", ""), "4821 yes": ("yes", "4821", ""), "no 4821": ("no", "4821", ""), "DENY 4821": ("no", "4821", ""), "4821 no": ("no", "4821", ""), "TRUST 4821": ("trust", "4821", ""), "4821 use staging first": ("reroute", "4821", "use staging first"), "4821: deploy\nonly to eu": ("reroute", "4821", "deploy\nonly to eu")}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_reply(text), expected)

    def test_not_answers(self):
        for text in ("YES", "4821", "hello", "yes please 4821", "YES 48213", "482 yes", ""):
            with self.subTest(text=text):
                self.assertIsNone(parse_reply(text))

    def test_question(self):
        text = question("Deploy", "TOKEN=abc fly deploy", "shop", "4821", True)
        self.assertIn("TOKEN=[hidden]", text)
        self.assertIn("Project: shop", text)
        self.assertIn("TRUST 4821", text)
        self.assertNotIn("TRUST", question("Send", "x", "", "4821", False))
        self.assertNotIn("secret-plan", question("Deploy", "secret-plan", "", "4821", True, show_detail=False))
        self.assertLess(len(question("D", "x" * 5000, "", "4821", False)), 400)
        self.assertNotRegex(question("D", "x", "", "4821", True), r"\bSTOP\b")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"SURYA_GATES_HOME": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)


class Conversation(Base):
    server_class = FakeTelegram

    def setUp(self):
        super().setUp()
        self.server = self.server_class()
        self.addCleanup(self.server.close)
        self.channel = self.make_channel()

    def make_channel(self):
        return Telegram(FakeTelegram.TOKEN, CHAT, self.server.url)

    def ask(self, wait=5, trustable=True):
        return ask_human(self.channel, "Deploy", "fly deploy", "shop", trustable, wait, poll_seconds=0.02)

    def answer_with(self, template):
        self.server.on_send = lambda text: "YES" in text and self.server.reply(template.format(code=self.server.code()))

    def test_yes(self):
        self.answer_with("YES {code}")
        self.assertEqual(self.ask(), {"decision": "yes", "instruction": ""})

    def test_no(self):
        self.answer_with("no {code}")
        self.assertEqual(self.ask()["decision"], "no")

    def test_reroute(self):
        self.answer_with("{code} use staging")
        self.assertEqual(self.ask(), {"decision": "reroute", "instruction": "use staging"})

    def test_trust(self):
        self.answer_with("TRUST {code}")
        self.assertEqual(self.ask()["decision"], "trust")

    def test_no_answer(self):
        self.assertIsNone(self.ask(wait=0.2))
        self.assertIn("No answer", self.server.sent[-1])

    def test_old_codes_do_nothing(self):
        def replies(text):
            if "YES" in text:
                self.server.reply("YES 0000")
                self.server.reply(f"NO {self.server.code()}")

        self.server.on_send = replies
        self.assertEqual(self.ask()["decision"], "no")

    def test_wrong_codes_end_it(self):
        def replies(text):
            if "YES" in text:
                for _ in range(5):
                    self.server.reply("YES 0000")

        self.server.on_send = replies
        self.assertEqual(self.ask()["decision"], "no")
        self.assertIn("wrong codes", self.server.sent[-1])

    def test_trust_refused_for_an_untrustable_kind(self):
        def replies(text):
            if text.startswith("Surya Gates"):
                code = self.server.code()
                self.server.reply(f"TRUST {code}")
                time.sleep(0.1)
                self.server.reply(f"YES {code}")

        self.server.on_send = replies
        self.assertEqual(self.ask(trustable=False)["decision"], "yes")
        self.assertTrue(any("can't be trusted" in s for s in self.server.sent))

    def test_several_at_once_each_get_their_own_answer(self):
        results = {}
        threads = [threading.Thread(target=lambda i=i: results.__setitem__(i, self.ask(wait=8))) for i in range(3)]
        for thread in threads:
            thread.start()
        self.server.wait_for_send(3)
        for index, word in enumerate(("YES", "NO", "TRUST")):
            self.server.reply(f"{word} {self.server.code(index)}")
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(r["decision"] for r in results.values()), ["no", "trust", "yes"])

    def test_code_is_released(self):
        self.answer_with("YES {code}")
        self.ask()
        self.assertEqual(active_codes(), set())


class TwilioConversation(Conversation):
    server_class = FakeTwilio

    def make_channel(self):
        return Twilio(FakeTwilio.SID, FakeTwilio.TOKEN, ME, HUMAN, self.server.url)

    def test_other_numbers_are_ignored(self):
        def replies(text):
            if "YES" in text:
                self.server.reply(f"YES {self.server.code()}", sender="+15559999999")

        self.server.on_send = replies
        self.assertIsNone(self.ask(wait=0.3))

    def test_bad_login_reported(self):
        with self.assertRaises(ChannelError):
            Twilio("ACwrong", "x", ME, HUMAN, self.server.url).send("x")


class TelegramOnly(Conversation):
    def test_other_people_are_ignored(self):
        def replies(text):
            if "YES" in text:
                code = self.server.code()
                self.server.reply(f"YES {code}", sender=999, chat=999)
                self.server.reply(f"YES {code}", sender=999, chat=CHAT, chat_type="group")
                self.server.reply(f"YES {code}", sender=CHAT, chat=-100, chat_type="group")

        self.server.on_send = replies
        self.assertIsNone(self.ask(wait=0.3))

    def test_confirmed_only_after_saving(self):
        self.answer_with("YES {code}")
        self.ask()
        self.assertTrue(Path(self.dir.name, "telegram-offset").exists())
        self.assertIn("YES", Path(self.dir.name, "inbox.jsonl").read_text())

    def test_bad_token_reported(self):
        with self.assertRaises(ChannelError):
            Telegram("wrong", CHAT, self.server.url).send("x")


class NtfyTopic(unittest.TestCase):
    def test_short_and_typeable(self):
        topics = {new_topic() for _ in range(200)}
        self.assertEqual(len(topics), 200)
        for topic in topics:
            self.assertRegex(topic, r"^[a-z0-9]{10}$")
            Ntfy(topic)  # the channel accepts it
            self.assertEqual(guarded({"text": {"channel": "ntfy", "topic": topic}}), [topic])  # long enough for the guard


class NtfyConversation(Conversation):
    server_class = FakeNtfy

    def make_channel(self):
        return Ntfy(FakeNtfy.TOPIC, self.server.url)

    def test_buttons(self):
        self.answer_with("YES {code}")
        self.ask()
        question = self.server.published[0]
        labels = [a["label"] for a in question["actions"]]
        self.assertEqual(labels, ["Yes", "No", "Trust"])
        for action in question["actions"]:
            self.assertEqual(action["url"], f"{self.server.url}/{FakeNtfy.TOPIC}-reply")
            self.assertTrue(action["clear"])
        self.assertIn("Tap Yes or No", question["message"])
        self.assertNotIn("Reply YES", question["message"])

    def test_no_trust_button_for_an_untrustable_kind(self):
        self.answer_with("YES {code}")
        self.ask(trustable=False)
        self.assertEqual([a["label"] for a in self.server.published[0]["actions"]], ["Yes", "No"])

    def test_a_tap_is_the_answer(self):
        """Press the button the way the phone does: the action's own URL, method and body."""
        def tap(text):
            if "Surya Gates:" in text:
                action = self.server.published[-1]["actions"][1]
                urllib.request.urlopen(urllib.request.Request(action["url"], action["body"].encode(), method=action["method"])).read()

        self.server.on_send = tap
        self.assertEqual(self.ask()["decision"], "no")

    def test_answers_go_to_their_own_topic(self):
        self.answer_with("YES {code}")
        self.ask()
        self.assertTrue(all(p["topic"] == FakeNtfy.TOPIC for p in self.server.published))
        self.assertTrue(all(r["topic"] == FakeNtfy.TOPIC + "-reply" for r in self.server.replies))

    def test_token(self):
        server = FakeNtfy(token="tk_secret")
        self.addCleanup(server.close)
        with self.assertRaises(ChannelError):
            Ntfy(FakeNtfy.TOPIC, server.url).send("x")
        channel = Ntfy(FakeNtfy.TOPIC, server.url, "tk_secret")
        channel.send_question("Surya Gates: x", "1234", True)
        self.assertEqual(server.published[0]["actions"][0]["headers"], {"Authorization": "Bearer tk_secret"})

    def test_bad_topics(self):
        for topic in ("", "has space", "a/b", "x" * 59):
            with self.subTest(topic=topic), self.assertRaises(ChannelError):
                Ntfy(topic)


class IMessageConversation(Conversation):
    server_class = FakeMessages

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.dict(os.environ, {"PATH": self.server.bin + os.pathsep + os.environ["PATH"]}), mock.patch("surya_gates.channels.sys.platform", "darwin")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_channel(self):
        return IMessage(HUMAN, self.server.db)

    def test_plain_text_rows_too(self):
        self.server.on_send = lambda text: "YES" in text and self.server.reply(f"YES {self.server.code()}", plain=True)
        self.assertEqual(self.ask()["decision"], "yes")

    def test_long_reply(self):
        self.answer_with("{code} " + "deploy to staging first and wait for the checks " * 4)
        answer = self.ask()
        self.assertEqual(answer["decision"], "reroute")
        self.assertGreater(len(answer["instruction"]), 128)

    def test_other_people_are_ignored(self):
        def replies(text):
            if "YES" in text:
                self.server.reply(f"YES {self.server.code()}", sender="+15559999999")

        self.server.on_send = replies
        self.assertIsNone(self.ask(wait=0.3))

    def test_its_own_texts_are_not_answers(self):
        self.assertIsNone(self.ask(wait=0.3))
        self.assertGreaterEqual(len(self.server.sent), 2)

    def test_send_refused(self):
        with self.assertRaises(ChannelError):
            IMessage("+15559999999", self.server.db).send("x")

    def test_unreadable_database(self):
        with self.assertRaises(ChannelError) as raised:
            IMessage(HUMAN, "/nonexistent/chat.db").fetch(0)
        self.assertIn("Full Disk Access", str(raised.exception))

    def test_mac_only(self):
        with mock.patch("surya_gates.channels.sys.platform", "linux"), self.assertRaises(ChannelError):
            IMessage(HUMAN, self.server.db).send("x")


class MessagesFormat(unittest.TestCase):
    def test_body_text(self):
        for text in ("YES 4821", "x" * 127, "y" * 128, "z" * 3000, "4821 ünïcode ✓"):
            with self.subTest(size=len(text)):
                self.assertEqual(_body_text(typedstream(text)), text)
        self.assertEqual(_body_text(None), "")
        self.assertEqual(_body_text(b"no string here"), "")

    def test_handles(self):
        self.assertEqual(_handle("+1 (555) 000-0002"), "+15550000002")
        self.assertEqual(_handle(" Me@iCloud.com "), "me@icloud.com")
        with self.assertRaises(ChannelError):
            IMessage("")


class Unreachable(Base):
    def test_send_failure_raises_and_releases(self):
        channel = Telegram("t", 1, "http://127.0.0.1:9")
        with self.assertRaises(ChannelError):
            ask_human(channel, "D", "x", "", True, 1, poll_seconds=0.01)
        self.assertEqual(active_codes(), set())


class Settings(Base):
    def test_build(self):
        self.assertIsInstance(build_channel({"text": {"channel": "telegram", "token": "t", "chat_id": 1}}), Telegram)
        self.assertIsInstance(build_channel({"text": {"channel": "twilio", "sid": "a", "token": "b", "from": "c", "to": "d"}}), Twilio)
        self.assertIsInstance(build_channel({"text": {"channel": "ntfy", "topic": "gates-abcdefgh"}}), Ntfy)
        self.assertIsInstance(build_channel({"text": {"channel": "imessage", "to": "+15550000002"}}), IMessage)
        for bad in ({}, {"text": {"channel": "pigeon"}}, {"text": {"channel": "ntfy"}}, {"text": {"channel": "ntfy", "topic": "gates-x", "server": "ftp://x"}}, {"text": {"channel": "telegram"}}, {"text": {"channel": "telegram", "token": "t", "chat_id": 1, "base_url": "file:///etc/passwd"}}):
            with self.subTest(bad=bad), self.assertRaises(ChannelError):
                build_channel(bad)

    def test_off_by_default(self):
        self.assertFalse(text_enabled({}))
        self.assertFalse(text_enabled({"text": {"enabled": True}}))
        self.assertFalse(text_enabled({"text": {"channel": "telegram", "enabled": False}}))
        self.assertTrue(text_enabled({"text": {"channel": "telegram", "enabled": True}}))

    def test_wait_is_bounded(self):
        self.assertEqual(wait_seconds({}), 1800)
        self.assertEqual(wait_seconds({"text": {"wait_seconds": "x"}}), 1800)
        self.assertEqual(wait_seconds({"text": {"wait_seconds": 10**9}}), 82800)
        self.assertEqual(wait_seconds({"text": {"wait_seconds": 0}}), 5)

    def test_config_is_private(self):
        save_config({"text": {"token": "t"}})
        self.assertEqual(Path(self.dir.name, "config.json").stat().st_mode & 0o777, 0o600)

    def test_text_approver_in_an_app(self):
        server = FakeTelegram()
        self.addCleanup(server.close)
        save_config({"text": {"channel": "telegram", "token": FakeTelegram.TOKEN, "chat_id": CHAT, "base_url": server.url, "enabled": True}})
        server.on_send = lambda text: "YES" in text and server.reply(f"YES {server.code()}")
        self.assertTrue(Gate(approver=text_approver(wait=5, poll_seconds=0.02)).check("deploy_app").allowed)


class Guarded(unittest.TestCase):
    def test_words(self):
        self.assertEqual(guarded({}), [])
        self.assertEqual(guarded({"text": {"channel": "ntfy", "topic": "gates-abcdefgh", "token": ""}}), ["gates-abcdefgh"])
        self.assertIn("Library/Messages", guarded({"text": {"channel": "imessage", "to": "+15550000002"}}))
        self.assertIn("123:secret-token", guarded({"text": {"channel": "telegram", "token": "123:secret-token", "chat_id": 1}}))
        self.assertEqual(guarded({"text": {"channel": "ntfy", "topic": "short"}}), [])
