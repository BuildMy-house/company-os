"""axiom_usage — ships token-usage metrics AND full message/tool-call content
to Axiom.

Sibling to plugins/observability/langfuse: same register(ctx)/hook-name
contract. Axiom is the cross-tool (Hermes + Claude Code + OpenCode) activity
dashboard. Content-visible by explicit decision (2026-09-18) — previously
compact/metadata-only; the user asked to see full conversation content
across all three agent planes in Axiom. This ships real prompt/response text
and tool args/results to a third-party SaaS — if that scope ever needs to be
narrowed again, drop the `text`/`args`/`result` fields from the pushed
events below and keep only the usage/timing metadata.
Every hook body is failsafe — a broken network call must never interrupt an
actual Hermes turn.
"""
from __future__ import annotations

import atexit
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
_ENDPOINT = f"https://eu-central-1.aws.edge.axiom.co/v1/ingest/{_DATASET}"
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
        event.setdefault("service", os.environ.get("AXIOM_SERVICE_NAME", "hermes-gateway"))
        event.setdefault("environment", os.environ.get("DEPLOYMENT_ENVIRONMENT", "local"))
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


def _flush_all_atexit() -> None:
    # Short-lived processes (e.g. `hermes chat --oneshot`) can exit well
    # before the 5s daemon timer ever fires, silently dropping queued
    # events. Guarantee at least one synchronous flush on interpreter exit.
    while not _queue.empty():
        _flush_once()


def _ensure_started() -> None:
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_flush_loop, name="axiom-usage-flush", daemon=True).start()
        atexit.register(_flush_all_atexit)
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


def _text_of(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str) and content:
        return content
    return None


def on_pre_api_request(*, task_id: str = "", session_id: str = "", turn_id: str = "",
                       user_message: Any = None, request_messages: Any = None, **_: Any) -> None:
    try:
        _ensure_started()
        text = _text_of(user_message)
        if not text and isinstance(request_messages, list) and request_messages:
            text = _text_of(request_messages[-1])
        if not text:
            return
        _push({
            "event": "message", "role": "user", "task_id": task_id, "session_id": session_id,
            "turn_id": turn_id, "text": text,
        })
    except Exception as exc:
        logger.debug("axiom_usage: on_pre_api_request failed: %s", exc)


def on_post_api_request(*, task_id: str = "", session_id: str = "", turn_id: str = "", provider: str = "",
                        model: str = "", api_duration: float = 0.0, usage: Any = None,
                        response_model: Any = None, assistant_message: Any = None, **_: Any) -> None:
    try:
        _ensure_started()
        resolved_model = response_model if isinstance(response_model, str) and response_model else model
        _push({
            "event": "llm_call", "task_id": task_id, "session_id": session_id, "turn_id": turn_id,
            "provider": provider, "model": resolved_model, "duration_ms": round((api_duration or 0.0) * 1000),
            **_usage_from(usage),
        })
        text = _text_of(assistant_message)
        if text:
            _push({
                "event": "message", "role": "assistant", "task_id": task_id, "session_id": session_id,
                "turn_id": turn_id, "text": text,
            })
    except Exception as exc:
        logger.debug("axiom_usage: on_post_api_request failed: %s", exc)


def on_post_llm_call(*, task_id: str = "", session_id: str = "", turn_id: str = "",
                     user_message: Any = None, assistant_response: Any = None, model: str = "",
                     **_: Any) -> None:
    try:
        _ensure_started()
        _push({
            "event": "turn", "task_id": task_id, "session_id": session_id, "turn_id": turn_id,
            "model": model, "user_text": _text_of(user_message), "assistant_text": _text_of(assistant_response),
        })
    except Exception as exc:
        logger.debug("axiom_usage: on_post_llm_call failed: %s", exc)


def on_post_tool_call(*, tool_name: str = "", task_id: str = "", session_id: str = "",
                      tool_call_id: str = "", turn_id: str = "", args: Any = None,
                      result: Any = None, **_: Any) -> None:
    try:
        _ensure_started()
        result_text = result if isinstance(result, str) else json.dumps(result, default=str) if result is not None else None
        _push({
            "event": "tool_call", "task_id": task_id, "session_id": session_id, "turn_id": turn_id,
            "tool_name": tool_name, "tool_call_id": tool_call_id,
            "args": args if isinstance(args, (dict, list, str, int, float, bool)) else json.dumps(args, default=str) if args is not None else None,
            "result": result_text[:8000] if result_text else None,
        })
    except Exception as exc:
        logger.debug("axiom_usage: on_post_tool_call failed: %s", exc)


def register(ctx) -> None:
    hooks = (
        ("pre_api_request", on_pre_api_request),
        ("post_api_request", on_post_api_request),
        ("post_llm_call", on_post_llm_call),
        ("post_tool_call", on_post_tool_call),
    )
    for name, fn in hooks:
        try:
            ctx.register_hook(name, fn)
        except Exception as exc:
            logger.debug("axiom_usage: failed to register hook %s: %s", name, exc)
