"""Deterministic marketing-analytics ingester.

Pulls aggregate data from GA4, Search Console, Reddit and Substack (via the
pinned, allowlisted substack-mcp bridge) and upserts it into the ``marketing`` schema (sql/marketing_schema.sql).
No LLM is involved. Run daily by k8s/base/marketing-ingest.

Rules:
- Each source is independent: a failure is recorded in ``marketing.ingest_runs``
  and never stops the others.
- A source without credentials is recorded as ``skipped``.
- Substack has no official API: stats come from the unofficial cookie-based
  substack-mcp via company_ops/substack_bridge.py (read tools only, aggregates
  only). Without SUBSTACK_* env it is ``skipped``; an expired cookie is
  recorded as ``auth_failed``.
- Every row is validated (types, ranges, date windows) before upsert; one bad
  row fails the whole source, so a model or a bad payload cannot write
  invented numbers.
- Upserts are idempotent: re-running a window overwrites the same keys.
- Aggregates only. Nothing here reads or stores user-level data.
- Credentials come only from env (Secret ``marketing-ingest-secrets``); this
  module never logs or stores them.

Run: ``python -m company_ops.marketing_ingest [--days N] [--source NAME]``.
The same ``run`` code path backs the marketing-lead's ``marketing-ingest`` tool
(company_ops/marketing_sidecar.py).
"""

from __future__ import annotations

import os
import sys
import math
from datetime import UTC, date, datetime, timedelta
from typing import Callable
from urllib.parse import quote

import psycopg
import requests

from company_ops import substack_bridge

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


def fetch_substack(env: dict, http, start: date, end: date, call=None):
    """Aggregate publication and per-post stats via the allowlisted bridge.

    ``call(tool, args)`` is injectable for tests. Only get_publication_stats,
    get_post_stats and list_posts are reachable; no subscriber-level tool is.
    """
    _need(env, substack_bridge.SUBSTACK_ENV)
    pub = env["SUBSTACK_PUBLICATION_URL"].rstrip("/")
    call = call or (lambda tool, args: substack_bridge.call_tool(tool, args, env))
    stats = call("get_publication_stats", {})
    pub_row = {
        "snapshot_date": end,
        "publication": pub,
        "subscribers": stats.get("subscribers"),
        "subscribers_last_30_days": stats.get("subscribers_last_30_days"),
        "email_subscribers": stats.get("email_subscribers"),
        "app_subscribers": stats.get("app_subscribers"),
        "views": stats.get("views"),
        "views_30d": stats.get("views_30d"),
        "open_rate_30d": stats.get("open_rate_30d"),
    }
    post_rows: list[dict] = []
    offset = 0
    while offset < 500:  # hard bound on archive pages
        page = call("get_post_stats", {"limit": 100, "offset": offset})
        posts = page.get("posts", [])
        for p in posts:
            post_rows.append({
                "snapshot_date": end,
                "publication": pub,
                "post_id": p.get("post_id"),
                "title": p.get("title") or "",
                "post_date": p.get("post_date"),
                "delivered": p.get("delivered"),
                "opens": p.get("opens"),
                "open_rate": p.get("open_rate"),
                "clicks": p.get("clicks"),
                "views": p.get("views"),
                "signups": p.get("signups"),
                "subscribes": p.get("subscribes"),
                "unsubscribes": p.get("unsubscribes"),
                "likes": p.get("likes"),
            })
        offset += len(posts)
        if len(posts) < 100 or offset >= int(page.get("total") or 0):
            break
    return [("substack_publication_daily", [pub_row]), ("substack_post_stats", post_rows)]


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
    "substack_publication_daily": ("snapshot_date", "publication"),
    "substack_post_stats": ("snapshot_date", "publication", "post_id"),
}

# ---------------------------------------------------------------- validation
# column -> (kind, min, max). kind: int | float | str | date | ts. NULL allowed
# only for columns listed in NULLABLE. Anything else raises ValidationError.

BIG = 10**12
SPEC: dict[str, dict[str, tuple]] = {
    "ga4_daily": {
        "date": ("date",), "property_id": ("str",), "page_path": ("str",), "channel_group": ("str",),
        "sessions": ("int", 0, BIG), "total_users": ("int", 0, BIG),
        "screen_page_views": ("int", 0, BIG), "engaged_sessions": ("int", 0, BIG),
    },
    "gsc_daily": {
        "date": ("date",), "site_url": ("str",), "page": ("str",), "query": ("str",),
        "clicks": ("int", 0, BIG), "impressions": ("int", 0, BIG),
        "ctr": ("float", 0, 1), "position": ("float", 0, 10000),
    },
    "reddit_posts_daily": {
        "snapshot_date": ("date",), "subreddit": ("str",), "post_id": ("str",), "title": ("str",),
        "permalink": ("str",), "created_at": ("ts",), "score": ("int", -BIG, BIG),
        "num_comments": ("int", 0, BIG), "upvote_ratio": ("float", 0, 1),
    },
    "substack_publication_daily": {
        "snapshot_date": ("date",), "publication": ("str",),
        "subscribers": ("int", 0, BIG), "subscribers_last_30_days": ("int", -BIG, BIG),
        "email_subscribers": ("int", 0, BIG), "app_subscribers": ("int", 0, BIG),
        "views": ("int", 0, BIG), "views_30d": ("int", 0, BIG), "open_rate_30d": ("float", 0, 1),
    },
    "substack_post_stats": {
        "snapshot_date": ("date",), "publication": ("str",), "post_id": ("int", 1, BIG),
        "title": ("str",), "post_date": ("ts",), "delivered": ("int", 0, BIG),
        "opens": ("int", 0, BIG), "open_rate": ("float", 0, 1), "clicks": ("int", 0, BIG),
        "views": ("int", 0, BIG), "signups": ("int", 0, BIG), "subscribes": ("int", 0, BIG),
        "unsubscribes": ("int", 0, BIG), "likes": ("int", 0, BIG),
    },
}
NULLABLE = {
    "reddit_posts_daily": {"upvote_ratio"},
    "substack_publication_daily": {
        "subscribers", "subscribers_last_30_days", "email_subscribers", "app_subscribers",
        "views", "views_30d", "open_rate_30d"},
    "substack_post_stats": {
        "post_date", "delivered", "opens", "open_rate", "clicks", "views", "signups",
        "subscribes", "unsubscribes", "likes"},
}
# date columns that must fall inside the pulled window (snapshots are stamped
# with the window end, so they must equal it; event dates must be in-window).
WINDOW_DATE_COLS = {"ga4_daily": "date", "gsc_daily": "date"}
SNAPSHOT_TABLES = {"reddit_posts_daily", "substack_publication_daily", "substack_post_stats"}


