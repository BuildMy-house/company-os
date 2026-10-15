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

    def test_substack_rss_inventory_and_unavailable(self):
        http = FakeHttp({"/feed": Resp(content=FEED)})
        _, rows = mi.fetch_substack({"SUBSTACK_PUBLICATION_URL": "https://x.substack.com"}, http, TODAY, TODAY)
        self.assertEqual(rows[0]["title"], "Hello")
        with self.assertRaises(mi.Unavailable):
            mi.fetch_substack({}, http, TODAY, TODAY)

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
        self.assertEqual(first["substack"]["status"], "unavailable")
        # second run, changed metric: same key updated, not duplicated
        ga2 = Resp({"rowCount": 1, "rows": [{"dimensionValues": [{"value": "20261012"}, {"value": "/a"}, {"value": "Direct"}],
                                             "metricValues": [{"value": "9"}, {"value": "4"}, {"value": "7"}, {"value": "3"}]}]})
        with self.conn() as c:
            mi.run(c, env, FakeHttp({**routes, "runReport": ga2}), today=TODAY)
        self.assertEqual(self.q("select count(*), max(sessions) from marketing.ga4_daily"), [(1, 9)])
        statuses = dict(self.q("select source, status from marketing.ingest_runs order by id desc limit 4"))
        self.assertEqual(statuses, {"ga4": "ok", "gsc": "failed", "reddit": "skipped", "substack": "unavailable"})
        self.assertEqual(self.q("select count(*) from marketing.ingest_runs where source='ga4'"), [(2,)])

    def test_no_pii_columns(self):
        cols = {r[0] for r in self.q("select column_name from information_schema.columns where table_schema='marketing'")}
        for banned in ("email", "user_id", "ip", "ip_address", "client_id", "username", "user_pseudo_id"):
            self.assertNotIn(banned, cols)


if __name__ == "__main__":
    unittest.main()
