"""Hive membership for pm-agent (docs/AGENT-CONTRACT.md).

Registers with POST /agents/register and re-registers on an interval; the
upsert refreshes last_seen_at, which is pm-agent's heartbeat. pm-agent
declares only the observe and propose modes: it is a chat front door, not a
worker, so it never bids on, claims, or executes Hive work (it declares no
bid/execute mode, so Hive's prompt/allocation paths skip it).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Any

log = logging.getLogger("pm-agent.hive")

HEARTBEAT_SECONDS = 60
MAX_BACKOFF_SECONDS = 60


def registration_payload(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env if env is not None else dict(os.environ)
    return {
        "id": env.get("HIVE_AGENT_ID", "pm-agent"),
        "endpoint": env.get("A2A_ENDPOINT", "http://pm-agent:4200"),
        "capabilities": {
            "profile": env.get("AGENT_PROFILE", "communicator"),
            "modes": ["observe", "propose"],
            "flavor": "hermes",
            "role": "front-door",
            "tier": "cheap",
        },
    }


def register_once(hive_url: str, payload: dict[str, Any], timeout: float = 10) -> None:
    req = urllib.request.Request(
        f"{hive_url.rstrip('/')}/agents/register",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        if not 200 <= getattr(resp, "status", 200) < 300:
            raise OSError(f"Hive register returned HTTP {resp.status}")


def run_forever(hive_url: str, stop: threading.Event) -> None:
    """Register, then heartbeat; never raise (Hive may be down at boot)."""
    payload = registration_payload()
    failures = 0
    while not stop.is_set():
        try:
            register_once(hive_url, payload)
            if failures:
                log.info(json.dumps({"event": "hive_registered", "after_failures": failures}))
            failures = 0
            delay = HEARTBEAT_SECONDS
        except Exception as exc:  # noqa: BLE001
            failures += 1
            delay = min(MAX_BACKOFF_SECONDS, 2 * 2 ** (failures - 1))
            log.warning(
                json.dumps({"event": "hive_register_failed", "attempt": failures, "error": str(exc)})
            )
        stop.wait(delay)


def start(hive_url: str) -> threading.Event:
    stop = threading.Event()
    threading.Thread(
        target=run_forever, args=(hive_url, stop), name="hive-presence", daemon=True
    ).start()
    return stop
