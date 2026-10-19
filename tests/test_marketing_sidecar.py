import json, os, tempfile, threading, unittest, urllib.error, urllib.request
from datetime import UTC, datetime
from http.server import HTTPServer

from company_ops import marketing_sidecar as sc

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
REF = "HERMES-APPROVAL-2026-10-15-001"


class Calls(list):
    def __call__(self, tool, args):
        self.append((tool, args))
        return {"draft_id": 5}


class SubstackGateTests(unittest.TestCase):
    def setUp(self):
        self.ledger = sc.Ledger(tempfile.mktemp())
        self.calls = Calls()

    def go(self, body, env=None, now=NOW):
        return sc.handle_substack(body, env or {}, self.ledger, call=self.calls, now=now)

    def test_publish_without_approval_refused_and_not_called(self):
        for ref in (None, "", "short", "has space in it!!", 5):
            with self.assertRaises(sc.Refused):
                self.go({"tool": "publish_draft", "arguments": {"draft_id": 5}, "approval_ref": ref})
        self.assertEqual(self.calls, [])

    def test_publish_with_approval_goes_through_once_and_is_ledgered(self):
        self.go({"tool": "publish_draft", "arguments": {"draft_id": 5}, "approval_ref": REF})
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(sc.Refused):  # same ref reused
            self.go({"tool": "publish_draft", "arguments": {"draft_id": 6}, "approval_ref": REF}, {"MARKETING_LEAD_SUBSTACK_DAILY_CAP": "5"})

    def test_daily_cap(self):
        self.go({"tool": "publish_draft", "arguments": {"draft_id": 5}, "approval_ref": REF})
        with self.assertRaises(sc.Refused):
            self.go({"tool": "publish_draft", "arguments": {"draft_id": 6}, "approval_ref": REF + "-2"})
        # next day is fine
        self.go({"tool": "publish_draft", "arguments": {"draft_id": 6}, "approval_ref": REF + "-2"},
                now=datetime(2026, 10, 16, tzinfo=UTC))

    def test_reddit_entries_in_shared_ledger_do_not_consume_substack_cap(self):
        self.ledger.append({"ts": NOW.isoformat(), "channel": "reddit", "approval_ref": REF})
        self.go({"tool": "publish_draft", "arguments": {"draft_id": 5}, "approval_ref": REF})
        self.assertEqual(len(self.calls), 1)

    def test_draft_and_list_need_no_approval(self):
        self.go({"tool": "create_draft_post", "arguments": {"title": "t", "subtitle": "s", "body": "b"}})
        self.go({"tool": "list_posts", "arguments": {}})
        self.assertEqual([c[0] for c in self.calls], ["create_draft_post", "list_posts"])

    def test_everything_else_refused(self):
        for tool in ("export_subscribers", "get_analytics", "get_post_stats", "upload_image", None):
            with self.assertRaises(sc.Refused):
                self.go({"tool": tool, "arguments": {}})
        self.assertEqual(self.calls, [])


class IngestTests(unittest.TestCase):
    def test_validation_and_rate_limit(self):
        env = {"MARKETING_INGEST_DATABASE_URL": "postgres://x"}
        seen = []

        class Conn:
            def __enter__(s): return s
            def __exit__(s, *a): return False

        def run(conn, e, days, sources):
            seen.append((days, sources))
            return {sources[0]: {"status": "ok"}}

        last = {}
        for bad in ({"source": "drop table"}, {"source": "ga4", "days": 0}, {"source": "ga4", "days": "7"},
                    {"source": "ga4", "days": True}):
            with self.assertRaises(sc.Refused):
                sc.handle_ingest(bad, env, last, connect=lambda d: Conn(), run=run)
        out = sc.handle_ingest({"source": "ga4", "days": 3}, env, last, connect=lambda d: Conn(), run=run, clock=lambda: 100)
        self.assertEqual(seen, [(3, ("ga4",))])
        with self.assertRaises(sc.Refused) as cm:
            sc.handle_ingest({"source": "ga4"}, env, last, connect=lambda d: Conn(), run=run, clock=lambda: 101)
        self.assertEqual(cm.exception.code, 429)
        with self.assertRaises(sc.Refused):
            sc.handle_ingest({"source": "gsc"}, {}, {}, connect=lambda d: Conn(), run=run)

    def test_ready_file_only_when_db_ok(self):
        path = tempfile.mktemp()
        env = {"MARKETING_INGEST_READY_FILE": path, "MARKETING_INGEST_DATABASE_URL": "x"}

        def bad(*a, **k): raise OSError("down")
        self.assertFalse(sc.write_ready(env, connect=bad))
        self.assertFalse(os.path.exists(path))

        class Conn:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, q): pass
        self.assertTrue(sc.write_ready(env, connect=lambda *a, **k: Conn()))
        self.assertTrue(os.path.exists(path))
        self.assertFalse(sc.write_ready({}, connect=lambda *a, **k: Conn()))
        # substack marker only with a complete cookie env
        sub = tempfile.mktemp()
        sc.write_ready({**env, "SUBSTACK_READY_FILE": sub}, connect=lambda *a, **k: Conn())
        self.assertFalse(os.path.exists(sub))
        full = {**env, "SUBSTACK_READY_FILE": sub, "SUBSTACK_PUBLICATION_URL": "u",
                "SUBSTACK_SESSION_TOKEN": "t", "SUBSTACK_USER_ID": "1"}
        sc.write_ready(full, connect=lambda *a, **k: Conn())
        self.assertTrue(os.path.exists(sub))


class HttpTests(unittest.TestCase):
    def test_http_refusals_and_no_secret_in_errors(self):
        env = {"SUBSTACK_SESSION_TOKEN": "COOKIE-VALUE"}
        srv = HTTPServer(("127.0.0.1", 0), sc.make_handler(env, sc.Ledger(tempfile.mktemp())))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        def post(path, body):
            req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            try:
                return urllib.request.urlopen(req).status, ""
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        code, text = post("/substack/call", {"tool": "export_subscribers"})
        self.assertEqual(code, 403)
        self.assertEqual(post("/substack/call", {"tool": "publish_draft", "arguments": {"draft_id": 1}})[0], 403)
        self.assertEqual(post("/nope", {})[0], 404)
        self.assertNotIn("COOKIE-VALUE", text)
        self.assertEqual(srv.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