class ValidationError(Exception):
    """A fetched row failed validation; nothing from this source is written."""


def _parse_ts(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            pass
    raise ValueError("not a timestamp")


def validate_rows(table: str, rows: list[dict], start: date, end: date) -> list[dict]:
    spec = SPEC[table]
    nullable = NULLABLE.get(table, set())
    out = []
    for n, row in enumerate(rows):
        if set(row) != set(spec):
            raise ValidationError(f"{table}[{n}]: columns {sorted(set(row) ^ set(spec))} differ from schema")
        clean = {}
        for col, rule in spec.items():
            v = row[col]
            if v is None:
                if col not in nullable:
                    raise ValidationError(f"{table}[{n}].{col}: null not allowed")
                clean[col] = None
                continue
            kind = rule[0]
            try:
                if kind == "int":
                    if isinstance(v, bool) or isinstance(v, float) and not float(v).is_integer():
                        raise ValueError("not an integer")
                    v = int(v)
                elif kind == "float":
                    if isinstance(v, bool):
                        raise ValueError("bool")
                    v = float(v)
                    if not math.isfinite(v):
                        raise ValueError("not finite")
                elif kind == "str":
                    if not isinstance(v, str) or len(v) > 4000:
                        raise ValueError("not a string <= 4000 chars")
                elif kind == "date":
                    if not isinstance(v, date) or isinstance(v, datetime):
                        raise ValueError("not a date")
                elif kind == "ts":
                    v = _parse_ts(v)
                if kind in ("int", "float") and not rule[1] <= v <= rule[2]:
                    raise ValueError(f"out of range [{rule[1]}, {rule[2]}]")
            except ValueError as e:
                raise ValidationError(f"{table}[{n}].{col}: {e}") from e
            clean[col] = v
        wc = WINDOW_DATE_COLS.get(table)
        if wc and not start <= clean[wc] <= end:
            raise ValidationError(f"{table}[{n}].{wc}: {clean[wc]} outside window {start}..{end}")
        if table in SNAPSHOT_TABLES and clean["snapshot_date"] != end:
            raise ValidationError(f"{table}[{n}].snapshot_date must equal window end {end}")
        if table == "gsc_daily" and clean["clicks"] > clean["impressions"]:
            raise ValidationError(f"{table}[{n}]: clicks exceed impressions")
        out.append(clean)
    return out


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
        fetched = FETCHERS[name](env, http, start, end)
        if isinstance(fetched, tuple):
            fetched = [fetched]
        n = 0
        for table, rows in fetched:
            n += upsert(conn, table, validate_rows(table, rows, start, end))
        conn.commit()
        result = {"status": "ok", "rows": n, "error": None}
    except Skipped as e:
        conn.rollback()
        result = {"status": "skipped", "rows": 0, "error": str(e)}
    except Unavailable as e:
        conn.rollback()
        result = {"status": "unavailable", "rows": 0, "error": str(e)}
    except substack_bridge.AuthFailed as e:
        conn.rollback()
        result = {"status": "auth_failed", "rows": 0, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - isolation: one source must not stop the others
        conn.rollback()
        result = {"status": "failed", "rows": 0, "error": f"{type(e).__name__}: {e}"}
    record_run(conn, name, start, end, result["status"], result["rows"], result["error"], started)
    return result


def run(conn, env: dict, http=None, days: int = 7, today: date | None = None,
        sources: tuple[str, ...] | None = None) -> dict:
    """Pull the named sources (default all). Shared by the CronJob and the sidecar."""
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    names = tuple(sources or SOURCES)
    unknown = [n for n in names if n not in FETCHERS]
    if unknown:
        raise ValueError(f"unknown source(s): {unknown}")
    http = http or requests.Session()
    end = (today or datetime.now(UTC).date()) - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return {name: run_source(conn, name, env, http, start, end) for name in names}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Ingest marketing analytics into the marketing schema.")
    ap.add_argument("--days", type=int, default=int(os.environ.get("MARKETING_INGEST_DAYS", "7")))
    ap.add_argument("--source", action="append", choices=SOURCES, help="repeatable; default all")
    args = ap.parse_args(argv)
    dsn = os.environ.get("MARKETING_INGEST_DATABASE_URL")
    if not dsn:
        print("MARKETING_INGEST_DATABASE_URL not set", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn:
        results = run(conn, dict(os.environ), days=args.days, sources=tuple(args.source or ()) or None)
    print(json.dumps(results))
    # Non-zero for real failures and expired cookies so the CronJob shows red;
    # skipped and unavailable are expected states.
    return 1 if any(r["status"] in ("failed", "auth_failed") for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
