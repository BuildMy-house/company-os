"""Fail-open Axiom ingest for company-os Python components (jobs, pm-agent).

Same event envelope as hive/lib/hive/telemetry.ex and the opencode/hermes
plugins, so one APL query covers every component:

    _time, service, environment, role, event, + caller fields

* service      AXIOM_SERVICE_NAME env (set per manifest in k8s/), else default
* environment  DEPLOYMENT_ENVIRONMENT env, else "local"
* role         caller-provided: "job", "agent", ...
* dataset      AXIOM_DATASET (default bmh-company), eu-central-1 edge ingest

No-op without AXIOM_TOKEN. Never raises: telemetry must not fail a backup or
break a chat request. Callers must keep secrets/PII out of fields.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any

DEFAULT_DATASET = "bmh-company"
INGEST_URL = "https://eu-central-1.aws.edge.axiom.co/v1/ingest/{dataset}"
TIMEOUT_SECONDS = 3.0


def build_event(event: str, role: str, service: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "_time": datetime.now(timezone.utc).isoformat(),
        "service": os.environ.get("AXIOM_SERVICE_NAME", service),
        "environment": os.environ.get("DEPLOYMENT_ENVIRONMENT", "local"),
        "role": role,
        "event": event,
        **fields,
    }


def emit(event: str, *, role: str, service: str, **fields: Any) -> bool:
    """Send one event synchronously (short timeout). Returns True on success."""
    try:
        token = os.environ.get("AXIOM_TOKEN")
        if not token:
            return False
        dataset = os.environ.get("AXIOM_DATASET") or DEFAULT_DATASET
        body = json.dumps([build_event(event, role, service, fields)], default=str).encode()
        request = urllib.request.Request(
            INGEST_URL.format(dataset=dataset),
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):  # noqa: S310
            return True
    except Exception:
        return False


def emit_background(event: str, *, role: str, service: str, **fields: Any) -> None:
    """Fire-and-forget variant for request paths and long-lived servers."""
    threading.Thread(
        target=lambda: emit(event, role=role, service=service, **fields), daemon=True
    ).start()
