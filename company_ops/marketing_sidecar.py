"""Sidecar for the publisher pod: the only holder of the ingest DSN and the
Substack session cookie. The agent (main container) reaches it on 127.0.0.1.

Endpoints (JSON in, JSON out, loopback only):
  POST /ingest          {"source": "ga4|gsc|reddit|substack", "days": 1..90}
      Runs the SAME code path as the daily CronJob (company_ops.marketing_ingest.run)
      for one named source: fetch, validate, upsert, record ingest_runs.
      The agent supplies no SQL and no row data, only a source name.
  POST /substack/call   {"tool": ..., "arguments": {...}, "approval_ref": "..."}
      Allowlisted substack-mcp calls (company_ops.substack_bridge). Agent-facing
      tools are list_posts, create_draft_post, publish_draft. publish_draft needs
      a well-formed approval_ref, refuses a reused ref, and is capped per UTC day.
      Subscriber export and every other tool are refused.
  GET  /health

Readiness: after the DB connection and Substack env are checked, a marker file
(MARKETING_INGEST_READY_FILE) is written. The main container declares the
``marketing-ingest`` tool only if that file exists.

Limit: approval_ref is checked for presence and format only. Hive does not
expose payload metadata to the pod, so authenticity cannot be verified here.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg

from company_ops import marketing_ingest, substack_bridge

APPROVAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{5,119}$")
AGENT_SUBSTACK_TOOLS = frozenset({"list_posts", "create_draft_post", "publish_draft"})
MIN_INGEST_INTERVAL = 60  # seconds between pulls of the same source


class Refused(Exception):
    def __init__(self, msg: str, code: int = 403):
        super().__init__(msg)
        self.code = code


class Ledger:
    """Append-only JSONL of approved publishes (reuse + daily-cap enforcement)."""

    def __init__(self, path: str):
        self.path = path

    def entries(self) -> list[dict]:
        try:
            with open(self.path) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def append(self, entry: dict) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def check_publish(self, ref: str, cap: int, now: datetime) -> None:
        entries = self.entries()
        if any(e.get("approval_ref") == ref for e in entries):
            raise Refused("approval_ref was already used for a publish")
        today = now.date().isoformat()
        if sum(1 for e in entries if e.get("ts", "")[:10] == today) >= cap:
            raise Refused(f"daily substack publish cap ({cap}) reached")


def handle_substack(body: dict, env: dict, ledger: Ledger, call=None, now: datetime | None = None) -> dict:
    tool = body.get("tool")
    args = body.get("arguments") or {}
    if tool not in AGENT_SUBSTACK_TOOLS:
        raise Refused(f"substack tool {tool!r} is not exposed to the agent")
    if not isinstance(args, dict):
        raise Refused("arguments must be an object", 400)
    call = call if call is not None else (lambda t, a: substack_bridge.call_tool(t, a, env))
    now = now or datetime.now(UTC)
    if tool == "publish_draft":
        ref = body.get("approval_ref")
        if not isinstance(ref, str) or not APPROVAL_REF_RE.match(ref):
            raise Refused("publish refused: a valid approval_ref from Hermes is required; draft only")
        cap = int(env.get("PUBLISHER_SUBSTACK_DAILY_CAP", "1"))
        ledger.check_publish(ref, cap, now)
        result = call(tool, args)
        ledger.append({"ts": now.isoformat(), "approval_ref": ref, "tool": tool,
                       "draft_id": args.get("draft_id")})
        return result
    return call(tool, args)


def handle_ingest(body: dict, env: dict, last_run: dict, connect=None, run=None, clock=time.monotonic) -> dict:
    source = body.get("source")
    if source not in marketing_ingest.SOURCES:
        raise Refused(f"source must be one of {list(marketing_ingest.SOURCES)}", 400)
    days = body.get("days", 7)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise Refused("days must be an integer 1..90", 400)
    now = clock()
    if source in last_run and now - last_run[source] < MIN_INGEST_INTERVAL:
        raise Refused(f"{source} was pulled less than {MIN_INGEST_INTERVAL}s ago", 429)
    last_run[source] = now
    dsn = env.get("MARKETING_INGEST_DATABASE_URL")
    if not dsn:
        raise Refused("marketing ingest is not configured", 503)
    connect = connect or psycopg.connect
    run = run or marketing_ingest.run
    with connect(dsn) as conn:
        return run(conn, env, days=days, sources=(source,))


def write_ready(env: dict, connect=None) -> bool:
    """Write the ready marker iff the DSN connects and Substack env is complete."""
    path = env.get("MARKETING_INGEST_READY_FILE")
    dsn = env.get("MARKETING_INGEST_DATABASE_URL")
    if not path or not dsn:
        return False
    try:
        with (connect or psycopg.connect)(dsn, connect_timeout=5) as conn:
            conn.execute("select 1")
    except Exception as e:  # noqa: BLE001
        print(f"sidecar: DB check failed: {type(e).__name__}", file=sys.stderr)
        return False
    with open(path, "w") as f:
        f.write(datetime.now(UTC).isoformat())
    return True


def make_handler(env: dict, ledger: Ledger):
    last_run: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # request bodies/headers are never logged
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > 200_000:
                    raise Refused("body too large", 413)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise Refused("body must be an object", 400)
                if self.path == "/ingest":
                    out = handle_ingest(body, env, last_run)
                elif self.path == "/substack/call":
                    out = handle_substack(body, env, ledger)
                else:
                    raise Refused("not found", 404)
                self._send(200, out)
            except Refused as e:
                self._send(e.code, {"error": str(e)})
            except substack_bridge.AuthFailed as e:
                self._send(502, {"error": str(e), "status": "auth_failed"})
            except ValueError as e:
                self._send(400, {"error": str(e)[:200]})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": f"{type(e).__name__}"})

    return H


def main() -> int:
    env = dict(os.environ)
    ledger = Ledger(env.get("PUBLISHER_LEDGER_FILE", "/var/lib/publisher/ledger.jsonl"))
    if not write_ready(env):
        print("sidecar: not ready (DB DSN/ready-file missing or DB unreachable); marketing-ingest stays undeclared",
              file=sys.stderr)
    HTTPServer(("127.0.0.1", int(env.get("MARKETING_SIDECAR_PORT", "8099"))), make_handler(env, ledger)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
