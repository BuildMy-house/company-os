# This client deliberately exposes ONLY message/send (filing work) and
# intentionally does NOT implement /agents/register, /work/:id/bids, or
# /work/:id/claim -- this is a structural scope limit for pm-agent, not an
# oversight.
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any


class HiveClientError(Exception):
    """Raised when the Hive JSON-RPC call fails or the response is malformed."""


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "pm-agent-hive-client/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HiveClientError(
            f"Hive returned invalid JSON (HTTP {status}): {raw!r}"
        ) from exc

    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        raise HiveClientError(
            f"Hive JSON-RPC error {error.get('code', 'unknown')}: "
            f"{error.get('message', '')} (HTTP {status})"
        )
    if not 200 <= status < 300:
        raise HiveClientError(f"Hive HTTP {status}: {raw!r}")
    if not isinstance(payload, dict) or "result" not in payload:
        raise HiveClientError(f"Hive response missing result field: {payload!r}")
    return payload["result"]


def file_feedback_work(
    hive_url: str, title: str, description: str, metadata: dict
) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {"parts": [{"type": "text", "text": description}]},
            "metadata": {"title": title, **metadata},
        },
    }
    return _post(f"{hive_url.rstrip('/')}/", body)
