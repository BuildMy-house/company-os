"""Deterministic marketing-analytics ingester.

Pulls aggregate data from GA4, Search Console, Reddit and the public Substack
RSS feed and upserts it into the ``marketing`` schema (sql/marketing_schema.sql).
No LLM is involved. Run daily by k8s/base/marketing-ingest.

Rules:
- Each source is independent: a failure is recorded in ``marketing.ingest_runs``
  and never stops the others.
- A source without credentials is recorded as ``skipped``.
- Substack has no official stats API; only the public RSS post inventory is
  read. Without a publication URL the source is ``unavailable`` (not faked).
- Upserts are idempotent: re-running a window overwrites the same keys.
- Aggregates only. Nothing here reads or stores user-level data.
- Credentials come only from env (Secret ``marketing-ingest-secrets``); this
  module never logs or stores them.

Run: ``python -m company_ops.marketing_ingest [--days N]``.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote

import psycopg
import requests

SOURCES = ("ga4", "gsc", "reddit", "substack")
HTTP_TIMEOUT = 30
USER_AGENT = "buildmy-house-marketing-ingest/1.0 (read-only analytics)"

GA4_DIMENSIONS = ["date", "pagePath", "sessionDefaultChannelGroup"]
GA4_METRICS = ["sessions", "totalUsers", "screenPageViews", "engagedSessions"]

GOOGLE_ENV = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
REDDIT_ENV = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD")


class Skipped(Exception):
    """Source not configured (missing credential env)."""


class Unavailable(Exception):
    """No real API route exists for this source."""


def _missing(env: dict, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if not env.get(n)]


def _need(env: dict, names: tuple[str, ...]) -> None:
    missing = _missing(env, names)
    if missing:
        raise Skipped("missing env: " + ", ".join(missing))


def _check(resp: requests.Response, what: str) -> dict:
    if resp.status_code >= 400:
        # Status and short body only; request headers (credentials) never included.
        raise RuntimeError(f"{what}: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# ----------------------------------------------------------------- fetchers
# Each returns (table, rows) with rows as dicts keyed by column name.


def google_access_token(env: dict, http) -> str:
    resp = http.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": env["GOOGLE_CLIENT_ID"],
            "client_secret": env["GOOGLE_CLIENT_SECRET"],
            "refresh_token": env["GOOGLE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
        timeout=HTTP_TIMEOUT,
    )
    return _check(resp, "google token")["access_token"]


def fetch_ga4(env: dict, http, start: date, end: date) -> tuple[str, list[dict]]:
    _need(env, GOOGLE_ENV + ("GA4_PROPERTY_ID",))
    prop = env["GA4_PROPERTY_ID"]
    token = google_access_token(env, http)
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport"
    rows: list[dict] = []
    offset = 0
    while True:
        body = {
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": n} for n in GA4_DIMENSIONS],
            "metrics": [{"name": n} for n in GA4_METRICS],
            "limit": 10000,
            "offset": offset,
        }
        data = _check(
            http.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=HTTP_TIMEOUT),
            "ga4 runReport",
        )
        page = data.get("rows", [])
        for r in page:
            d = [x["value"] for x in r["dimensionValues"]]
            m = [int(float(x["value"])) for x in r["metricValues"]]
            rows.append({
                "date": datetime.strptime(d[0], "%Y%m%d").date(),
                "property_id": prop,
                "page_path": d[1],
                "channel_group": d[2],
                "sessions": m[0],
                "total_users": m[1],
                "screen_page_views": m[2],
                "engaged_sessions": m[3],
            })
        offset += len(page)
        if not page or offset >= int(data.get("rowCount", 0)):
            break
    return "ga4_daily", rows


def fetch_gsc(env: dict, http, start: date, end: date) -> tuple[str, list[dict]]:
    _need(env, GOOGLE_ENV + ("GSC_SITE_URL",))
    site = env["GSC_SITE_URL"]
    token = google_access_token(env, http)
    url = f"https://www.googleapis.com/webmasters/v3/sites/{quote(site, safe='')}/searchAnalytics/query"
    rows: list[dict] = []
    start_row = 0
    while True:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["date", "page", "query"],
            "rowLimit": 25000,
            "startRow": start_row,
        }
        data = _check(
            http.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=HTTP_TIMEOUT),
            "gsc searchAnalytics",
        )
        page = data.get("rows", [])
        for r in page:
            k = r["keys"]
            rows.append({
                "date": date.fromisoformat(k[0]),
                "site_url": site,
                "page": k[1],
                "query": k[2],
                "clicks": int(r.get("clicks", 0)),
                "impressions": int(r.get("impressions", 0)),
                "ctr": float(r.get("ctr", 0)),
                "position": float(r.get("position", 0)),
            })
        start_row += len(page)
        if len(page) < 25000:
            break
    return "gsc_daily", rows


def fetch_reddit(env: dict, http, start: date, end: date) -> tuple[str, list[dict]]:
    """Snapshot score/comments of our own account's recent submissions.

    Uses the official OAuth2 API (script-app password grant, read-only calls).
    Whether the account's app registration is approved under Reddit's current
    API terms cannot be verified from code.
    """
    _need(env, REDDIT_ENV)
    headers = {"User-Agent": USER_AGENT}
    tok = _check(
        http.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(env["REDDIT_CLIENT_ID"], env["REDDIT_CLIENT_SECRET"]),
            data={"grant_type": "password", "username": env["REDDIT_USERNAME"], "password": env["REDDIT_PASSWORD"]},
            headers=headers,
            timeout=HTTP_TIMEOUT,
        ),
        "reddit token",
    )
    if "access_token" not in tok:
        raise RuntimeError("reddit token: no access_token in response")
    headers["Authorization"] = f"bearer {tok['access_token']}"
    data = _check(
        http.get(
            f"https://oauth.reddit.com/user/{quote(env['REDDIT_USERNAME'])}/submitted",
            params={"limit": 100, "raw_json": 1},
            headers=headers,
            timeout=HTTP_TIMEOUT,
        ),
        "reddit submitted",
    )
    rows = []
    for child in data.get("data", {}).get("children", []):
        p = child["data"]
        rows.append({
            "snapshot_date": end,
            "subreddit": p["subreddit"],
            "post_id": p["id"],
            "title": p["title"],
            "permalink": "https://www.reddit.com" + p["permalink"],
            "created_at": datetime.fromtimestamp(float(p["created_utc"]), UTC),
            "score": int(p.get("score", 0)),
            "num_comments": int(p.get("num_comments", 0)),
            "upvote_ratio": p.get("upvote_ratio"),
        })
    return "reddit_posts_daily", rows


def fetch_substack(env: dict, http, start: date, end: date) -> tuple[str, list[dict]]:
    """Post inventory from the public RSS feed. Stats: unavailable (no API)."""
    base = env.get("SUBSTACK_PUBLICATION_URL", "").rstrip("/")
    if not base:
        raise Unavailable("no SUBSTACK_PUBLICATION_URL; Substack has no official stats API")
    resp = http.get(base + "/feed", headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"substack feed: HTTP {resp.status_code}")
    root = ET.fromstring(resp.content)
    rows = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        pub = item.findtext("pubDate")
        rows.append({
            "snapshot_date": end,
            "publication": base,
            "post_url": link,
            "title": (item.findtext("title") or "").strip(),
            "published_at": parsedate_to_datetime(pub) if pub else None,
        })
    return "substack_posts", rows


FETCHERS: dict[str, Callable] = {
    "ga4": fetch_ga4,
    "gsc": fetch_gsc,
    "reddit": fetch_reddit,
    "substack": fetch_substack,
}

# table -> primary-key columns (must match sql/marketing_schema.sql)
KEYS = {
    "ga4_daily": ("date", "property_id", "page_path", "channel_group"),
    "gsc_daily": ("date", "site_url", "page", "query"),
    "reddit_posts_daily": ("snapshot_date", "subreddit", "post_id"),
    "substack_posts": ("snapshot_date", "publication", "post_url"),
}


# ------------------------------------------------------------------- store


def upsert(conn, table: str, rows: list[dict]) -> int:
    """Idempotent upsert keyed on the table's primary key; stamps fetched_at."""
    if table not in KEYS:
        raise ValueError(f"unknown table {table!r}")
    if not rows:
        return 0
    keys = KEYS[table]
    cols = list(rows[0].keys())
    updates = [c for c in cols if c not in keys]
    stmt = (
        f"INSERT INTO marketing.{table} ({', '.join(cols)}, fetched_at) "
        f"VALUES ({', '.join(['%s'] * len(cols))}, now()) "
        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in updates + ["fetched_at"])
    )
    with conn.cursor() as cur:
        cur.executemany(stmt, [[r[c] for c in cols] for r in rows])
    return len(rows)


