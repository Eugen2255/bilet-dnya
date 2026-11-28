import os
import unittest
from datetime import timedelta
from itertools import count
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.queue import claim_job, enqueue_delivery
from app.telegram.api import TelegramError
from app.telegram.jobs import handle_reply_job, handle_update_job
from app.telegram.onboarding import INVALID_LINK, NOT_MEMBER, token_hash
from app.telegram.webhook import Update, telegram_webhook


GROUP_CHAT = -1009000000050
ADMIN, STUDENT, OUTSIDER = 900000000050, 900000000051, 900000000052


class FakeBot:
    username = "bilet_test_bot"

    def __init__(self):
        self.members = {ADMIN: "administrator", STUDENT: "member"}
        self.messages = []
        self.send_error = None

    def get_member(self, chat_id, user_id):
        assert chat_id == GROUP_CHAT
        return {"status": self.members.get(user_id, "left")}

    def send(self, payload):
        if self.send_error:
            raise self.send_error
        self.messages.append(payload)
        return len(self.messages)


class WebhookTests(unittest.TestCase):
    def test_denies_bad_secret_and_reports_unavailable_database(self):
        with patch.dict(os.environ, TELEGRAM_WEBHOOK_SECRET="test-secret", DATABASE_URL="postgresql://test"):
            with patch("app.telegram.webhook.psycopg.connect") as connect:
                with self.assertRaises(HTTPException) as wrong:
                    telegram_webhook(Update(update_id=1), "wrong")
                self.assertEqual(wrong.exception.status_code, 403)
                connect.assert_not_called()
                connect.side_effect = psycopg.OperationalError()
                with self.assertRaises(HTTPException) as unavailable:
                    telegram_webhook(Update(update_id=1), "test-secret")
                self.assertEqual(unavailable.exception.status_code, 503)


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a seeded test PostgreSQL")
class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["BILET_TEST_DATABASE_URL"]
        self.bot = FakeBot()
        self.update_ids = []
        self.next_id = count(900000001000)
        self.db = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        self.addCleanup(self.clean_fixture)
        env = patch.dict(os.environ, DATABASE_URL=self.url, TELEGRAM_WEBHOOK_SECRET="test-secret")
        env.start()
        self.addCleanup(env.stop)

    def clean_fixture(self):
        keys = [key for uid in self.update_ids for key in (f"telegram_update:{uid}", f"telegram_reply:{uid}:%")]
        self.db.execute("DELETE FROM jobs WHERE key LIKE ANY (%s) OR key LIKE 'tg-test-reply%%'", (keys,))
        self.db.execute("DELETE FROM telegram_updates WHERE update_id = ANY (%s)", (self.update_ids,))
        self.db.execute("DELETE FROM study_groups WHERE telegram_chat_id = %s", (GROUP_CHAT,))
        self.db.execute("DELETE FROM users WHERE telegram_user_id = ANY (%s)", ([ADMIN, STUDENT, OUTSIDER],))
        self.db.execute("DELETE FROM telegram_send_limits WHERE chat_id = ANY (%s)", ([GROUP_CHAT, ADMIN, STUDENT, OUTSIDER],))
        self.db.execute("UPDATE telegram_send_limits SET next_send_at = clock_timestamp() WHERE chat_id = 0")

    def command(self, user, text, *, chat="private"):
        update_id = next(self.next_id)
        self.update_ids.append(update_id)
        update = Update(update_id=update_id, message={
            "chat": {"type": chat, "id": user if chat == "private" else GROUP_CHAT},
            "from": {"id": user, "is_bot": False},
            "text": text,
        })
        self.assertEqual(telegram_webhook(update, "test-secret"), {"ok": True})
        self.assertEqual(telegram_webhook(update, "test-secret"), {"ok": True})
        job = claim_job(self.db, ("telegram_update",), 60)
        self.assertEqual(handle_update_job(self.db, job, self.bot), "done")
        self.assertIsNone(claim_job(self.db, ("telegram_update",), 60))
        stored = self.db.execute("""
            SELECT state, payload FROM telegram_updates WHERE update_id = %s
        """, (update_id,)).fetchone()
        self.assertEqual(stored, {"state": "processed", "payload": None})
        self.bot.messages.clear()
        while job := claim_job(self.db, ("telegram_reply",), 60):
            self.assertEqual(handle_reply_job(self.db, job, self.bot), "done")
            self.assertEqual(self.db.execute("SELECT payload FROM jobs WHERE id = %s", (job.id,)).fetchone()["payload"],
                             {})
        return self.bot.messages.copy()

    def button_token(self, message):
        button = message["reply_markup"]["inline_keyboard"][0][0]
        return parse_qs(urlsplit(button["url"]).query)["start"][0]

    def membership(self, user):
        return self.db.execute("""
            SELECT role, state, dm_chat_id FROM memberships
            JOIN users ON users.id = user_id
            WHERE telegram_user_id = %s
        """, (user,)).fetchone()

    def test_bind_one_time_and_join_checks_telegram_membership(self):
        denied = self.command(STUDENT, "/bind", chat="supergroup")
        self.assertIn("администратор", denied[0]["text"])
        self.assertIsNone(self.membership(STUDENT))

        messages = self.command(ADMIN, "/bind@bilet_test_bot", chat="supergroup")
        self.assertEqual(len(messages), 1)
        bind = self.button_token(messages[0])
        self.assertEqual(self.membership(ADMIN)["state"], "pending")
        self.assertEqual(self.command(STUDENT, f"/start {bind}")[0]["text"], INVALID_LINK)

        messages = self.command(ADMIN, f"/start {bind}")
        self.assertEqual(len(messages), 2)
        group_message = next(message for message in messages if message["chat_id"] == GROUP_CHAT)
        join = self.button_token(group_message)
        self.assertEqual(group_message["reply_markup"]["inline_keyboard"][0][0]["text"], "Ответить")
        self.assertEqual(self.membership(ADMIN), {"role": "organizer", "state": "active", "dm_chat_id": ADMIN})
        self.assertEqual(self.command(ADMIN, f"/start {bind}")[0]["text"], INVALID_LINK)

        self.assertEqual(self.command(OUTSIDER, f"/start {join}")[0]["text"], NOT_MEMBER)
        self.assertIsNone(self.membership(OUTSIDER))
        self.assertEqual(self.command(STUDENT, f"/start {join}")[0]["text"], "Вы подключились к группе.")
        self.assertEqual(self.membership(STUDENT), {"role": "student", "state": "active", "dm_chat_id": STUDENT})
        self.db.execute("""
            UPDATE action_tokens SET expires_at = clock_timestamp() - interval '1 second' WHERE token_hash = %s
        """, (token_hash(join),))
        self.assertEqual(self.command(STUDENT, f"/start {join}")[0]["text"], INVALID_LINK)

    def queue_reply(self, label, chat_id):
        with self.db.cursor() as cursor:
            enqueue_delivery(cursor, f"tg-test-reply-{label}", "telegram_reply", {"chat_id": chat_id, "text": "Тест"})
        return claim_job(self.db, ("telegram_reply",), 60)

    def test_rate_limit_and_ambiguous_delivery(self):
        first = self.queue_reply("rate", ADMIN)
        self.bot.send_error = TelegramError(429, 30)
        self.assertEqual(handle_reply_job(self.db, first, self.bot), "queued")
        row = self.db.execute("""
            SELECT extract(epoch FROM run_at - clock_timestamp()) AS delay, result FROM jobs WHERE id = %s
        """, (first.id,)).fetchone()
        self.assertAlmostEqual(float(row["delay"]), 30, delta=2)
        self.assertIsNone(row["result"])
        another = self.queue_reply("other-chat", STUDENT)
        self.bot.send_error = None
        self.assertEqual(handle_reply_job(self.db, another, self.bot), "queued")
        self.assertEqual(self.bot.messages, [])
        self.assertEqual(self.db.execute("SELECT tries FROM jobs WHERE id = %s", (another.id,)).fetchone()["tries"], 0)
        self.db.execute("UPDATE telegram_send_limits SET next_send_at = clock_timestamp() WHERE chat_id = 0")
        self.db.execute("UPDATE jobs SET run_at = clock_timestamp() WHERE id = %s", (first.id,))
        second = claim_job(self.db, ("telegram_reply",), 60)
        self.bot.send_error = TelegramError()
        self.assertEqual(handle_reply_job(self.db, second, self.bot), "delivery_unknown")
        self.assertIsNone(claim_job(self.db, ("telegram_reply",), 60))

    def test_send_reservations_limit_bot_and_chat_independently(self):
        with patch("app.telegram.rate_limit.GLOBAL_INTERVAL", timedelta(seconds=10)):
            first = self.queue_reply("first", ADMIN)
            self.assertEqual(handle_reply_job(self.db, first, self.bot, pace=True), "done")
            another = self.queue_reply("global", STUDENT)
            self.assertEqual(handle_reply_job(self.db, another, self.bot, pace=True), "queued")

        self.db.execute("UPDATE telegram_send_limits SET next_send_at = clock_timestamp() WHERE chat_id = 0")
        self.db.execute("""
            UPDATE telegram_send_limits SET next_send_at = clock_timestamp() + interval '10 seconds'
            WHERE chat_id = %s
        """, (ADMIN,))
        same = self.queue_reply("same-chat", ADMIN)
        self.assertEqual(handle_reply_job(self.db, same, self.bot, pace=True), "queued")
        free = self.queue_reply("free-chat", OUTSIDER)
        self.assertEqual(handle_reply_job(self.db, free, self.bot, pace=True), "done")
        self.assertEqual([message["chat_id"] for message in self.bot.messages], [ADMIN, OUTSIDER])

    def test_forbidden_chat_is_skipped_until_telegram_restores_access(self):
        update_id = next(self.next_id)
        self.update_ids.append(update_id)
        restored_access = Update(update_id=update_id, my_chat_member={
            "chat": {"id": GROUP_CHAT, "type": "supergroup"},
            "new_chat_member": {"status": "administrator"},
        })

        def reject_while_readded(_payload):
            telegram_webhook(restored_access, "test-secret")
            raise TelegramError(403)

        first = self.queue_reply("forbidden", GROUP_CHAT)
        with patch.object(self.bot, "send", side_effect=reject_while_readded):
            self.assertEqual(handle_reply_job(self.db, first, self.bot), "failed")
        skipped = self.queue_reply("blocked", GROUP_CHAT)
        self.assertEqual(handle_reply_job(self.db, skipped, self.bot), "done")
        self.assertEqual(self.bot.messages, [])
        self.assertEqual(self.db.execute("SELECT result FROM jobs WHERE id = %s", (skipped.id,)).fetchone()["result"],
                         {"skipped": "chat_unreachable"})

        self.assertEqual(handle_update_job(self.db, claim_job(self.db, ("telegram_update",), 60), self.bot), "done")
        restored = self.queue_reply("restored", GROUP_CHAT)
        self.assertEqual(handle_reply_job(self.db, restored, self.bot), "done")
        self.assertEqual([message["chat_id"] for message in self.bot.messages], [GROUP_CHAT])
