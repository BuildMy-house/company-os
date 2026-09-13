import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import psycopg

from company_ops.ledger import Ledger
from company_ops.routing import choose_provider
from company_ops.policy import model_cost
from company_ops.telemetry import Telemetry
from company_ops.mcp_client import MCPClient, MCPServer
from company_ops.workers import run_worker

_DSN = os.environ.get("TEST_COMPANY_DATABASE_URL", "")


@unittest.skipUnless(_DSN, "TEST_COMPANY_DATABASE_URL not set")
class CompanyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(_DSN)
        self.ledger.init()

    def tearDown(self):
        try:
            with self.ledger.db.cursor() as cur:
                for t in ("routing_lessons", "provider_usage", "actions",
                           "credit_transactions", "revenue_events", "tool_requests",
                           "deployments", "plans"):
                    cur.execute(f"DELETE FROM {t}")
            self.ledger.db.commit()
        except psycopg.OperationalError:
            pass
        finally:
            self.ledger.close()

    def test_idempotent_charge_and_free_provider_usage(self):
        plan = self.ledger.create_plan("draft SEO improvements")
        first = self.ledger.charge("opencode", "seo", plan, "same-action")
        second = self.ledger.charge("opencode", "seo", plan, "same-action")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.ledger.balance(), 95)
        self.ledger.record_provider_usage(first["id"], "opencode", "free", 0)

    def test_revenue_creates_seventy_percent_credit_value(self):
        result = self.ledger.record_revenue(1000, "subscription")
        self.assertEqual(result["credits_created"], 7)
        self.assertEqual(self.ledger.balance(), 107)

    def test_cost_policy_and_routing_lesson(self):
        self.assertEqual(model_cost("mimo-v2.5-free")["estimated_cost_cents"], 0)
        self.assertEqual(model_cost("z-ai/glm-5.3-flash", 1_000_000, 1_000_000)["estimated_cost_cents"], 33)
        self.ledger.add_routing_lesson("mimo-v2.5-free", "seo", "quota unavailable")
        self.assertEqual(choose_provider("seo", True, self.ledger.avoided_models("seo"))["model"], "z-ai/glm-5.3-flash")

    def test_deployment_record_and_update(self):
        from company_ops.cli import main

        dep_id_json = main(["deployment", "record", "staging", "abc123"])
        self.assertEqual(dep_id_json, 0)

    def test_deployment_update_via_ledger(self):
        dep_id = self.ledger.add_deployment("staging", "def456")
        self.ledger.update_deployment_status(dep_id, "deployed", "all tests passed")
        with self.ledger.db.cursor() as cur:
            cur.execute("SELECT status, result FROM deployments WHERE id = %s", (dep_id,))
            row = cur.fetchone()
        self.assertEqual(row["status"], "deployed")
        self.assertEqual(row["result"], "all tests passed")


class CompanyOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_routing_prefers_free_opencode_but_escalates_risk(self):
        self.assertEqual(choose_provider("seo")["model"], "mimo-v2.5-free")
        self.assertEqual(choose_provider("security")["provider"], "claude")
        self.assertEqual(choose_provider("seo", False)["model"], "z-ai/glm-5.3-flash")

    def test_telemetry_is_jsonl_and_separate(self):
        path = Path(self.temp.name) / "telemetry.jsonl"
        trace = Telemetry(path).emit("task_finished", plan_id="PLAN-1", success=True)
        record = json.loads(path.read_text())
        self.assertEqual(record["trace_id"], trace)
        self.assertEqual(record["event"], "task_finished")

    def test_worker_dry_run_does_not_start_mcp(self):
        result = run_worker("opencode", "seo", "draft", {
            "opencode": MCPServer((sys.executable, "not-started"), "execute_task")
        })
        self.assertEqual(result.status, "planned")
        self.assertTrue(result.dry_run)

    def test_mcp_stdio_call(self):
        server = Path(self.temp.name) / "server.py"
        server.write_text(
            "import sys,json\n"
            "for line in sys.stdin:\n"
            " m=json.loads(line)\n"
            " if m.get('id')==1: print(json.dumps({'jsonrpc':'2.0','id':1,'result':{}}),flush=True)\n"
            " if m.get('id')==2: print(json.dumps({'jsonrpc':'2.0','id':2,'result':{'ok':True}}),flush=True); break\n",
            encoding="utf-8",
        )
        result = MCPClient(MCPServer((sys.executable, str(server)), "execute_task"), 3).call({"prompt": "x"})
        self.assertEqual(result, {"ok": True})

    def test_mcp_async_session_is_polled(self):
        server = Path(self.temp.name) / "async_server.py"
        server.write_text(
            "import sys,json\n"
            "for line in sys.stdin:\n"
            " m=json.loads(line); i=m.get('id')\n"
            " if i==1: print(json.dumps({'jsonrpc':'2.0','id':1,'result':{}}),flush=True)\n"
            " if i==2: print(json.dumps({'jsonrpc':'2.0','id':2,'result':{'content':[{'type':'text','text':'{\\\"sessionId\\\":\\\"s1\\\"}'}]}}),flush=True)\n"
            " if i==3: print(json.dumps({'jsonrpc':'2.0','id':3,'result':{'content':[{'type':'text','text':'{\\\"status\\\":\\\"idle\\\",\\\"result\\\":{\\\"ok\\\":true}}'}]}}),flush=True); break\n",
            encoding="utf-8",
        )
        result = MCPClient(MCPServer((sys.executable, str(server)), "claude_code", poll_tool="claude_code_check", poll_interval=0), 3).call({"prompt": "x"})
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
