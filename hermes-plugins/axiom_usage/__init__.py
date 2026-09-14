"""axiom_usage — ships lightweight token-usage and tool-call metrics to Axiom.

Sibling to plugins/observability/langfuse: same register(ctx)/hook-name
contract, but ships compact usage records instead of full trace content.
Axiom is the cross-tool (Hermes + Claude Code + OpenCode) usage dashboard;
Langfuse, if ever enabled, remains the place for full trace/tool-call content.
Every hook body is failsafe — a broken network call must never interrupt an
actual Hermes turn.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_DATASET = "bmh-company"
_ENDPOINT = f"https://api.axiom.co/v1/datasets/{_DATASET}/ingest"
_FLUSH_INTERVAL = 5.0
_BATCH_MAX = 50

_queue: "queue.Queue[dict]" = queue.Queue()
_started = False
_start_lock = threading.Lock()


def _token() -> str:
    try:
        from agent.secret_scope import get_secret
        val = get_secret("AXIOM_TOKEN")
        if val:
            return val.strip()
    except Exception:
        pass
    return (os.environ.get("AXIOM_TOKEN") or "").strip()


def _push(event: dict[str, Any]) -> None:
    try:
        event.setdefault("_time", time.time())
        event.setdefault("service", "hermes-gateway")
        _queue.put_nowait(event)
    except Exception as exc:
        logger.debug("axiom_usage: enqueue failed: %s", exc)


def _flush_once() -> None:
    token = _token()
    if not token:
        return
    batch: list[dict] = []
    while len(batch) < _BATCH_MAX:
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            break
    if not batch:
        return
    try:
        body = json.dumps(batch).encode("utf-8")
        req = urllib.request.Request(_ENDPOINT, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        logger.debug("axiom_usage: flush of %d events failed: %s", len(batch), exc)


def _flush_loop() -> None:
    while True:
        time.sleep(_FLUSH_INTERVAL)
        _flush_once()


def _ensure_started() -> None:
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_flush_loop, name="axiom-usage-flush", daemon=True).start()
        _started = True


_USAGE_ATTRS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_tokens", "cache_read_input_tokens"),
    ("cache_write_tokens", "cache_creation_input_tokens"),
    ("reasoning_tokens", "reasoning_tokens"),
)


def _usage_from(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    get = usage.get if isinstance(usage, dict) else lambda key, default=None: getattr(usage, key, default)
    out: dict[str, Any] = {}
    for out_key, src_key in _USAGE_ATTRS:
        val = get(src_key)
        if val:
            out[out_key] = val
    return out


def on_post_llm_call(*, task_id: str = "", session_id: str = "", turn_id: str = "", provider: str = "",
                     model: str = "", response: Any = None, api_duration: float = 0.0, usage: Any = None,
                     response_model: Any = None, **_: Any) -> None:
    try:
        _ensure_started()
        resolved_model = response_model if isinstance(response_model, str) and response_model else model
        resolved_usage = usage if isinstance(usage, dict) and usage else getattr(response, "usage", None)
        _push({
            "event": "llm_call", "task_id": task_id, "session_id": session_id, "turn_id": turn_id,
            "provider": provider, "model": resolved_model, "duration_ms": round((api_duration or 0.0) * 1000),
            **_usage_from(resolved_usage),
        })
    except Exception as exc:
        logger.debug("axiom_usage: on_post_llm_call failed: %s", exc)


def on_post_tool_call(*, tool_name: str = "", task_id: str = "", session_id: str = "",
                      tool_call_id: str = "", turn_id: str = "", **_: Any) -> None:
    try:
        _ensure_started()
        _push({
            "event": "tool_call", "task_id": task_id, "session_id": session_id, "turn_id": turn_id,
            "tool_name": tool_name, "tool_call_id": tool_call_id,
        })
    except Exception as exc:
        logger.debug("axiom_usage: on_post_tool_call failed: %s", exc)


def register(ctx) -> None:
    # Both hook-name variants, same reasoning as the langfuse plugin: *_api_request
    # fires per API call (preferred); *_llm_call fires once per turn on older Hermes versions.
    hooks = (
        ("post_api_request", on_post_llm_call),
        ("post_llm_call", on_post_llm_call),
        ("post_tool_call", on_post_tool_call),
    )
    for name, fn in hooks:
        try:
            ctx.register_hook(name, fn)
        except Exception as exc:
            logger.debug("axiom_usage: failed to register hook %s: %s", name, exc)
