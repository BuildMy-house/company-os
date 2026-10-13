"""Real-Postgres tests for PmStore conversation boundaries and history reads.

Uses TEST_COMPANY_DATABASE_URL (a DB that already has sql/pm_schema.sql) when
set; otherwise starts a scratch postgres:16-alpine via docker (same pattern as
tests/test_provision_db_roles.py). Skips cleanly when neither is available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest
import uuid

import psycopg

from company_ops.pm_store import (
    CONVERSATION_IDLE_SECONDS,
    FILED_GAP_SECONDS,
    PmStore,
    conversation_ended,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool_row(outcome):
    return {
        "role": "tool",
        "content": json.dumps(
            {"tool": "file_feedback_work", "result": {"outcome": outcome}}
        ),
    }


class ConversationEndedTest(unittest.TestCase):
    """Pure logic, no database."""

    def test_idle_timeout_ends(self):
        self.assertTrue(conversation_ended([], CONVERSATION_IDLE_SECONDS + 1))
        self.assertFalse(conversation_ended([], 60))

    def test_filing_then_gap_ends_but_immediate_followup_does_not(self):
        msgs = [{"role": "user", "content": "x"}, _tool_row("filed_new")]
        self.assertFalse(conversation_ended(msgs, FILED_GAP_SECONDS - 1))
        self.assertTrue(conversation_ended(msgs, FILED_GAP_SECONDS + 1))

    def test_every_filing_outcome_counts_and_others_do_not(self):
        for outcome in ("filed_new", "already_known", "escalated", "recorded_unfiled"):
            self.assertTrue(conversation_ended([_tool_row(outcome)], 200), outcome)
        self.assertFalse(conversation_ended([_tool_row("rate_limited")], 200))
        lookup = {"role": "tool", "content": json.dumps({"tool": "find_similar_signal", "result": {}})}
        self.assertFalse(conversation_ended([lookup], 200))
        self.assertFalse(conversation_ended([{"role": "tool", "content": "not json"}], 200))


class ScratchDb:
    dsn = ""
    name = ""

    @classmethod
    def start(cls):
        dsn = os.environ.get("TEST_COMPANY_DATABASE_URL")
        if dsn:
            cls.dsn = dsn
            return
        if not shutil.which("docker"):
            raise unittest.SkipTest("no TEST_COMPANY_DATABASE_URL and no docker")
        cls.name = "pm-store-test-" + uuid.uuid4().hex[:8]
        out = subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", cls.name, "-p", "127.0.0.1::5432",
             "-e", "POSTGRES_PASSWORD=scratchpw", "-e", "POSTGRES_DB=homely_company",
             "postgres:16-alpine"], capture_output=True, text=True)
        if out.returncode != 0:
            raise unittest.SkipTest("cannot start scratch postgres: " + out.stderr[:200])
        port = subprocess.run(["docker", "port", cls.name, "5432/tcp"], capture_output=True,
                              text=True).stdout.split(":")[-1].strip()
        cls.dsn = f"postgresql://postgres:scratchpw@127.0.0.1:{port}/homely_company"
        for _ in range(60):
            try:
                with psycopg.connect(cls.dsn, connect_timeout=2) as c:
                    c.execute("select 1")
                time.sleep(2)  # image restarts once during init
                with psycopg.connect(cls.dsn, connect_timeout=2) as c:
                    c.execute("select 1")
                    c.execute("CREATE SCHEMA IF NOT EXISTS company")
                    with open(os.path.join(REPO, "sql", "pm_schema.sql")) as f:
                        c.execute(f.read())
                return
            except psycopg.Error:
                time.sleep(1)
        cls.stop()
        raise unittest.SkipTest("scratch postgres never became ready")

    @classmethod
    def stop(cls):
        if cls.name:
            subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)


class PmStoreConversationDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ScratchDb.start()

    @classmethod
    def tearDownClass(cls):
        ScratchDb.stop()

    def setUp(self):
        self.user = "TEST-PMC-" + uuid.uuid4().hex[:8]
        self.store = PmStore(ScratchDb.dsn)
        self.addCleanup(self.store.close)
        self.addCleanup(
            lambda: self.store.conn.execute(
                "DELETE FROM company.pm_conversations WHERE app_user_id LIKE 'TEST-PMC-%'"
            )
        )

    def _age(self, conv_id, seconds):
        self.store.conn.execute(
            "UPDATE company.pm_conversations SET last_message_at = now() - make_interval(secs => %s) "
            "WHERE id = %s", (seconds, conv_id))

    def test_same_conversation_while_active(self):
        c1 = self.store.get_or_create_conversation(self.user)
        self.store.record_message(c1, self.user, "user", "hi")
        self.assertEqual(self.store.get_or_create_conversation(self.user), c1)

    def test_idle_timeout_starts_new_conversation(self):
        c1 = self.store.get_or_create_conversation(self.user)
        self.store.record_message(c1, self.user, "user", "old")
        self._age(c1, CONVERSATION_IDLE_SECONDS + 5)
        c2 = self.store.get_or_create_conversation(self.user)
        self.assertNotEqual(c1, c2)
        self.assertEqual(self.store.recent_messages(c2, self.user, 10), [])

    def test_filing_then_gap_starts_new_conversation(self):
        c1 = self.store.get_or_create_conversation(self.user)
        self.store.record_message(c1, self.user, "user", "bug")
        self.store.record_message(c1, self.user, "tool", _tool_row("filed_new")["content"])
        self.store.record_message(c1, self.user, "assistant", "Filed, thanks")
        self.assertEqual(self.store.get_or_create_conversation(self.user), c1)
        self._age(c1, FILED_GAP_SECONDS + 5)
        self.assertNotEqual(self.store.get_or_create_conversation(self.user), c1)

    def test_recent_messages_scoped_ordered_and_limited(self):
        other = "TEST-PMC-other-" + uuid.uuid4().hex[:6]
        c1 = self.store.get_or_create_conversation(self.user)
        c2 = self.store.get_or_create_conversation(other)
        for i in range(5):
            self.store.record_message(c1, self.user, "user", f"mine {i}")
        self.store.record_message(c2, other, "user", "theirs")
        got = self.store.recent_messages(c1, self.user, 3)
        self.assertEqual(got, [("user", "mine 2"), ("user", "mine 3"), ("user", "mine 4")])
        # Wrong owner for the conversation id returns nothing.
        self.assertEqual(self.store.recent_messages(c2, self.user, 10), [])
        self.assertEqual(self.store.recent_messages(c1, self.user, 0), [])
