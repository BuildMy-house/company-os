from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import psycopg

from company_ops import human_interface
from company_ops.human_interface import (
    ask_information,
    ask_judgment,
    complete_human_interface_request,
    request_action,
    request_approval,
)
from company_ops.observer import VALID_TABLES, ObserverWriter

OBSERVER_DSN = os.environ.get("TEST_OBSERVER_DATABASE_URL")
SUPERUSER_DSN = "postgresql://postgres:localtestpw@localhost:5544/homely_company"


def _cleanup():
    """Delete all rows from observer tables (superuser only)."""
    conn = psycopg.connect(SUPERUSER_DSN, autocommit=True)
    for table in VALID_TABLES:
        conn.execute(f"DELETE FROM observer.{table}")
    conn.close()


@unittest.skipUnless(OBSERVER_DSN, "TEST_OBSERVER_DATABASE_URL not set")
class TestHumanInterface(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.writer = ObserverWriter(OBSERVER_DSN)
        self.sent: list[tuple[tuple, dict]] = []
        self._orig_send_dm = human_interface._send_dm
        self._orig_dm_target = human_interface._dm_target
        self._orig_post_message = human_interface._post_message
        human_interface._send_dm = lambda *a, **k: self.sent.append((a, k))
        human_interface._dm_target = lambda: "TEST_USER_ID"

    def tearDown(self):
        human_interface._send_dm = self._orig_send_dm
        human_interface._dm_target = self._orig_dm_target
        human_interface._post_message = self._orig_post_message
        self.writer.close()
        _cleanup()

    def _count_rows(self, question: str) -> int:
        cur = self.writer.conn.execute(
            "SELECT count(*) AS n FROM observer.human_requests "
            "WHERE lower(trim(question)) = %s",
            (question.strip().lower(),),
        )
        return cur.fetchone()["n"]

    def test_ask_information_no_prior(self):
        result = ask_information(self.writer, "What is the repo layout?")

        self.assertEqual(result["source"], "human")
        self.assertIsNone(result["outcome"])
        self.assertTrue(result["request_id"].startswith("HUMA-"))
        self.assertEqual(len(self.sent), 1)

        n = self._count_rows("What is the repo layout?")
        self.assertEqual(n, 1)

    def test_ask_information_returns_cache_on_prior_answer(self):
        original_id = self.writer.record_human_request(
            type="ask_information",
            question="What is the schema?",
        )
        self.writer.complete_human_request(
            original_id=original_id,
            outcome="10 tables in observer schema",
        )

        result = ask_information(self.writer, "What is the schema?")

        self.assertEqual(result["source"], "cache")
        self.assertEqual(result["request_id"], original_id)
        self.assertEqual(result["outcome"], "10 tables in observer schema")
        self.assertEqual(len(self.sent), 0)

        n = self._count_rows("What is the schema?")
        self.assertEqual(n, 2)  # original + completion, no new request

    def test_ask_judgment(self):
        result = ask_judgment(
            self.writer,
            "Which hosting provider?",
            options="AWS, GCP, Azure",
        )

        self.assertEqual(result["source"], "human")
        self.assertIsNone(result["outcome"])
        self.assertTrue(result["request_id"].startswith("HUMA-"))
        self.assertEqual(len(self.sent), 1)

        cur = self.writer.conn.execute(
            "SELECT type FROM observer.human_requests WHERE id = %s",
            (result["request_id"],),
        )
        row = cur.fetchone()
        self.assertEqual(row["type"], "ask_judgment")

    def test_request_approval(self):
        result = request_approval(
            self.writer,
            "Buy new monitor",
            cost_estimate="$500",
            risk="Budget tight",
        )

        self.assertEqual(result["source"], "human")
        self.assertIsNone(result["outcome"])
        self.assertTrue(result["request_id"].startswith("HUMA-"))
        self.assertEqual(len(self.sent), 1)

        cur = self.writer.conn.execute(
            "SELECT type FROM observer.human_requests WHERE id = %s",
            (result["request_id"],),
        )
        row = cur.fetchone()
        self.assertEqual(row["type"], "request_approval")

    def test_request_action(self):
        result = request_action(
            self.writer,
            "Update DNS records",
            reason="Domain expiring",
        )

        self.assertEqual(result["source"], "human")
        self.assertIsNone(result["outcome"])
        self.assertTrue(result["request_id"].startswith("HUMA-"))
        self.assertEqual(len(self.sent), 1)

        cur = self.writer.conn.execute(
            "SELECT type FROM observer.human_requests WHERE id = %s",
            (result["request_id"],),
        )
        row = cur.fetchone()
        self.assertEqual(row["type"], "request_action")

    def test_complete_human_interface_request(self):
        info = ask_information(self.writer, "What port does Postgres use?")
        request_id = info["request_id"]

        comp_id = complete_human_interface_request(
            self.writer,
            request_id,
            outcome="Port 5432",
            human_minutes=0.5,
        )

        self.assertTrue(comp_id.startswith("HUMA-"))
        self.assertNotEqual(comp_id, request_id)

        prior = self.writer.find_prior_answer("What port does Postgres use?")
        self.assertIsNotNone(prior)
        self.assertEqual(prior["outcome"], "Port 5432")
        self.assertEqual(prior["original_id"], request_id)


@unittest.skipUnless(OBSERVER_DSN, "TEST_OBSERVER_DATABASE_URL not set")
class TestPostMessageTokenError(unittest.TestCase):
    def test_post_message_raises_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            if "DISCORD_BOT_TOKEN" in os.environ:
                del os.environ["DISCORD_BOT_TOKEN"]
            with self.assertRaises(RuntimeError, msg="should raise when token missing"):
                human_interface._post_message("12345", "hello")


if __name__ == "__main__":
    unittest.main()
