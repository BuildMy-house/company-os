from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import psycopg

from company_ops.cli import main
from company_ops.ledger import Ledger
from company_ops.spend_review import (
    compile_spend_review,
    format_spend_review_message,
    run_spend_review,
)

_DSN = os.environ.get("TEST_COMPANY_DATABASE_URL", "")


@unittest.skipUnless(_DSN, "TEST_COMPANY_DATABASE_URL not set")
class SpendReviewCompilationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(_DSN)
        self.ledger.init()
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        self.ledger.close()

    def _cleanup(self):
        try:
            with self.ledger.db.cursor() as cur:
                for t in ("routing_lessons", "provider_usage", "actions", "plans", "finance_expenses", "finance_revenue"):
                    cur.execute(f"DELETE FROM {t}")
            self.ledger.db.commit()
        except psycopg.OperationalError:
            pass

    def test_compile_empty_database(self):
        as_of = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
        report = compile_spend_review(db=self.ledger.db, days=30, as_of=as_of)

        self.assertEqual(report["period_days"], 30)
        self.assertEqual(report["total_spend_cents"], 0)
        self.assertEqual(report["spend_by_category"], {})
        self.assertFalse(report["has_prior_data"])
        self.assertIsNone(report["prior_total_spend_cents"])
        self.assertIsNone(report["month_over_month_delta"])
        self.assertEqual(report["routing_lessons"], [])
        self.assertEqual(report["provider_usage"]["total_cost_cents"], 0)
        self.assertEqual(report["provider_usage"]["call_count"], 0)

    def test_compile_with_expenses_and_provider_usage(self):
        as_of = datetime.now(UTC) + timedelta(minutes=1)

        # 1. Insert expenses in current period (within trailing 30 days)
        self.ledger.add_expense("hosting", 5000, "monthly", "vps")
        self.ledger.add_expense("domains", 1200, "annual", "registrar")

        # 2. Insert action + provider_usage in current period
        plan_id = self.ledger.create_plan("spend review test")
        action = self.ledger.charge("hermes", "deployment", plan_id=plan_id)
        self.ledger.record_provider_usage(action["id"], "openrouter", "gpt-4o-mini", provider_cost_cents=150)
        self.ledger.record_provider_usage(action["id"], "tokenrouter", "glm-free", provider_cost_cents=0)

        # 3. Insert routing lesson
        self.ledger.add_routing_lesson("gpt-4o-mini", "sql_queries", "context limit exceeded")

        report = compile_spend_review(db=self.ledger.db, days=30, as_of=as_of)

        # Total spend: 5000 + 1200 + 150 = 6350 cents ($63.50)
        self.assertEqual(report["total_spend_cents"], 6350)
        self.assertEqual(report["spend_by_category"]["hosting"], 5000)
        self.assertEqual(report["spend_by_category"]["domains"], 1200)
        self.assertEqual(report["spend_by_category"]["provider_api"], 150)

        # Provider usage breakdown
        self.assertEqual(report["provider_usage"]["total_cost_cents"], 150)
        self.assertEqual(report["provider_usage"]["call_count"], 2)
        by_prov = report["provider_usage"]["by_provider"]
        self.assertIn("openrouter", by_prov)
        self.assertEqual(by_prov["openrouter"]["cost_cents"], 150)
        self.assertEqual(by_prov["openrouter"]["call_count"], 1)
        self.assertIn("tokenrouter", by_prov)
        self.assertEqual(by_prov["tokenrouter"]["cost_cents"], 0)

        # Routing lessons
        self.assertEqual(len(report["routing_lessons"]), 1)
        self.assertEqual(report["routing_lessons"][0]["model"], "gpt-4o-mini")
        self.assertEqual(report["routing_lessons"][0]["task_type"], "sql_queries")
        self.assertEqual(report["routing_lessons"][0]["reason"], "context limit exceeded")

    def test_month_over_month_delta_calculation(self):
        as_of = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)

        # Current period expense (e.g. 5 days ago)
        cur_date = (as_of - timedelta(days=5)).isoformat()
        with self.ledger.db.cursor() as cur:
            cur.execute(
                "INSERT INTO finance_expenses (id, category, amount_cents, recurrence, source, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("EXP-cur1", "hosting", 6000, "monthly", "test", cur_date),
            )
            cur.execute(
                "INSERT INTO finance_expenses (id, category, amount_cents, recurrence, source, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("EXP-cur2", "saas", 2000, "monthly", "test", cur_date),
            )

            # Prior period expense (e.g. 45 days ago -> in [as_of - 60d, as_of - 30d))
            prior_date = (as_of - timedelta(days=45)).isoformat()
            cur.execute(
                "INSERT INTO finance_expenses (id, category, amount_cents, recurrence, source, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("EXP-pri1", "hosting", 4000, "monthly", "test", prior_date),
            )
        self.ledger.db.commit()

        report = compile_spend_review(db=self.ledger.db, days=30, as_of=as_of)

        self.assertTrue(report["has_prior_data"])
        self.assertEqual(report["total_spend_cents"], 8000)
        self.assertEqual(report["prior_total_spend_cents"], 4000)

        mom = report["month_over_month_delta"]
        self.assertIsNotNone(mom)
        self.assertEqual(mom["delta_cents"], 4000)  # +$40.00
        self.assertEqual(mom["delta_pct"], 100.0)   # +100%

        hosting_delta = mom["by_category"]["hosting"]
        self.assertEqual(hosting_delta["current_cents"], 6000)
        self.assertEqual(hosting_delta["prior_cents"], 4000)
        self.assertEqual(hosting_delta["delta_cents"], 2000)
        self.assertEqual(hosting_delta["delta_pct"], 50.0)

        saas_delta = mom["by_category"]["saas"]
        self.assertEqual(saas_delta["current_cents"], 2000)
        self.assertEqual(saas_delta["prior_cents"], 0)
        self.assertEqual(saas_delta["delta_cents"], 2000)
        self.assertIsNone(saas_delta["delta_pct"])  # new category


