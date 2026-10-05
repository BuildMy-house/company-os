from __future__ import annotations

import os
import unittest

import psycopg
from psycopg.rows import dict_row

from company_ops.pm_store import PmStore

_DSN = os.environ.get("TEST_COMPANY_DATABASE_URL", "")
SUPERUSER_DSN = "postgresql://postgres:localtestpw@localhost:5544/homely_company"

# Rows created by this test file are prefixed so cleanup stays scoped when the
# harness Postgres is shared with other suites/sessions.
_PREFIX = "TEST-PM%"


def _cleanup():
    """Delete only this test file's rows (superuser)."""
    conn = psycopg.connect(SUPERUSER_DSN, autocommit=True)
    conn.execute(
        "DELETE FROM company.pm_conversations WHERE app_user_id LIKE %s",
        (_PREFIX,),
    )
    conn.execute(
        "DELETE FROM company.feedback_signals WHERE topic LIKE %s",
        (_PREFIX,),
    )
    conn.close()


@unittest.skipUnless(_DSN, "TEST_COMPANY_DATABASE_URL not set")
class TestPmStore(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.store = PmStore(_DSN)

    def tearDown(self):
        self.store.close()
        _cleanup()

    def _get_conversation(self, conversation_id: str):
        cur = self.store.conn.execute(
            "SELECT * FROM company.pm_conversations WHERE id = %s",
            (conversation_id,),
        )
        return cur.fetchone()

    def _get_signal(self, signal_id: str):
        cur = self.store.conn.execute(
            "SELECT * FROM company.feedback_signals WHERE id = %s",
            (signal_id,),
        )
        return cur.fetchone()

    def test_requires_a_dsn(self):
        old = os.environ.pop("PM_DATABASE_URL", None)
        try:
            with self.assertRaises(ValueError):
                PmStore()
        finally:
            if old is not None:
                os.environ["PM_DATABASE_URL"] = old

    def test_get_or_create_creates_new_conversation(self):
        cid = self.store.get_or_create_conversation("TEST-PM-user-1")
        row = self._get_conversation(cid)
        self.assertIsNotNone(row)
        self.assertEqual(row["app_user_id"], "TEST-PM-user-1")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["last_message_at"])
        self.assertIsNone(row["summary"])
        self.assertEqual(row["messages"], [])

    def test_get_or_create_reuses_existing_conversation(self):
        first = self.store.get_or_create_conversation("TEST-PM-user-1")
        second = self.store.get_or_create_conversation("TEST-PM-user-1")
        self.assertEqual(first, second)
        cur = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM company.pm_conversations "
            "WHERE app_user_id = 'TEST-PM-user-1'"
        )
        self.assertEqual(cur.fetchone()["n"], 1)

    def test_get_or_create_distinct_users_get_distinct_ids(self):
        a = self.store.get_or_create_conversation("TEST-PM-user-a")
        b = self.store.get_or_create_conversation("TEST-PM-user-b")
        self.assertNotEqual(a, b)

    def test_record_message_appends_and_bumps_last_message_at(self):
        cid = self.store.get_or_create_conversation("TEST-PM-user-1")
        started = self._get_conversation(cid)["started_at"]

        out = self.store.record_message(cid, "TEST-PM-user-1", "user", "hello")
        self.assertEqual(out, cid)
        self.store.record_message(cid, "TEST-PM-user-1", "assistant", "hi there")

        row = self._get_conversation(cid)
        messages = row["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "hello")
        self.assertIsNotNone(messages[0]["at"])
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["content"], "hi there")
        self.assertGreaterEqual(row["last_message_at"], started)

    def test_record_message_unknown_conversation_raises(self):
        with self.assertRaises(ValueError):
            self.store.record_message(
                "00000000-0000-0000-0000-000000000000",
                "TEST-PM-user-1", "user", "hi",
            )

    def test_record_message_wrong_app_user_raises(self):
        cid = self.store.get_or_create_conversation("TEST-PM-user-1")
        with self.assertRaises(ValueError):
            self.store.record_message(cid, "TEST-PM-user-evil", "user", "hi")

    def test_find_similar_signal_returns_none_when_missing(self):
        self.assertIsNone(self.store.find_similar_signal("TEST-PM-nothing-here"))

    def test_find_similar_signal_normalizes_input(self):
        self.store.upsert_signal("TEST-PM Dark Mode ", "it is too bright")
        found = self.store.find_similar_signal("TEST-PM DARK MODE")
        self.assertIsNotNone(found)
        self.assertEqual(found["normalized_topic"], "test-pm dark mode")

    def test_upsert_signal_creates_row_with_defaults(self):
        sid = self.store.upsert_signal("TEST-PM slow search", "quote one")
        row = self._get_signal(sid)
        self.assertIsNotNone(row)
        self.assertEqual(row["topic"], "TEST-PM slow search")
        self.assertEqual(row["normalized_topic"], "test-pm slow search")
        self.assertEqual(row["occurrence_count"], 1)
        self.assertEqual(row["example_quotes"], ["quote one"])
        self.assertEqual(row["status"], "new")
        self.assertIsNone(row["linked_hive_work_id"])
        self.assertIsNotNone(row["first_seen_at"])
        self.assertIsNotNone(row["last_seen_at"])

    def test_upsert_signal_increments_and_appends_quote(self):
        first = self.store.upsert_signal("TEST-PM slow search", "quote one")
        first_seen = self._get_signal(first)["first_seen_at"]

        again = self.store.upsert_signal("TEST-PM SLOW SEARCH", "quote two")
        self.assertEqual(again, first)

        row = self._get_signal(first)
        self.assertEqual(row["occurrence_count"], 2)
        self.assertEqual(row["example_quotes"], ["quote one", "quote two"])
        self.assertEqual(row["first_seen_at"], first_seen)

    def test_upsert_signal_distinct_topics_get_distinct_ids(self):
        a = self.store.upsert_signal("TEST-PM topic one", "q1")
        b = self.store.upsert_signal("TEST-PM topic two", "q2")
        self.assertNotEqual(a, b)
        self.assertEqual(self._get_signal(a)["occurrence_count"], 1)
        self.assertEqual(self._get_signal(b)["occurrence_count"], 1)

    def test_mark_signal_filed_sets_status_and_link(self):
        sid = self.store.upsert_signal("TEST-PM filed topic", "q")
        out = self.store.mark_signal_filed(sid, "HIVE-WORK-123")
        self.assertEqual(out, sid)
        row = self._get_signal(sid)
        self.assertEqual(row["status"], "filed")
        self.assertEqual(row["linked_hive_work_id"], "HIVE-WORK-123")

    def test_mark_signal_filed_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            self.store.mark_signal_filed(
                "00000000-0000-0000-0000-000000000000", "HIVE-WORK-123"
            )

    def test_status_check_constraint_rejects_unknown_status(self):
        conn = psycopg.connect(SUPERUSER_DSN, autocommit=True, row_factory=dict_row)
        with self.assertRaises(psycopg.Error):
            conn.execute(
                "INSERT INTO company.feedback_signals "
                "    (topic, normalized_topic, status) "
                "VALUES ('TEST-PM bad status', 'test-pm bad status', 'bogus')"
            )
        conn.close()

    def test_normalized_topic_index_exists_and_allows_duplicates(self):
        conn = psycopg.connect(SUPERUSER_DSN, autocommit=True, row_factory=dict_row)
        cur = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'company' AND tablename = 'feedback_signals'"
        )
        defs = [r["indexdef"] for r in cur.fetchall()]
        self.assertTrue(
            any("normalized_topic" in d for d in defs),
            f"expected a normalized_topic index, got: {defs}",
        )
        self.assertFalse(
            any("UNIQUE" in d and "normalized_topic" in d for d in defs),
            "normalized_topic index must not be unique (manual merge later)",
        )
        for suffix in ("a", "b"):
            conn.execute(
                "INSERT INTO company.feedback_signals (topic, normalized_topic) "
                "VALUES (%s, 'TEST-PM dupe')",
                (f"TEST-PM dupe {suffix}",),
            )
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM company.feedback_signals "
            "WHERE normalized_topic = 'TEST-PM dupe'"
        )
        self.assertEqual(cur.fetchone()["n"], 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
