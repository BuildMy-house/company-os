import os
import unittest

import psycopg

from company_ops.resource_pools import ResourcePool

_DSN = os.environ.get("TEST_COMPANY_DATABASE_URL", "")


@unittest.skipUnless(_DSN, "TEST_COMPANY_DATABASE_URL not set")
class ResourcePoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = ResourcePool(_DSN)

    def tearDown(self):
        try:
            with self.pool.conn.cursor() as cur:
                cur.execute("DELETE FROM resource_pools")
            self.pool.conn.commit()
        except psycopg.OperationalError:
            pass
        finally:
            self.pool.close()

    def test_add_and_list(self):
        pool_id = self.pool.add("opencode", "free", "5h", 100, "2026-09-07T12:00:00Z")
        self.assertTrue(pool_id.startswith("POOL-"))
        pools = self.pool.list_pools()
        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0]["tool"], "opencode")
        self.assertEqual(pools[0]["tier"], "free")
        self.assertEqual(pools[0]["limit_value"], 100)
        self.assertEqual(pools[0]["used"], 0)

    def test_debit(self):
        pool_id = self.pool.add("opencode", "free", "5h", 100, "2026-09-07T12:00:00Z")
        result = self.pool.debit(pool_id, 5)
        self.assertEqual(result["used"], 5)
        result = self.pool.debit(pool_id, 3)
        self.assertEqual(result["used"], 8)

    def test_debit_nonexistent_raises(self):
        with self.assertRaises(ValueError):
            self.pool.debit("POOL-nonexistent", 1)

    def test_reset(self):
        pool_id = self.pool.add("opencode", "free", "5h", 100, "2026-09-07T12:00:00Z")
        self.pool.debit(pool_id, 50)
        result = self.pool.reset(pool_id)
        self.assertEqual(result["used"], 0)
        self.assertIsNotNone(result["reset_at"])

    def test_reset_nonexistent_raises(self):
        with self.assertRaises(ValueError):
            self.pool.reset("POOL-nonexistent")


if __name__ == "__main__":
    unittest.main()