class SpendReviewFormattingTests(unittest.TestCase):
    def test_exploratory_formatting_yields_ask_judgment(self):
        report = {
            "period_days": 30,
            "current_period": {"start": "2026-08-08T00:00:00+00:00", "end": "2026-09-07T00:00:00+00:00"},
            "prior_period": {"start": "2026-07-09T00:00:00+00:00", "end": "2026-08-08T00:00:00+00:00"},
            "has_prior_data": True,
            "total_spend_cents": 5000,
            "spend_by_category": {"hosting": 5000},
            "prior_total_spend_cents": 4000,
            "prior_spend_by_category": {"hosting": 4000},
            "month_over_month_delta": {
                "delta_cents": 1000,
                "delta_pct": 25.0,
                "by_category": {
                    "hosting": {"current_cents": 5000, "prior_cents": 4000, "delta_cents": 1000, "delta_pct": 25.0}
                },
            },
            "provider_usage": {"total_cost_cents": 0, "call_count": 0, "by_provider": {}},
            "routing_lessons": [{"model": "mimo-v2.5-free", "task_type": "seo", "reason": "quota unavailable"}],
            "detected_proposals": [],
        }

        msg = format_spend_review_message(report)
        self.assertEqual(msg["action_type"], "ask_judgment")
        self.assertIn("Monthly Spend Review", msg["question"])
        self.assertIn("Total Spend: $50.00 (+$10.00 (+25.0% MoM))", msg["question"])
        self.assertIn("hosting: $50.00", msg["question"])
        self.assertIn("Notable Routing Lessons", msg["question"])
        self.assertIn("options", msg)
        self.assertIn("approve-as-is", msg["options"])

    def test_proposal_formatting_yields_request_approval(self):
        report = {
            "period_days": 30,
            "current_period": {"start": "2026-08-08T00:00:00+00:00", "end": "2026-09-07T00:00:00+00:00"},
            "prior_period": {"start": "2026-07-09T00:00:00+00:00", "end": "2026-08-08T00:00:00+00:00"},
            "has_prior_data": False,
            "total_spend_cents": 2000,
            "spend_by_category": {"claude": 2000},
            "prior_total_spend_cents": None,
            "prior_spend_by_category": None,
            "month_over_month_delta": None,
            "provider_usage": {"total_cost_cents": 0, "call_count": 0, "by_provider": {}},
            "routing_lessons": [],
            "detected_proposals": [],
        }

        msg = format_spend_review_message(report, proposal="Renew Claude subscription for next month")
        self.assertEqual(msg["action_type"], "request_approval")
        self.assertIn("Proposal: Renew Claude subscription for next month", msg["proposal"])
        self.assertEqual(msg["cost_estimate"], "$20.00")
        self.assertIn("risk", msg)


