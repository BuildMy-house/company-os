import json
import os
import tempfile
import unittest

import psycopg

from company_ops.dispatch_capture import (
    parse_opencode_output,
    parse_codex_output,
    parse_agy_output,
    capture_and_record,
)

_DSN = os.environ.get("TEST_COMPANY_DATABASE_URL", "")


# ── Realistic sample fixtures ──────────────────────────────────────────

OPENCODE_NDJSON = json.dumps({
    "event": "step_finish",
    "part": {
        "tokens": {"total": 1500, "input": 1000, "output": 500, "reasoning": 0,
                    "cache": {"write": 0, "read": 0}},
        "cost": 0.042,
    },
}) + "\n" + json.dumps({
    "event": "step_finish",
    "part": {
        "tokens": {"total": 800, "input": 500, "output": 300, "reasoning": 0,
                    "cache": {"write": 0, "read": 0}},
        "cost": 0.018,
    },
})

CODEX_NDJSON = json.dumps({
    "event": "turn.completed",
    "usage": {"input_tokens": 2000, "output_tokens": 750,
              "cached_input_tokens": 100, "cache_write_input_tokens": 50,
              "reasoning_output_tokens": 0},
}) + "\n" + json.dumps({
    "event": "turn.completed",
    "usage": {"input_tokens": 500, "output_tokens": 200,
              "cached_input_tokens": 0, "cache_write_input_tokens": 0,
              "reasoning_output_tokens": 0},
})

AGY_JSON = json.dumps({
    "status": "SUCCESS",
    "duration_seconds": 12.5,
    "usage": {"input_tokens": 3000, "output_tokens": 1200, "thinking_tokens": 0,
              "cache_read_tokens": 0, "total_tokens": 4200},
})


# ── Unit tests (no DB) ─────────────────────────────────────────────────

class ParseOpencodeTests(unittest.TestCase):
    def test_sums_multiple_step_finish_events(self):
        result = parse_opencode_output(OPENCODE_NDJSON, "mimo-v2.5-free")
        self.assertEqual(result["provider"], "opencode")
        self.assertEqual(result["model"], "mimo-v2.5-free")
        self.assertEqual(result["input_tokens"], 1500)   # 1000 + 500
        self.assertEqual(result["output_tokens"], 800)    # 500 + 300
        self.assertEqual(result["total_tokens"], 2300)    # 1500 + 800
        self.assertEqual(result["cost_cents"], 6)         # round((0.042+0.018)*100)

    def test_zero_cost_free_tier(self):
        line = json.dumps({"event": "step_finish", "part": {
            "tokens": {"total": 100, "input": 60, "output": 40, "reasoning": 0,
                        "cache": {"write": 0, "read": 0}},
            "cost": 0,
        }})
        result = parse_opencode_output(line, "mimo-v2.5-free")
        self.assertEqual(result["input_tokens"], 60)
        self.assertEqual(result["cost_cents"], 0)

    def test_empty_input_returns_zeros(self):
        result = parse_opencode_output("", "some-model")
        self.assertEqual(result["input_tokens"], 0)
        self.assertEqual(result["output_tokens"], 0)
        self.assertEqual(result["cost_cents"], None)

    def test_malformed_lines_ignored(self):
        result = parse_opencode_output("not json\n{broken\n", "model")
        self.assertEqual(result["input_tokens"], 0)

    def test_non_step_finish_events_ignored(self):
        line = json.dumps({"event": "step_start", "part": {}})
        result = parse_opencode_output(line, "model")
        self.assertEqual(result["input_tokens"], 0)


class ParseCodexTests(unittest.TestCase):
    def test_sums_multiple_turn_completed_events(self):
        result = parse_codex_output(CODEX_NDJSON, "o4-mini")
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["model"], "o4-mini")
        self.assertEqual(result["input_tokens"], 2500)   # 2000 + 500
        self.assertEqual(result["output_tokens"], 950)    # 750 + 200
        self.assertEqual(result["total_tokens"], 3450)
        self.assertIsNone(result["cost_cents"])

    def test_empty_input_returns_zeros(self):
        result = parse_codex_output("", "model")
        self.assertEqual(result["input_tokens"], 0)
        self.assertIsNone(result["cost_cents"])

    def test_malformed_input(self):
        result = parse_codex_output("trash", "model")
        self.assertEqual(result["input_tokens"], 0)


class ParseAgyTests(unittest.TestCase):
    def test_parses_valid_json(self):
        result = parse_agy_output(AGY_JSON, "glm-5.3-flash")
        self.assertEqual(result["provider"], "antigravity")
        self.assertEqual(result["model"], "glm-5.3-flash")
        self.assertEqual(result["input_tokens"], 3000)
        self.assertEqual(result["output_tokens"], 1200)
        self.assertEqual(result["total_tokens"], 4200)
        self.assertIsNone(result["cost_cents"])

    def test_empty_input_returns_zeros(self):
        result = parse_agy_output("", "model")
        self.assertEqual(result["input_tokens"], 0)

    def test_malformed_json(self):
        result = parse_agy_output("not json at all", "model")
        self.assertEqual(result["input_tokens"], 0)


# ── DB-backed tests ────────────────────────────────────────────────────

