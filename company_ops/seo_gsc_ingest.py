"""Google Search Console ingestion into ``marketing.gsc_search_analytics``.

Pulls the full searchAnalytics breakdown (date, query, page, country, device)
for one property using a **service account** (RS256 JWT, webmasters.readonly
only) and upserts it idempotently. This is the 16-month-capable job; the
OAuth-refresh-token fetcher in ``company_ops/marketing_ingest.py`` remains the
narrower daily path (see docs/SEO-GSC-INGEST.md).

- incremental (default): re-pulls the last ``GSC_DAYS`` (default 7) days ending
  at ``today(UTC) - GSC_LAG_DAYS`` (default 3, GSC's publication lag), so late
  data revisions overwrite the same primary-key rows.
- ``--backfill``: pulls the last 16 months (GSC retention) in contiguous
  30-day windows.

Runs are recorded in ``marketing.ingest_runs`` (source ``gsc``) via
``marketing_ingest.record_run``. Credentials come only from
``GSC_SERVICE_ACCOUNT_JSON``; the key, assertion and token are never logged.

Run: ``python -m company_ops.seo_gsc_ingest [--backfill] [--days N] [--dry-run]``.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import psycopg
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from company_ops.marketing_ingest import record_run

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
API_URL = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
DEFAULT_SITE_URL = "sc-domain:buildmy.house"
DEFAULT_LAG_DAYS = 3
DEFAULT_DAYS = 7
BACKFILL_MONTHS = 16
BACKFILL_CHUNK_DAYS = 30
ROW_LIMIT = 25000
DIMENSIONS = ["date", "query", "page", "country", "device"]
MAX_RETRIES = 4
HTTP_TIMEOUT = 30
BATCH_SIZE = 1000

COLUMNS = (
    "site_url", "date", "query", "page", "country", "device",
    "clicks", "impressions", "ctr", "position",
)

UPSERT_SQL = """
INSERT INTO marketing.gsc_search_analytics
  (site_url, date, query, page, country, device,
   clicks, impressions, ctr, position, fetched_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (site_url, date, query, page, country, device) DO UPDATE SET
  clicks = EXCLUDED.clicks,
  impressions = EXCLUDED.impressions,
  ctr = EXCLUDED.ctr,
  position = EXCLUDED.position,
  fetched_at = now()
"""


class GSCError(RuntimeError):
    """Search Console call failed. Message is always safe to log."""


class GSCAuthError(GSCError):
    """Credentials rejected (401/403 or token exchange failure)."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _service_account(raw: str | None = None) -> dict:
    raw = raw if raw is not None else os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise ValueError(
            "GSC_SERVICE_ACCOUNT_JSON is not set — required for Search Console ingestion"
        )
    try:
        sa = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("GSC_SERVICE_ACCOUNT_JSON is not valid JSON") from None
    if not sa.get("client_email") or not sa.get("private_key"):
        raise ValueError("GSC_SERVICE_ACCOUNT_JSON lacks client_email or private_key")
    return sa


def build_jwt(sa: dict, now: datetime | None = None) -> str:
    """Build an RS256-signed assertion for the webmasters.readonly scope."""
    now = now or datetime.now(UTC)
    iat = int(now.timestamp())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": sa["client_email"],
        "scope": SCOPE,
        "aud": sa.get("token_uri") or TOKEN_URL,
        "iat": iat,
        "exp": iat + 3600,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


def _sleep_backoff(attempt: int) -> None:
    time.sleep(min(2**attempt, 16))


def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Issue one HTTP request, retrying 429/5xx/network errors with backoff."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
        except requests.RequestException:
            if attempt < MAX_RETRIES:
                _sleep_backoff(attempt)
                continue
            raise GSCError(f"request to {url} failed after {MAX_RETRIES + 1} attempts") from None
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < MAX_RETRIES:
                log.warning("gsc: HTTP %s from %s, retrying", resp.status_code, url)
                _sleep_backoff(attempt)
                continue
            raise GSCError(f"{url}: HTTP {resp.status_code} after {MAX_RETRIES + 1} attempts")
        if resp.status_code in (401, 403):
            raise GSCAuthError(
                f"{url}: HTTP {resp.status_code} — credentials rejected or permission denied"
            )
        if resp.status_code >= 400:
            raise GSCError(f"{url}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp
    raise GSCError(f"request to {url} failed")


def get_access_token(sa: dict, session: requests.Session | None = None) -> str:
    session = session or requests.Session()
    assertion = build_jwt(sa)
    resp = _request(
        session,
        "POST",
        TOKEN_URL,
        data={"grant_type": JWT_BEARER_GRANT, "assertion": assertion},
    )
    try:
        payload = resp.json()
    except ValueError:
        raise GSCAuthError(f"token endpoint returned non-JSON (HTTP {resp.status_code})") from None
    token = payload.get("access_token")
    if not token:
        raise GSCAuthError("token endpoint response had no access_token")
    return token


def query_window(
    site_url: str,
    token: str,
    start: date,
    end: date,
    session: requests.Session | None = None,
) -> list[dict]:
    """Fetch every row for [start, end], paginating until a short page."""
    session = session or requests.Session()
    url = API_URL.format(site=quote(site_url, safe=""))
    headers = {"Authorization": f"Bearer {token}"}
    rows: list[dict] = []
    start_row = 0
    while True:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": DIMENSIONS,
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "dataState": "final",
        }
        resp = _request(session, "POST", url, json=body, headers=headers)
        try:
            payload = resp.json()
        except ValueError:
            raise GSCError("searchAnalytics returned non-JSON") from None
        page = payload.get("rows") or []
        for r in page:
            keys = r.get("keys") or []
            if len(keys) != len(DIMENSIONS):
                raise GSCError("searchAnalytics row has unexpected dimension count")
            rows.append(
                {
                    "date": date.fromisoformat(keys[0]),
                    "query": keys[1],
                    "page": keys[2],
                    "country": keys[3],
                    "device": keys[4],
                    "clicks": int(r.get("clicks", 0)),
                    "impressions": int(r.get("impressions", 0)),
                    "ctr": float(r.get("ctr", 0)),
                    "position": float(r.get("position", 0)),
                }
            )
        if len(page) < ROW_LIMIT:
            break
        start_row += len(page)
    return rows


def _subtract_months(d: date, months: int) -> date:
    month_index = d.year * 12 + (d.month - 1) - months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    return date(year, month, min(d.day, _days_in_month(year, month)))


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def window(
    today: date, lag_days: int = DEFAULT_LAG_DAYS, days: int = DEFAULT_DAYS
) -> tuple[date, date]:
    """Inclusive (start, end) for incremental mode: last `days` days, lagged."""
    if days < 1:
        raise ValueError("days must be >= 1")
    end = today - timedelta(days=lag_days)
    return end - timedelta(days=days - 1), end


def backfill_windows(
    today: date,
    lag_days: int = DEFAULT_LAG_DAYS,
    months: int = BACKFILL_MONTHS,
    chunk_days: int = BACKFILL_CHUNK_DAYS,
) -> list[tuple[date, date]]:
    """Contiguous chunks covering the last `months` months ending at the lagged day."""
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")
    end = today - timedelta(days=lag_days)
    start = _subtract_months(end, months) + timedelta(days=1)
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        windows.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return windows


def write_rows(conn: psycopg.Connection, site_url: str, rows: list[dict]) -> int:
    """Upsert rows in batches. Idempotent on the composite primary key."""
    if not rows:
        return 0
    values = [
        (
            site_url,
            r["date"], r["query"], r["page"], r["country"], r["device"],
            r["clicks"], r["impressions"], r["ctr"], r["position"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        for i in range(0, len(values), BATCH_SIZE):
            cur.executemany(UPSERT_SQL, values[i : i + BATCH_SIZE])
    return len(values)


def ingest(
    *,
    backfill: bool = False,
    days: int = DEFAULT_DAYS,
    lag_days: int = DEFAULT_LAG_DAYS,
    dry_run: bool = False,
    today: date | None = None,
    site_url: str | None = None,
    dsn: str | None = None,
    session: requests.Session | None = None,
    sa: dict | None = None,
) -> dict:
    """Run one ingestion pass. Returns a summary dict; raises on failure."""
    today = today or datetime.now(UTC).date()
    site_url = site_url or os.environ.get("GSC_SITE_URL") or DEFAULT_SITE_URL
    sa = sa or _service_account()
    session = session or requests.Session()

    if backfill:
        windows = backfill_windows(today, lag_days)
        mode = "backfill"
    else:
        windows = [window(today, lag_days, days)]
        mode = "incremental"

    conn: psycopg.Connection | None = None
    if not dry_run:
        dsn = dsn or os.environ.get("MARKETING_INGEST_DATABASE_URL")
        if not dsn:
            raise ValueError(
                "MARKETING_INGEST_DATABASE_URL is not set — required to store Search Console data"
            )
        conn = psycopg.connect(dsn)
        conn.execute("SET search_path TO marketing, public")

    started = datetime.now(UTC)
    rows_fetched = 0
    status = "ok"
    error: str | None = None
    try:
        token = get_access_token(sa, session)
        for start, end in windows:
            rows = query_window(site_url, token, start, end, session)
            rows_fetched += len(rows)
            if conn is not None:
                write_rows(conn, site_url, rows)
            log.info(
                "gsc: %s..%s fetched %d rows%s",
                start, end, len(rows), " (dry-run)" if dry_run else "",
            )
        if conn is not None:
            conn.commit()
    except GSCAuthError as exc:
        status, error = "auth_failed", str(exc)
        if conn is not None:
            conn.rollback()
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        if conn is not None:
            conn.rollback()
    finally:
        if conn is not None:
            record_run(
                conn, "gsc", windows[0][0], windows[-1][1], status,
                rows_fetched, error, started,
            )
            conn.close()

    summary = {
        "mode": mode,
        "site_url": site_url,
        "start_date": windows[0][0].isoformat(),
        "end_date": windows[-1][1].isoformat(),
        "windows": len(windows),
        "rows_fetched": rows_fetched,
        "status": "dry-run" if dry_run else status,
        "dry_run": dry_run,
    }
    if error:
        summary["error"] = error
    if status not in ("ok",):
        raise GSCError(error or status)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest Google Search Console search analytics into Postgres."
    )
    parser.add_argument(
        "--backfill", action="store_true", help="pull the last 16 months in 30-day windows"
    )
    parser.add_argument(
        "--days", type=int, default=int(os.environ.get("GSC_DAYS", DEFAULT_DAYS)),
        help="incremental window length in days (default 7)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and print counts, write nothing"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    lag_days = int(os.environ.get("GSC_LAG_DAYS", DEFAULT_LAG_DAYS))
    try:
        summary = ingest(
            backfill=args.backfill, days=args.days, lag_days=lag_days, dry_run=args.dry_run
        )
    except Exception as exc:
        print(f"seo_gsc_ingest failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
