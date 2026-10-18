"""Ingester tests: HTTP is faked; DB tests use an ephemeral docker postgres
(skipped without docker). No network, no real credentials."""
import json
import os
import shutil
import subprocess
import time
import unittest
import uuid
from datetime import date

import psycopg

from company_ops import marketing_ingest as mi

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 10, 15)


class Resp:
    def __init__(self, payload=None, status=200, content=b""):
        self.status_code, self._p, self.content = status, payload, content
        self.text = json.dumps(payload) if payload is not None else content.decode()

    def json(self):
        return self._p


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _hit(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for frag, resp in self.routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError("unexpected url " + url)

    def post(self, url, **kw):
        return self._hit("POST", url, **kw)

    def get(self, url, **kw):
        return self._hit("GET", url, **kw)


GOOGLE = {"GOOGLE_CLIENT_ID": "i", "GOOGLE_CLIENT_SECRET": "s", "GOOGLE_REFRESH_TOKEN": "r"}
TOKEN = Resp({"access_token": "tok"})
FEED = b"""<rss><channel><item><title>Hello</title><link>https://x.substack.com/p/hello</link>
<pubDate>Mon, 12 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"""


class FetcherTests(unittest.TestCase):
    def test_ga4_parses_rows(self):
        http = FakeHttp({"oauth2": TOKEN, "runReport": Resp({"rowCount": 1, "rows": [{
            "dimensionValues": [{"value": "20261012"}, {"value": "/a"}, {"value": "Organic Search"}],
            "metricValues": [{"value": "5"}, {"value": "4"}, {"value": "7"}, {"value": "3"}]}]})})
        table, rows = mi.fetch_ga4({**GOOGLE, "GA4_PROPERTY_ID": "123"}, http, TODAY, TODAY)
        self.assertEqual(table, "ga4_daily")
        self.assertEqual(rows[0]["date"], date(2026, 10, 12))
        self.assertEqual((rows[0]["sessions"], rows[0]["total_users"]), (5, 4))
        self.assertEqual(set(rows[0]) - set(mi.KEYS["ga4_daily"]),
                         {"sessions", "total_users", "screen_page_views", "engaged_sessions"})

    def test_gsc_parses_rows(self):
        http = FakeHttp({"oauth2": TOKEN, "searchAnalytics": Resp({"rows": [
            {"keys": ["2026-10-12", "https://s/a", "q"], "clicks": 2, "impressions": 9, "ctr": 0.2, "position": 3.5}]})})
        _, rows = mi.fetch_gsc({**GOOGLE, "GSC_SITE_URL": "sc-domain:s"}, http, TODAY, TODAY)
        self.assertEqual((rows[0]["query"], rows[0]["clicks"]), ("q", 2))
        self.assertIn("sc-domain%3As", http.calls[-1][1])

    def test_reddit_parses_and_requests_only_reads(self):
        env = dict(REDDIT_CLIENT_ID="a", REDDIT_CLIENT_SECRET="b", REDDIT_USERNAME="u", REDDIT_PASSWORD="p")
        http = FakeHttp({"access_token": Resp({"access_token": "t"}), "submitted": Resp({"data": {"children": [
            {"data": {"subreddit": "x", "id": "1", "title": "T", "permalink": "/r/x/1", "created_utc": 1.7e9,
                      "score": 3, "num_comments": 1, "upvote_ratio": 0.9}}]}})})
        _, rows = mi.fetch_reddit(env, http, TODAY, TODAY)
        self.assertEqual(rows[0]["post_id"], "1")
        self.assertFalse([c for c in http.calls if "submit" in c[1] and "api/submit" in c[1]])

    def test_substack_aggregates_via_allowlisted_calls(self):
        env = dict(SUBSTACK_PUBLICATION_URL="https://x.substack.com/", SUBSTACK_SESSION_TOKEN="c", SUBSTACK_USER_ID="1")
        calls = []

        def call(tool, args):
            calls.append(tool)
            if tool == "get_publication_stats":
                return {"subscribers": 10, "views_30d": 50, "open_rate_30d": 0.4}
            return {"total": 1, "posts": [{"post_id": 7, "title": "Hello", "post_date": "2026-10-12T10:00:00Z",
                                           "opens": 3, "open_rate": 0.5, "views": 9}]}

        out = dict(mi.fetch_substack(env, None, TODAY, TODAY, call=call))
        self.assertEqual(out["substack_publication_daily"][0]["publication"], "https://x.substack.com")
        self.assertEqual(out["substack_post_stats"][0]["post_id"], 7)
        self.assertEqual(set(calls), {"get_publication_stats", "get_post_stats"})
        self.assertTrue(set(calls) <= mi.substack_bridge.READ_TOOLS)
        with self.assertRaises(mi.Skipped):
            mi.fetch_substack({}, None, TODAY, TODAY)

    def test_substack_aggregates_have_no_subscriber_level_columns(self):
        for table in ("substack_publication_daily", "substack_post_stats"):
            for banned in ("email", "name", "subscriber_id", "user_id"):
                self.assertNotIn(banned, mi.SPEC[table])


class ValidationTests(unittest.TestCase):
    S, E = date(2026, 10, 8), date(2026, 10, 14)

    def ga(self, **kw):
        row = {"date": date(2026, 10, 12), "property_id": "1", "page_path": "/a", "channel_group": "Direct",
               "sessions": 5, "total_users": 4, "screen_page_views": 7, "engaged_sessions": 3}
        return {**row, **kw}

    def check(self, **kw):
        return mi.validate_rows("ga4_daily", [self.ga(**kw)], self.S, self.E)

    def test_valid_row_passes(self):
        self.assertEqual(self.check()[0]["sessions"], 5)

    def test_rejects_bad_values(self):
        for kw in ({"sessions": -1}, {"sessions": 1.5}, {"sessions": "5x"}, {"sessions": True},
                   {"sessions": float("nan")}, {"date": date(2026, 9, 1)}, {"date": "2026-10-12"},
                   {"page_path": None}, {"page_path": "x" * 5000}):
            with self.assertRaises(mi.ValidationError, msg=str(kw)):
                self.check(**kw)

    def test_rejects_unknown_or_missing_columns(self):
        with self.assertRaises(mi.ValidationError):
            mi.validate_rows("ga4_daily", [{**self.ga(), "email": "a@b"}], self.S, self.E)
        row = self.ga()
        del row["sessions"]
        with self.assertRaises(mi.ValidationError):
            mi.validate_rows("ga4_daily", [row], self.S, self.E)

    def test_gsc_ctr_and_clicks_bounds(self):
        base = {"date": date(2026, 10, 12), "site_url": "s", "page": "p", "query": "q",
                "clicks": 2, "impressions": 9, "ctr": 0.2, "position": 3.0}
        mi.validate_rows("gsc_daily", [base], self.S, self.E)
        for kw in ({"ctr": 1.5}, {"clicks": 20}):
            with self.assertRaises(mi.ValidationError):
                mi.validate_rows("gsc_daily", [{**base, **kw}], self.S, self.E)

    def test_snapshot_date_must_equal_window_end(self):
        row = {"snapshot_date": self.S, "publication": "p", "subscribers": 1, "subscribers_last_30_days": 0,
               "email_subscribers": 1, "app_subscribers": 0, "views": 1, "views_30d": 1, "open_rate_30d": 0.1}
        with self.assertRaises(mi.ValidationError):
            mi.validate_rows("substack_publication_daily", [row], self.S, self.E)

    def test_one_bad_row_writes_nothing_and_run_source_marks_failed(self):
        class Conn:
            def __init__(self):
                self.rolled = self.executed = False
                self.log = []

            def cursor(self):
                outer = self

                class Cur:
                    def __enter__(s): return s
                    def __exit__(s, *a): return False
                    def executemany(s, *a): outer.log.append("write")
                    def execute(s, sql, *a): outer.log.append("run")
                return Cur()

            def commit(self): pass
            def rollback(self): self.rolled = True

        bad = {**self.ga(), "sessions": -5}
        mi.FETCHERS["_t"] = lambda env, http, s, e: ("ga4_daily", [self.ga(), bad])
        try:
            c = Conn()
            res = mi.run_source(c, "_t", {}, None, self.S, self.E)
        finally:
            del mi.FETCHERS["_t"]
        self.assertEqual(res["status"], "failed")
        self.assertIn("ValidationError", res["error"])
        self.assertNotIn("write", c.log)

    def test_auth_failure_recorded_as_auth_failed(self):
        class C:
            def cursor(s):
                class Cur:
                    def __enter__(x): return x
                    def __exit__(x, *a): return False
                    def execute(x, sql, params=None): C.params = params
                return Cur()
            def commit(s): pass
            def rollback(s): pass

        def boom(*a):
            raise mi.substack_bridge.AuthFailed("cookie rejected")

        mi.FETCHERS["_t"] = boom
        try:
            res = mi.run_source(C(), "_t", {}, None, self.S, self.E)
        finally:
            del mi.FETCHERS["_t"]
        self.assertEqual(res["status"], "auth_failed")

    def test_run_rejects_bad_days_and_sources(self):
        with self.assertRaises(ValueError):
            mi.run(None, {}, days=0)
        with self.assertRaises(ValueError):
            mi.run(None, {}, days=7, sources=("nope",))

    def test_missing_credentials_skip(self):
        with self.assertRaises(mi.Skipped):
            mi.fetch_ga4({}, FakeHttp({}), TODAY, TODAY)
        with self.assertRaises(mi.Skipped):
            mi.fetch_reddit({}, FakeHttp({}), TODAY, TODAY)


@unittest.skipUnless(shutil.which("docker"), "docker required")
class DbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "mkt-ingest-test-" + uuid.uuid4().hex[:8]
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", cls.name, "-p", "127.0.0.1::5432",
                            "-e", "POSTGRES_PASSWORD=pw", "postgres:16-alpine"], capture_output=True, text=True)
        if r.returncode:
            raise unittest.SkipTest("no scratch postgres")
        port = subprocess.run(["docker", "port", cls.name, "5432/tcp"], capture_output=True,
                              text=True).stdout.split(":")[-1].strip()
        cls.dsn = f"postgresql://postgres:pw@127.0.0.1:{port}/postgres"
        for _ in range(60):
            try:
                with psycopg.connect(cls.dsn, connect_timeout=2) as c:
                    c.execute("select 1")
                time.sleep(2)
                with psycopg.connect(cls.dsn, connect_timeout=2) as c:
                    c.execute("select 1")
                break
            except Exception:
                time.sleep(1)
        else:
            cls.tearDownClass()
            raise unittest.SkipTest("postgres never ready")
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute(open(os.path.join(REPO, "sql", "marketing_schema.sql")).read())

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)

    def conn(self):
        return psycopg.connect(self.dsn)

    def q(self, sql):
        with self.conn() as c:
            return c.execute(sql).fetchall()

    def test_isolation_skip_unavailable_and_upsert_idempotent(self):
        env = {**GOOGLE, "GA4_PROPERTY_ID": "1", "GSC_SITE_URL": "sc-domain:s"}
        ga = Resp({"rowCount": 1, "rows": [{"dimensionValues": [{"value": "20261012"}, {"value": "/a"}, {"value": "Direct"}],
                                            "metricValues": [{"value": "5"}, {"value": "4"}, {"value": "7"}, {"value": "3"}]}]})
        routes = {"oauth2": TOKEN, "runReport": ga, "searchAnalytics": RuntimeError("boom")}
        with self.conn() as c:
            first = mi.run(c, env, FakeHttp(routes), today=TODAY)
        self.assertEqual(first["ga4"]["status"], "ok")          # gsc failure did not stop ga4
        self.assertEqual(first["gsc"]["status"], "failed")
        self.assertIn("boom", first["gsc"]["error"])
        self.assertEqual(first["reddit"]["status"], "skipped")
        self.assertEqual(first["substack"]["status"], "skipped")
        # second run, changed metric: same key updated, not duplicated
        ga2 = Resp({"rowCount": 1, "rows": [{"dimensionValues": [{"value": "20261012"}, {"value": "/a"}, {"value": "Direct"}],
                                             "metricValues": [{"value": "9"}, {"value": "4"}, {"value": "7"}, {"value": "3"}]}]})
        with self.conn() as c:
            mi.run(c, env, FakeHttp({**routes, "runReport": ga2}), today=TODAY)
        self.assertEqual(self.q("select count(*), max(sessions) from marketing.ga4_daily"), [(1, 9)])
        statuses = dict(self.q("select source, status from marketing.ingest_runs order by id desc limit 4"))
        self.assertEqual(statuses, {"ga4": "ok", "gsc": "failed", "reddit": "skipped", "substack": "skipped"})
        self.assertEqual(self.q("select count(*) from marketing.ingest_runs where source='ga4'"), [(2,)])

    def test_no_pii_columns(self):
        cols = {r[0] for r in self.q("select column_name from information_schema.columns where table_schema='marketing'")}
        for banned in ("email", "user_id", "ip", "ip_address", "client_id", "username", "user_pseudo_id"):
            self.assertNotIn(banned, cols)


if __name__ == "__main__":
    unittest.main()