class SpendReviewRunTests(unittest.TestCase):
    def test_run_dry_run_does_not_call_observer(self):
        with patch("company_ops.spend_review.compile_spend_review") as mock_compile:
            mock_compile.return_value = {
                "period_days": 30,
                "current_period": {"start": "2026-08-08T00:00:00+00:00", "end": "2026-09-07T00:00:00+00:00"},
                "prior_period": {"start": "2026-07-09T00:00:00+00:00", "end": "2026-08-08T00:00:00+00:00"},
                "has_prior_data": False,
                "total_spend_cents": 0,
                "spend_by_category": {},
                "prior_total_spend_cents": None,
                "prior_spend_by_category": None,
                "month_over_month_delta": None,
                "provider_usage": {"total_cost_cents": 0, "call_count": 0, "by_provider": {}},
                "routing_lessons": [],
                "detected_proposals": [],
            }
            res = run_spend_review(company_db="mock_db", dry_run=True)
            self.assertTrue(res["human_interface"]["dry_run"])
            self.assertEqual(res["human_interface"]["action_type"], "ask_judgment")

    def test_run_fires_request_approval_when_proposal_present(self):
        mock_writer = MagicMock()
        mock_writer.record_human_request.return_value = "HUMA-test123"

        with patch("company_ops.spend_review.compile_spend_review") as mock_compile, \
             patch("company_ops.spend_review.request_approval") as mock_ra:
            mock_compile.return_value = {
                "period_days": 30,
                "current_period": {"start": "2026-08-08T00:00:00+00:00", "end": "2026-09-07T00:00:00+00:00"},
                "prior_period": {"start": "2026-07-09T00:00:00+00:00", "end": "2026-08-08T00:00:00+00:00"},
                "has_prior_data": False,
                "total_spend_cents": 3000,
                "spend_by_category": {"server": 3000},
                "prior_total_spend_cents": None,
                "prior_spend_by_category": None,
                "month_over_month_delta": None,
                "provider_usage": {"total_cost_cents": 0, "call_count": 0, "by_provider": {}},
                "routing_lessons": [],
                "detected_proposals": [],
            }
            mock_ra.return_value = {"source": "human", "request_id": "HUMA-test123", "outcome": None}

            res = run_spend_review(
                company_db="mock_db",
                observer_writer=mock_writer,
                proposal="Upgrade memory to 16GB",
            )
            self.assertEqual(res["human_interface"]["action_type"], "request_approval")
            self.assertEqual(res["human_interface"]["request_id"], "HUMA-test123")
            mock_ra.assert_called_once()
            call_kwargs = mock_ra.call_args[1]
            self.assertIn("Upgrade memory to 16GB", call_kwargs["proposal"])
            self.assertEqual(call_kwargs["cost_estimate"], "$30.00")


@unittest.skipUnless(_DSN, "TEST_COMPANY_DATABASE_URL not set")
class SpendReviewCLITests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(_DSN)
        self.ledger.init()

    def tearDown(self):
        self.ledger.close()

    def test_cli_spend_review_compile(self):
        f = io.StringIO()
        with redirect_stdout(f):
            exit_code = main(["spend-review", "compile", "--days", "15"])
        self.assertEqual(exit_code, 0)
        output = json.loads(f.getvalue())
        self.assertEqual(output["period_days"], 15)
        self.assertIn("total_spend_cents", output)
        self.assertIn("spend_by_category", output)

    def test_cli_spend_review_run_dry_run(self):
        f = io.StringIO()
        with redirect_stdout(f):
            exit_code = main(["spend-review", "run", "--dry-run", "--proposal", "Test renewal"])
        self.assertEqual(exit_code, 0)
        output = json.loads(f.getvalue())
        self.assertIn("report", output)
        self.assertIn("human_interface", output)
        self.assertTrue(output["human_interface"]["dry_run"])
        self.assertEqual(output["human_interface"]["action_type"], "request_approval")


if __name__ == "__main__":
    unittest.main()
