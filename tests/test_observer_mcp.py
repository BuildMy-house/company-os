from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest

from company_ops import observer_mcp
from company_ops.observer import ObserverWriter, VALID_TABLES


class FakeWriter:
    def __init__(self, fail: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def __getattr__(self, name):
        def method(**kw):
            if self.fail:
                raise self.fail
            self.calls.append((name, kw))
            return "DECI-abc123"
        return method


def rpc(factory, method, params=None, id_=1):
    return observer_mcp.handle(
        factory, {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    )


class FakeReader:
    """Stands in for ObserverWriter.read: mimics its table validation."""

    def __init__(self, rows=None, fail: Exception | None = None):
        self.rows = rows if rows is not None else [
            {"id": "DECI-1", "created_at": "2026-10-28T00:00:00+00:00"}
        ]
        self.fail = fail
        self.calls: list[dict] = []

    def observer_ledger_read(self, table, since=None, limit=50, id=None):
        if table not in VALID_TABLES:
            raise ValueError(f"Invalid table {table!r}")
        if self.fail:
            raise self.fail
        self.calls.append({"table": table, "since": since, "limit": limit, "id": id})
        return self.rows


class TestObserverMcp(unittest.TestCase):
    def test_every_tool_maps_to_a_writer_method_and_signature(self):
        import inspect

        for name, (_, schema) in observer_mcp.TOOLS.items():
            params = inspect.signature(getattr(ObserverWriter, name)).parameters
            for prop in schema["properties"]:
                self.assertIn(prop, params, f"{name}.{prop} not a writer parameter")
            for req in schema["required"]:
                self.assertIn(req, schema["properties"])

    def test_record_decision_is_listed(self):
        r = rpc(lambda: FakeWriter(), "tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertIn("record_decision", names)

    def test_record_decision_call_reaches_writer_and_returns_id(self):
        w = FakeWriter()
        r = rpc(lambda: w, "tools/call", {
            "name": "record_decision",
            "arguments": {"problem": "p", "decision": "d", "initiated_by": "hermes"},
        })
        self.assertNotIn("isError", r["result"])
        self.assertEqual(r["result"]["content"][0]["text"], "DECI-abc123")
        self.assertEqual(
            w.calls, [("record_decision", {"problem": "p", "decision": "d", "initiated_by": "hermes"})]
        )

    def test_db_failure_is_loud_not_silent(self):
        w = FakeWriter(fail=RuntimeError("permission denied for table decisions"))
        r = rpc(lambda: w, "tools/call", {
            "name": "record_decision", "arguments": {"problem": "p", "decision": "d"},
        })
        self.assertTrue(r["result"]["isError"])
        text = r["result"]["content"][0]["text"]
        self.assertIn("NOT RECORDED", text)
        self.assertIn("permission denied", text)

    def test_connection_failure_is_loud(self):
        def boom():
            raise ConnectionError("db down")

        r = rpc(boom, "tools/call", {
            "name": "record_failure", "arguments": {"description": "x"},
        })
        self.assertTrue(r["result"]["isError"])
        self.assertIn("db down", r["result"]["content"][0]["text"])

    def test_missing_and_unknown_args_rejected(self):
        for args in ({"problem": "p"}, {"problem": "p", "decision": "d", "bogus": "1"}):
            r = rpc(lambda: FakeWriter(), "tools/call", {"name": "record_decision", "arguments": args})
            self.assertTrue(r["result"]["isError"])

    def test_unknown_tool_is_error(self):
        r = rpc(lambda: FakeWriter(), "tools/call", {"name": "drop_table", "arguments": {}})
        self.assertTrue(r["result"]["isError"])

    def test_observer_ledger_read_is_listed_with_enum(self):
        r = rpc(lambda: FakeWriter(), "tools/list")
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertIn("observer_ledger_read", tools)
        schema = tools["observer_ledger_read"]["inputSchema"]
        self.assertEqual(schema["properties"]["table"]["enum"], sorted(VALID_TABLES))

    def test_observer_ledger_read_returns_rows_as_json(self):
        rows = [{"id": "FAIL-1", "created_at": "2026-10-28T01:00:00+00:00"}]
        w = FakeReader(rows=rows)
        r = rpc(lambda: w, "tools/call", {"name": "observer_ledger_read", "arguments": {"table": "failures"}})
        self.assertNotIn("isError", r["result"])
        self.assertEqual(json.loads(r["result"]["content"][0]["text"]), rows)
        self.assertEqual(w.calls, [{"table": "failures", "since": None, "limit": 50, "id": None}])

    def test_observer_ledger_read_filters_and_limit(self):
        w = FakeReader()
        r = rpc(lambda: w, "tools/call", {
            "name": "observer_ledger_read",
            "arguments": {"table": "decisions", "since": "2026-10-01", "limit": 10, "id": "DECI-1"},
        })
        self.assertNotIn("isError", r["result"])
        self.assertEqual(
            w.calls,
            [{"table": "decisions", "since": "2026-10-01", "limit": 10, "id": "DECI-1"}],
        )

    def test_observer_ledger_read_bad_table_is_error(self):
        r = rpc(lambda: FakeReader(), "tools/call", {
            "name": "observer_ledger_read", "arguments": {"table": "user_passwords"},
        })
        self.assertTrue(r["result"]["isError"])
        self.assertIn("Invalid table", r["result"]["content"][0]["text"])

    def test_observer_ledger_read_missing_table_is_error(self):
        r = rpc(lambda: FakeReader(), "tools/call", {
            "name": "observer_ledger_read", "arguments": {},
        })
        self.assertTrue(r["result"]["isError"])
        self.assertIn("missing required argument", r["result"]["content"][0]["text"])

    def test_observer_ledger_read_rejects_unknown_args_cannot_inject_writes(self):
        for bogus in ({"problem": "x"}, {"id": "DECI-1", "decision": "drop all rows"}):
            args = {k: v for k, v in bogus.items() if k != "id"}
            r = rpc(lambda: FakeReader(), "tools/call", {
                "name": "observer_ledger_read",
                "arguments": {"table": "decisions", **args},
            })
            self.assertTrue(r["result"]["isError"], f"must reject {args}")
            self.assertIn("unknown argument", r["result"]["content"][0]["text"])

    def test_observer_ledger_read_db_failure_is_loud(self):
        w = FakeReader(fail=RuntimeError("permission denied for table decisions"))
        r = rpc(lambda: w, "tools/call", {
            "name": "observer_ledger_read", "arguments": {"table": "decisions"},
        })
        self.assertTrue(r["result"]["isError"])
        self.assertIn("permission denied", r["result"]["content"][0]["text"])

    def test_observer_ledger_read_never_invokes_write_tools(self):
        w = FakeWriter()
        r = rpc(lambda: w, "tools/call", {
            "name": "observer_ledger_read", "arguments": {"table": "decisions"},
        })
        self.assertNotIn("isError", r["result"])
        self.assertEqual(
            {name for name, _ in w.calls}, {"observer_ledger_read"},
            "the read tool must only call the reader, never a write method",
        )

    def test_serve_round_trip_over_stdio_framing(self):
        w = FakeWriter()
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        out = io.StringIO()
        observer_mcp.serve(lambda: w, io.StringIO("\n".join(map(json.dumps, lines)) + "\n"), out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])

    def test_startup_without_dsn_exits_nonzero(self):
        env = {k: v for k, v in os.environ.items() if k != "OBSERVER_DATABASE_URL"}
        p = subprocess.run(
            [sys.executable, "-m", "company_ops.observer_mcp"],
            input="", capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot start", p.stderr)


if __name__ == "__main__":
    unittest.main()


class TestHermesWiring(unittest.TestCase):
    """record_decision was never callable by Hermes: pin the wiring."""

    def test_config_exposes_observer_mcp_to_api_server_and_passes_dsn(self):
        import pathlib

        import yaml

        cfg = yaml.safe_load(
            (pathlib.Path(__file__).parent.parent / "hermes" / "config.yaml").read_text()
        )
        srv = cfg["mcp_servers"]["observer"]
        self.assertEqual(srv["args"], ["-m", "company_ops.observer_mcp"])
        self.assertIn("OBSERVER_DATABASE_URL", srv["env"])
        self.assertIn("observer", cfg["platform_toolsets"]["api_server"])