def record_run(conn, source, start, end, status, rows=0, error=None, started_at=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO marketing.ingest_runs "
            "(source, window_start, window_end, rows_upserted, status, error, started_at, finished_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,now())",
            (source, start, end, rows, status, (error or None) and error[:500], started_at or datetime.now(UTC)),
        )
    conn.commit()


def run_source(conn, name: str, env: dict, http, start: date, end: date) -> dict:
    started = datetime.now(UTC)
    try:
        table, rows = FETCHERS[name](env, http, start, end)
        n = upsert(conn, table, rows)
        conn.commit()
        result = {"status": "ok", "rows": n, "error": None}
    except Skipped as e:
        conn.rollback()
        result = {"status": "skipped", "rows": 0, "error": str(e)}
    except Unavailable as e:
        conn.rollback()
        result = {"status": "unavailable", "rows": 0, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - isolation: one source must not stop the others
        conn.rollback()
        result = {"status": "failed", "rows": 0, "error": f"{type(e).__name__}: {e}"}
    if name == "substack" and result["status"] == "ok":
        result["error"] = "inventory only; views/subscribers unavailable (no official API)"
    record_run(conn, name, start, end, result["status"], result["rows"], result["error"], started)
    return result


def run(conn, env: dict, http=None, days: int = 7, today: date | None = None) -> dict:
    http = http or requests.Session()
    end = (today or datetime.now(UTC).date()) - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return {name: run_source(conn, name, env, http, start, end) for name in SOURCES}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Ingest marketing analytics into the marketing schema.")
    ap.add_argument("--days", type=int, default=int(os.environ.get("MARKETING_INGEST_DAYS", "7")))
    args = ap.parse_args(argv)
    dsn = os.environ.get("MARKETING_INGEST_DATABASE_URL")
    if not dsn:
        print("MARKETING_INGEST_DATABASE_URL not set", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn:
        results = run(conn, dict(os.environ), days=args.days)
    print(json.dumps(results))
    # Non-zero only for real failures so the CronJob shows red; skipped and
    # unavailable are expected states.
    return 1 if any(r["status"] == "failed" for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