@unittest.skipUnless(_DSN, "TEST_COMPANY_DATABASE_URL not set")
class CaptureAndRecordTests(unittest.TestCase):
    def setUp(self):
        from company_ops.ledger import Ledger
        self.ledger = Ledger(_DSN)
        self.ledger.init()

    def tearDown(self):
        try:
            with self.ledger.db.cursor() as cur:
                cur.execute("DELETE FROM provider_usage")
            self.ledger.db.commit()
        except psycopg.OperationalError:
            pass
        finally:
            self.ledger.close()

    def test_records_provider_usage_row(self):
        parsed = parse_opencode_output(OPENCODE_NDJSON, "mimo-v2.5-free")
        usage_id = capture_and_record(self.ledger, parsed)
        self.assertTrue(usage_id.startswith("USAGE-"))

        with self.ledger.db.cursor() as cur:
            cur.execute("SELECT * FROM provider_usage WHERE id = %s", (usage_id,))
            row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["provider"], "opencode")
        self.assertEqual(row["model"], "mimo-v2.5-free")
        # cost: round((0.042+0.018)*100) = 6
        self.assertEqual(row["provider_cost_cents"], 6)

    def test_policy_fallback_for_unknown_model(self):
        parsed = {"provider": "codex", "model": "o4-mini",
                  "input_tokens": 1000, "output_tokens": 500,
                  "total_tokens": 1500, "cost_cents": None}
        usage_id = capture_and_record(self.ledger, parsed)
        with self.ledger.db.cursor() as cur:
            cur.execute("SELECT provider_cost_cents FROM provider_usage WHERE id = %s", (usage_id,))
            row = cur.fetchone()
        # o4-mini not in MODEL_COSTS → known=False → cost stays 0
        self.assertEqual(row["provider_cost_cents"], 0)

    def test_policy_known_model_estimates_cost(self):
        parsed = {"provider": "opencode", "model": "z-ai/glm-5.3-flash",
                  "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                  "total_tokens": 2_000_000, "cost_cents": None}
        usage_id = capture_and_record(self.ledger, parsed)
        with self.ledger.db.cursor() as cur:
            cur.execute("SELECT provider_cost_cents FROM provider_usage WHERE id = %s", (usage_id,))
            row = cur.fetchone()
        # glm-5.3-flash: ceil((0.075 + 0.250) / 1M * 100) = 33 cents
        self.assertEqual(row["provider_cost_cents"], 33)

    def test_no_pool_id_does_not_raise(self):
        parsed = parse_agy_output(AGY_JSON, "glm-5.3-flash")
        usage_id = capture_and_record(self.ledger, parsed, resource_pool_id=None)
        self.assertTrue(usage_id.startswith("USAGE-"))

    def test_nonexistent_pool_does_not_raise(self):
        from company_ops.resource_pools import ResourcePools
        rp = ResourcePools(_DSN)
        try:
            parsed = parse_codex_output(CODEX_NDJSON, "o4-mini")
            usage_id = capture_and_record(
                self.ledger, parsed, resource_pool_id="TEST-POOL-NONEXISTENT",
                resource_pools_conn=rp,
            )
            self.assertTrue(usage_id.startswith("USAGE-"))
        finally:
            rp.close()

    def test_resource_pool_update_when_pool_exists(self):
        from company_ops.resource_pools import ResourcePools
        rp = ResourcePools(_DSN)
        try:
            rp.record_pool(
                "TEST-POOL-CAPTURE", "opencode-go", "tokens", 100_000, "weekly",
                "2026-09-07T00:00:00+00:00", "2026-09-14T00:00:00+00:00",
                consumed_amount=5000, source="manual",
            )
            parsed = parse_opencode_output(OPENCODE_NDJSON, "mimo-v2.5-free")
            usage_id = capture_and_record(
                self.ledger, parsed, resource_pool_id="TEST-POOL-CAPTURE",
                resource_pools_conn=rp,
            )
            self.assertTrue(usage_id.startswith("USAGE-"))
            pool = rp.get_pool("TEST-POOL-CAPTURE")
            # 5000 + total_tokens (2300) = 7300
            self.assertEqual(pool["consumed_amount"], 7300)
            self.assertIn("dispatch capture:", pool["source"])
        finally:
            with rp.conn.cursor() as cur:
                cur.execute("DELETE FROM resource_pools WHERE pool_id = 'TEST-POOL-CAPTURE'")
            rp.conn.commit()
            rp.close()

    def test_resource_pool_dollar_unit_uses_cost(self):
        from company_ops.resource_pools import ResourcePools
        rp = ResourcePools(_DSN)
        try:
            rp.record_pool(
                "TEST-POOL-DOLLAR", "opencode-go", "dollars", 30.00, "weekly",
                "2026-09-07T00:00:00+00:00", "2026-09-14T00:00:00+00:00",
                consumed_amount=12.50, source="manual",
            )
            parsed = {"provider": "opencode", "model": "z-ai/glm-5.3-flash",
                      "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                      "total_tokens": 2_000_000, "cost_cents": 33}
            usage_id = capture_and_record(
                self.ledger, parsed, resource_pool_id="TEST-POOL-DOLLAR",
                resource_pools_conn=rp,
            )
            self.assertTrue(usage_id.startswith("USAGE-"))
            pool = rp.get_pool("TEST-POOL-DOLLAR")
            # 12.50 + 33/100 = 12.83
            self.assertAlmostEqual(float(pool["consumed_amount"]), 12.83, places=2)
        finally:
            with rp.conn.cursor() as cur:
                cur.execute("DELETE FROM resource_pools WHERE pool_id = 'TEST-POOL-DOLLAR'")
            rp.conn.commit()
            rp.close()


if __name__ == "__main__":
    unittest.main()
