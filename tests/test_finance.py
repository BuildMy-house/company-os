import os
import unittest

import psycopg

from company_ops.ledger import Ledger
from company_ops.policy import action_cost

_DSN = os.environ.get("TEST_COMPANY_DATABASE_URL", "")


@unittest.skipUnless(_DSN, "TEST_COMPANY_DATABASE_URL not set")
class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(_DSN)
        self.ledger.init()

    def tearDown(self):
        try:
            with self.ledger.db.cursor() as cur:
                for t in ("finance_expenses", "finance_revenue"):
                    cur.execute(f"DELETE FROM {t}")
            self.ledger.db.commit()
        except psycopg.OperationalError:
            pass
        finally:
            self.ledger.close()

    def test_add_expense_round_trips(self):
        expense_id = self.ledger.add_expense("claude_subscription", 2000, "monthly", "manual entry")
        self.assertTrue(expense_id.startswith("EXP-"))
        with self.ledger.db.cursor() as cur:
            cur.execute("SELECT * FROM finance_expenses WHERE id = %s", (expense_id,))
            row = cur.fetchone()
        self.assertEqual(row["category"], "claude_subscription")
        self.assertEqual(row["amount_cents"], 2000)
        self.assertEqual(row["recurrence"], "monthly")
        self.assertEqual(row["source"], "manual entry")

    def test_status_includes_total_expenses(self):
        self.ledger.add_expense("server_hosting", 500, "monthly")
        self.ledger.add_expense("opencode_paid_usage", 300, "one_time")
        status = self.ledger.status()
        self.assertEqual(status["total_expenses_cents"], 800)

    def test_ads_action_cost(self):
        self.assertEqual(action_cost("ads"), 5)


if __name__ == "__main__":
    unittest.main()
