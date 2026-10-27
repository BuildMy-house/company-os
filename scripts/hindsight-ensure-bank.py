#!/usr/bin/env python3
"""Ensure one Hindsight bank exists (idempotent create-or-update).

Hindsight only creates a bank on a write, and the CEO runs tools-only
(auto_retain=false), so nothing ever creates the CEO's bank: every
hindsight_recall then 404s with "Bank 'ceo' not found" (docs/HINDSIGHT-BANKS.md).
This waits for the server to answer GET /health, GETs the bank profile and,
when it is missing (404), PUTs an empty create-or-update body to
/v1/default/banks/{bank_id} (idempotent).

    HINDSIGHT_URL=http://hindsight:8888 HINDSIGHT_API_KEY=... \
        python3 scripts/hindsight-ensure-bank.py [--bank ceo] [--wait 600]

The bank defaults to $HERMES_HOME/hindsight/config.json's bank_id, else "ceo".
Stdlib only (runs in the Hermes image). The API key is never printed; logs
carry only the bank name, url and outcome. Exit 0 on success, nonzero on
failure (missing key, server never healthy, request error).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TAG = "[hindsight]"
POLL_INTERVAL = 2


def _log(msg: str) -> None:
    print(f"{TAG} {msg}", flush=True)


def _request(url: str, key: str, path: str, method: str = "GET", body: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def _wait_healthy(url: str, key: str, deadline_s: int) -> bool:
    """Poll GET /health until it answers or deadline_s passes."""
    end = time.monotonic() + deadline_s
    while True:
        try:
            _request(url, key, "/health", timeout=10)
            return True
        except urllib.error.HTTPError as exc:
            status = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            status = type(exc).__name__
        if time.monotonic() >= end:
            _log(f"server not healthy at {url} after {deadline_s}s (last: {status})")
            return False
        time.sleep(POLL_INTERVAL)


def ensure_bank(url: str, key: str, bank: str) -> str:
    """Create the bank if missing. Returns "exists" or "created"."""
    try:
        _request(url, key, f"/v1/default/banks/{bank}")
        return "exists"
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    _request(url, key, f"/v1/default/banks/{bank}", method="PUT", body={})
    return "created"


def default_bank() -> str:
    path = os.path.join(os.environ.get("HERMES_HOME") or "/opt/data", "hindsight", "config.json")
    try:
        with open(path) as f:
            bank = json.load(f).get("bank_id")
    except (OSError, ValueError):
        bank = None
    return bank or "ceo"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ensure a Hindsight bank exists (idempotent).")
    p.add_argument("--bank", default=None, help="bank id (default: $HERMES_HOME/hindsight/config.json bank_id, else ceo)")
    p.add_argument("--url", default=os.environ.get("HINDSIGHT_URL") or "http://hindsight:8888")
    p.add_argument("--wait", type=int, default=600, help="seconds to wait for server health (default 600)")
    args = p.parse_args(argv)
    bank = args.bank or default_bank()
    key = os.environ.get("HINDSIGHT_API_KEY", "").strip()
    if not key or key.startswith("${"):
        _log(f"ERROR: HINDSIGHT_API_KEY not set; cannot ensure bank {bank}")
        return 2
    if not _wait_healthy(args.url, key, args.wait):
        return 1
    try:
        action = ensure_bank(args.url, key, bank)
    except urllib.error.HTTPError as exc:
        _log(f"ERROR: ensuring bank {bank}: HTTP {exc.code}")
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log(f"ERROR: ensuring bank {bank}: {type(exc).__name__}")
        return 1
    _log(f"bank {bank} {action} at {args.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
