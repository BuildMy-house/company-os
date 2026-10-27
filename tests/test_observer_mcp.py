from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest

from company_ops import observer_mcp
from company_ops.observer import ObserverWriter


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
