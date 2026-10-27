from __future__ import annotations

import base64
import json
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from company_ops import seo_gsc_ingest as gsc


def _key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key.public_key(), pem


def _service_account():
    _, pem = _key_pair()
    return {"client_email": "seo@example.iam.gserviceaccount.com", "private_key": pem}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records requests and replays queued responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


class FakeCursor:
    def __init__(self):
        self.executed = []

    def executemany(self, stmt, params):
        self.executed.append((stmt, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, *args, **kwargs):
        return None

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(gsc, "_sleep_backoff", lambda attempt: None)


def _decode(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_token_exchange_uses_rs256_and_readonly_scope():
    public_key, pem = _key_pair()
    sa = {"client_email": "seo@example.iam.gserviceaccount.com", "private_key": pem}
    session = FakeSession([FakeResponse(payload={"access_token": "tok"})])

    assert gsc.get_access_token(sa, session) == "tok"

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == gsc.TOKEN_URL
    assert call["data"]["grant_type"] == gsc.JWT_BEARER_GRANT
    header_b64, claims_b64, sig_b64 = call["data"]["assertion"].split(".")
    header = _decode(header_b64)
    claims = _decode(claims_b64)
    assert header["alg"] == "RS256"
    assert claims["scope"] == "https://www.googleapis.com/auth/webmasters.readonly"
    assert claims["iss"] == sa["client_email"]
    assert claims["aud"] == gsc.TOKEN_URL
    assert claims["exp"] > claims["iat"]
    public_key.verify(
        base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4)),
        f"{header_b64}.{claims_b64}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_query_paginates_until_short_page():
    rows_page_1 = [
        {"keys": ["2026-10-01", "q", "https://x/", "usa", "DESKTOP"], "clicks": 1,
         "impressions": 2, "ctr": 0.5, "position": 1.0}
    ] * gsc.ROW_LIMIT
    rows_page_2 = [
        {"keys": ["2026-10-02", "q2", "https://x/", "gbr", "MOBILE"], "clicks": 3,
         "impressions": 4, "ctr": 0.75, "position": 2.5}
    ]
    session = FakeSession([
        FakeResponse(payload={"rows": rows_page_1}),
        FakeResponse(payload={"rows": rows_page_2}),
    ])

    rows = gsc.query_window(
        "sc-domain:buildmy.house", "tok", date(2026, 10, 1), date(2026, 10, 3), session
    )

    assert len(rows) == gsc.ROW_LIMIT + 1
    assert len(session.calls) == 2
    first, second = session.calls
    assert first["json"]["startRow"] == 0
    assert second["json"]["startRow"] == gsc.ROW_LIMIT
    assert first["json"]["dimensions"] == ["date", "query", "page", "country", "device"]
    assert first["json"]["rowLimit"] == gsc.ROW_LIMIT
    assert first["json"]["dataState"] == "final"
    assert first["url"] == (
        "https://www.googleapis.com/webmasters/v3/sites/"
        "sc-domain%3Abuildmy.house/searchAnalytics/query"
    )
    assert rows[-1]["device"] == "MOBILE"
    assert rows[-1]["date"] == date(2026, 10, 2)


def test_empty_response_is_zero_rows_not_error():
    session = FakeSession([FakeResponse(payload={})])
    assert gsc.query_window(
        "sc-domain:buildmy.house", "tok", date(2026, 10, 1), date(2026, 10, 3), session
    ) == []


def test_incremental_window_applies_three_day_lag():
    start, end = gsc.window(date(2026, 10, 19), lag_days=3, days=7)
    assert end == date(2026, 10, 16)
    assert start == date(2026, 10, 10)
    assert (end - start).days == 6


def test_backfill_chunks_cover_16_months_without_gaps_or_overlap():
    today = date(2026, 10, 19)
    windows = gsc.backfill_windows(today, lag_days=3)
    end = today - timedelta(days=3)

    assert windows[-1][1] == end
    assert (windows[-1][1] - windows[0][0]).days + 1 >= 16 * 28
    for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
        assert s2 == e1 + timedelta(days=1)
        assert s2 <= e2
    for start, stop in windows:
        assert (stop - start).days + 1 <= gsc.BACKFILL_CHUNK_DAYS
        assert stop <= end


def test_upsert_targets_marketing_table_and_is_idempotent():
    assert "INSERT INTO marketing.gsc_search_analytics" in gsc.UPSERT_SQL
    assert "ON CONFLICT (site_url, date, query, page, country, device)" in gsc.UPSERT_SQL
    assert "DO UPDATE" in gsc.UPSERT_SQL


def test_write_rows_batches_executemany():
    conn = FakeConn()
    rows = [
        {"date": date(2026, 10, 1), "query": "q", "page": "p", "country": "usa",
         "device": "DESKTOP", "clicks": 1, "impressions": 2, "ctr": 0.5, "position": 1.0}
    ] * (gsc.BATCH_SIZE + 1)

    assert gsc.write_rows(conn, "sc-domain:buildmy.house", rows) == gsc.BATCH_SIZE + 1
    batches = conn.cursor_obj.executed
    assert [len(params) for _, params in batches] == [gsc.BATCH_SIZE, 1]
    assert batches[0][1][0][0] == "sc-domain:buildmy.house"


def test_403_error_does_not_leak_credentials():
    session = FakeSession([FakeResponse(status_code=403, text="forbidden")])
    with pytest.raises(gsc.GSCAuthError) as excinfo:
        gsc.query_window(
            "sc-domain:buildmy.house", "secret-token", date(2026, 10, 1), date(2026, 10, 3), session
        )
    message = str(excinfo.value)
    assert "403" in message
    assert "secret-token" not in message


def test_5xx_is_retried_then_succeeds():
    session = FakeSession([
        FakeResponse(status_code=503, text="unavailable"),
        FakeResponse(payload={"rows": []}),
    ])
    rows = gsc.query_window(
        "sc-domain:buildmy.house", "tok", date(2026, 10, 1), date(2026, 10, 1), session
    )
    assert rows == []
    assert len(session.calls) == 2


def test_dry_run_writes_nothing(monkeypatch):
    session = FakeSession([
        FakeResponse(payload={"access_token": "tok"}),
        FakeResponse(payload={"rows": []}),
    ])

    def _boom(*args, **kwargs):
        raise AssertionError("dry-run must not connect to Postgres")

    monkeypatch.setattr(gsc.psycopg, "connect", _boom)
    summary = gsc.ingest(
        dry_run=True, today=date(2026, 10, 19), sa=_service_account(), session=session
    )
    assert summary["status"] == "dry-run"
    assert summary["rows_fetched"] == 0
    assert summary["mode"] == "incremental"


def test_backfill_mode_runs_one_query_per_window():
    n = len(gsc.backfill_windows(date(2026, 10, 19), 3))
    session = FakeSession(
        [FakeResponse(payload={"access_token": "tok"})]
        + [FakeResponse(payload={}) for _ in range(n)]
    )
    summary = gsc.ingest(
        backfill=True, dry_run=True, today=date(2026, 10, 19),
        sa=_service_account(), session=session,
    )
    assert summary["mode"] == "backfill"
    assert summary["windows"] == n
    assert len(session.calls) == n + 1


def test_empty_run_records_ok_with_zero_rows(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(gsc.psycopg, "connect", lambda dsn: conn)
    runs = []
    monkeypatch.setattr(gsc, "record_run", lambda *a, **kw: runs.append((a, kw)))
    monkeypatch.setenv("MARKETING_INGEST_DATABASE_URL", "postgresql://marketing@db/x")
    session = FakeSession([
        FakeResponse(payload={"access_token": "tok"}),
        FakeResponse(payload={}),
    ])

    summary = gsc.ingest(today=date(2026, 10, 19), sa=_service_account(), session=session)

    assert summary["status"] == "ok"
    assert summary["rows_fetched"] == 0
    assert conn.commits >= 1
    assert conn.closed is True
    assert runs and runs[0][0][1] == "gsc"
    assert runs[0][0][4] == "ok"
    assert runs[0][0][5] == 0


def test_auth_failure_records_auth_failed_and_raises(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(gsc.psycopg, "connect", lambda dsn: conn)
    runs = []
    monkeypatch.setattr(gsc, "record_run", lambda *a, **kw: runs.append((a, kw)))
    monkeypatch.setenv("MARKETING_INGEST_DATABASE_URL", "postgresql://marketing@db/x")
    session = FakeSession([FakeResponse(status_code=401, text="unauthorized")])

    with pytest.raises(gsc.GSCError):
        gsc.ingest(today=date(2026, 10, 19), sa=_service_account(), session=session)

    assert runs and runs[0][0][4] == "auth_failed"
    assert conn.rollbacks == 1


def test_missing_dsn_fails_before_http(monkeypatch):
    monkeypatch.delenv("MARKETING_INGEST_DATABASE_URL", raising=False)
    session = FakeSession([])
    with pytest.raises(ValueError, match="MARKETING_INGEST_DATABASE_URL"):
        gsc.ingest(today=date(2026, 10, 19), sa=_service_account(), session=session)
    assert session.calls == []


def test_cli_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        gsc.main(["--help"])
    assert excinfo.value.code == 0
    assert "--backfill" in capsys.readouterr().out


def test_main_reports_missing_credentials_without_echoing_them(monkeypatch, capsys):
    monkeypatch.delenv("GSC_SERVICE_ACCOUNT_JSON", raising=False)
    assert gsc.main(["--dry-run"]) == 1
    assert "GSC_SERVICE_ACCOUNT_JSON" in capsys.readouterr().err
