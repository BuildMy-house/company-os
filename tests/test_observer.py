from __future__ import annotations

import os
import unittest

import psycopg

from company_ops.observer import ObserverWriter, VALID_TABLES, now

OBSERVER_DSN = os.environ.get("TEST_OBSERVER_DATABASE_URL")
SUPERUSER_DSN = "postgresql://postgres:localtestpw@localhost:5544/homely_company"


def _cleanup():
    """Delete all rows from observer tables (superuser only)."""
    conn = psycopg.connect(SUPERUSER_DSN, autocommit=True)
    for table in VALID_TABLES:
        conn.execute(f"DELETE FROM observer.{table}")
    conn.close()


@unittest.skipUnless(OBSERVER_DSN, "TEST_OBSERVER_DATABASE_URL not set")
class TestObserverWriter(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.writer = ObserverWriter(OBSERVER_DSN)

    def tearDown(self):
        self.writer.close()
        _cleanup()

    def test_append_rejects_unknown_table(self):
        with self.assertRaises(ValueError, msg="must reject unknown table"):
            self.writer.append("nonexistent_table", foo="bar")

    def test_append_auto_generates_id_and_created_at(self):
        rid = self.writer.append("decisions", problem="test problem")
        self.assertTrue(rid.startswith("DECI-"))
        cur = self.writer.conn.execute(
            "SELECT id, created_at, problem FROM observer.decisions WHERE id = %s",
            (rid,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["problem"], "test problem")
        self.assertIsNotNone(row["created_at"])

    def test_append_respects_explicit_id(self):
        rid = self.writer.append("decisions", id="CUSTOM-123", problem="x")
        self.assertEqual(rid, "CUSTOM-123")

    def test_record_and_complete_human_request(self):
        original_id = self.writer.record_human_request(
            type="ask_information",
            question="What is the repo layout?",
            reason="need context",
            importance="high",
            blocking=True,
            related_ids="DECI-001",
            initiated_by="hermes",
        )
        self.assertTrue(original_id.startswith("HUMA-"))

        completion_id = self.writer.complete_human_request(
            original_id=original_id,
            outcome="The repo has a monorepo layout",
            human_minutes=5.0,
            avoidable=False,
            initiated_by="grace",
        )
        self.assertTrue(completion_id.startswith("HUMA-"))
        self.assertNotEqual(completion_id, original_id)

        cur = self.writer.conn.execute(
            "SELECT references_id, outcome, completed_at, type, question "
            "FROM observer.human_requests WHERE id = %s",
            (completion_id,),
        )
        comp = cur.fetchone()
        self.assertIsNotNone(comp)
        self.assertEqual(comp["references_id"], original_id)
        self.assertEqual(comp["outcome"], "The repo has a monorepo layout")
        self.assertIsNotNone(comp["completed_at"])

        cur2 = self.writer.conn.execute(
            "SELECT outcome, completed_at FROM observer.human_requests WHERE id = %s",
            (original_id,),
        )
        orig = cur2.fetchone()
        self.assertIsNotNone(orig)
        self.assertIsNone(orig["outcome"])
        self.assertIsNone(orig["completed_at"])

    def test_complete_nonexistent_request_raises(self):
        with self.assertRaises(ValueError, msg="must raise for missing id"):
            self.writer.complete_human_request(
                original_id="NOPE-999", outcome="none"
            )

    def test_find_prior_answer_returns_none_before_completion(self):
        self.writer.record_human_request(
            type="ask_information",
            question="What is the schema?",
        )
        result = self.writer.find_prior_answer("What is the schema?")
        self.assertIsNone(result)

    def test_find_prior_answer_returns_outcome_after_completion(self):
        original_id = self.writer.record_human_request(
            type="ask_information",
            question="What is the schema?",
        )
        self.writer.complete_human_request(
            original_id=original_id,
            outcome="It has 10 tables in observer schema",
        )
        result = self.writer.find_prior_answer("What is the schema?")
        self.assertIsNotNone(result)
        self.assertEqual(result["question"], "What is the schema?")
        self.assertEqual(result["outcome"], "It has 10 tables in observer schema")
        self.assertEqual(result["original_id"], original_id)
        self.assertIsNotNone(result["completed_at"])

    def test_find_prior_answer_normalizes_whitespace_and_case(self):
        original_id = self.writer.record_human_request(
            type="ask_information",
            question="  What IS the schema?  ",
        )
        self.writer.complete_human_request(
            original_id=original_id,
            outcome="Answer",
        )
        result = self.writer.find_prior_answer("  what is the schema?  ")
        self.assertIsNotNone(result)
        self.assertEqual(result["outcome"], "Answer")

    def test_find_prior_answer_ignores_non_ask_information_type(self):
        original_id = self.writer.record_human_request(
            type="bug_report",
            question="What is the schema?",
        )
        self.writer.complete_human_request(
            original_id=original_id,
            outcome="Not applicable",
        )
        result = self.writer.find_prior_answer("What is the schema?")
        self.assertIsNone(result)


@unittest.skipUnless(OBSERVER_DSN, "TEST_OBSERVER_DATABASE_URL not set")
class TestObserverPermissions(unittest.TestCase):
    def test_update_raises_permission_denied(self):
        conn = psycopg.connect(OBSERVER_DSN, autocommit=True)
        with self.assertRaises(psycopg.Error):
            conn.execute("UPDATE observer.decisions SET problem='x'")
        conn.close()

    def test_delete_raises_permission_denied(self):
        conn = psycopg.connect(OBSERVER_DSN, autocommit=True)
        with self.assertRaises(psycopg.Error):
            conn.execute("DELETE FROM observer.decisions")
        conn.close()


if __name__ == "__main__":
    unittest.main()
