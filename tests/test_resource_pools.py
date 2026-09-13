import os
import unittest
from datetime import UTC, datetime, timedelta

import psycopg

from company_ops.resource_pools import ResourcePools

_DSN = os.environ.get("TEST_COMPANY_DATABASE_URL", "")


@unittest.skipUnless(_DSN, "TEST_COMPANY_DATABASE_URL not set")
class ResourcePoolsTests(unittest.TestCase):
    def setUp(self):
        self.rp = ResourcePools(_DSN)

    def tearDown(self):
        try:
            with self.rp.conn.cursor() as cur:
                cur.execute("DELETE FROM resource_pools WHERE pool_id LIKE 'TEST-POOL-%'")
            self.rp.conn.commit()
        except psycopg.OperationalError:
            pass
        finally:
            self.rp.close()

    def test_record_pool_round_trips(self):
        self.rp.record_pool(
            "TEST-POOL-1", "opencode-go", "dollars", 30.00, "weekly",
            "2026-09-07T00:00:00+00:00", "2026-09-14T00:00:00+00:00",
            consumed_amount=12.50, source="manual test", level="claude_to_worker",
        )
        pool = self.rp.get_pool("TEST-POOL-1")
        self.assertIsNotNone(pool)
        self.assertEqual(pool["provider"], "opencode-go")
        self.assertEqual(pool["level"], "claude_to_worker")
        self.assertEqual(pool["unit"], "dollars")
        self.assertEqual(pool["quota_amount"], 30.00)
        self.assertEqual(pool["period_type"], "weekly")
        self.assertEqual(pool["consumed_amount"], 12.50)
        self.assertEqual(pool["source"], "manual test")

    def test_record_pool_upserts(self):
        self.rp.record_pool(
            "TEST-POOL-UPSERT", "provider-a", "requests", 100, "daily",
            "2026-09-07T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
            consumed_amount=10, source="first",
        )
        self.rp.record_pool(
            "TEST-POOL-UPSERT", "provider-b", "tokens", 200, "weekly",
            "2026-09-07T00:00:00+00:00", "2026-09-14T00:00:00+00:00",
            consumed_amount=50, source="second",
        )
        cur = self.rp.conn.execute(
            "SELECT COUNT(*) FROM company.resource_pools WHERE pool_id = 'TEST-POOL-UPSERT'"
        )
        count = cur.fetchone()["count"]
        self.assertEqual(count, 1)
        pool = self.rp.get_pool("TEST-POOL-UPSERT")
        self.assertEqual(pool["provider"], "provider-b")
        self.assertEqual(pool["quota_amount"], 200)
        self.assertEqual(pool["consumed_amount"], 50)
        self.assertEqual(pool["source"], "second")

    def test_update_consumed_nonexistent_raises(self):
        with self.assertRaises(ValueError):
            self.rp.update_consumed("TEST-POOL-NONEXISTENT", 50, "test")

    def test_update_consumed_only_updates_target_fields(self):
        self.rp.record_pool(
            "TEST-POOL-UPDATE", "provider-a", "requests", 100, "daily",
            "2026-09-07T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
            consumed_amount=10, source="original",
        )
        self.rp.update_consumed("TEST-POOL-UPDATE", 75, "updated")
        pool = self.rp.get_pool("TEST-POOL-UPDATE")
        self.assertEqual(pool["consumed_amount"], 75)
        self.assertEqual(pool["source"], "updated")
        self.assertIsNotNone(pool["last_checked_at"])
        # Other fields unchanged
        self.assertEqual(pool["provider"], "provider-a")
        self.assertEqual(pool["quota_amount"], 100)
        self.assertEqual(pool["period_type"], "daily")

    def test_remaining_budget_vs_time_nonexistent_raises(self):
        with self.assertRaises(ValueError):
            self.rp.remaining_budget_vs_time("TEST-POOL-NONEXISTENT")

    def test_remaining_budget_vs_time_balanced(self):
        now_ = datetime.now(UTC)
        period_start = (now_ - timedelta(days=3.5)).isoformat()
        period_end = (now_ + timedelta(days=3.5)).isoformat()
        self.rp.record_pool(
            "TEST-POOL-BALANCED", "opencode-go", "dollars", 100, "weekly",
            period_start, period_end,
            consumed_amount=50, source="test",
        )
        result = self.rp.remaining_budget_vs_time("TEST-POOL-BALANCED")
        self.assertAlmostEqual(result["fraction_budget_remaining"], 0.5, delta=0.05)
        self.assertAlmostEqual(result["fraction_time_remaining"], 0.5, delta=0.05)
        self.assertFalse(result["is_thin"])

    def test_remaining_budget_vs_time_thin(self):
        now_ = datetime.now(UTC)
        # 7-day period, ~10% elapsed (0.7 days in), 90% consumed (only 10% budget left)
        period_start = (now_ - timedelta(days=0.7)).isoformat()
        period_end = (now_ + timedelta(days=6.3)).isoformat()
        self.rp.record_pool(
            "TEST-POOL-THIN", "opencode-go", "dollars", 100, "weekly",
            period_start, period_end,
            consumed_amount=90, source="test",
        )
        result = self.rp.remaining_budget_vs_time("TEST-POOL-THIN")
        self.assertTrue(result["is_thin"])
        self.assertAlmostEqual(result["fraction_budget_remaining"], 0.1, delta=0.05)
        self.assertAlmostEqual(result["fraction_time_remaining"], 0.9, delta=0.05)

    def test_remaining_budget_vs_time_zero_quota(self):
        now_ = datetime.now(UTC)
        period_start = (now_ - timedelta(days=1)).isoformat()
        period_end = (now_ + timedelta(days=6)).isoformat()
        self.rp.record_pool(
            "TEST-POOL-ZERO", "opencode-go", "dollars", 0, "weekly",
            period_start, period_end,
            consumed_amount=0, source="test",
        )
        result = self.rp.remaining_budget_vs_time("TEST-POOL-ZERO")
        self.assertIsNone(result["fraction_budget_remaining"])


if __name__ == "__main__":
    unittest.main()
