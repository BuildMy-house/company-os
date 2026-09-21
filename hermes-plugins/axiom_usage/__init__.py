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

Caller identity (human vs. agent, e.g. hermes_ask MCP calls vs. interactive
chat) is NOT available at these hook sites today: the HTTP layer for
/v1/chat/completions and the Discord adapter both live in the third-party
nousresearch/hermes-agent base image (not this repo) and discard any
caller-identity signal before invoking hooks. `pod_name` (below) identifies
the emitting container/pod; `caller_channel` (from the `platform` kwarg,
where the two hooks that receive it forward it) is the closest available
proxy for "which surface initiated this", not who initiated it. Do not
invent caller_type/caller_id values — if this needs closing, it requires
upstream changes to the hermes-agent base image or a caller-side header
added at the hermes_ask/CLI/Discord ingress, both out of this repo's scope.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import socket
import threading
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_DATASET = "bmh-company"
# bmh-company lives on Axiom's eu-central-1 edge deployment, not the default
# api.axiom.co domain — that domain rejects ingest for this dataset with
# HTTP 400 ("must use the eu-central-1 edge deployment domain"). Edge
# ingest also uses a different path shape (/v1/ingest/<dataset>, not
# /v1/datasets/<dataset>/ingest). Confirmed live against the real token.
_ENDPOINT = f"https://eu-central-1.aws.edge.axiom.co/v1/ingest/{_DATASET}"
_FLUSH_INTERVAL = 5.0
_BATCH_MAX = 50

_queue: "queue.Queue[dict]" = queue.Queue()
_started = False
_start_lock = threading.Lock()


_failure_counts: dict[str, int] = {}
_failure_lock = threading.Lock()


def _record_failure(where: str) -> None:
    try:
        with _failure_lock:
            _failure_counts[where] = _failure_counts.get(where, 0) + 1
    except Exception:
        pass


def _pod_name() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return os.environ.get("HOSTNAME", "unknown")


_POD_NAME = _pod_name()


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
        event.setdefault("role", "hermes")
        event.setdefault("pod_name", _POD_NAME)
        _queue.put_nowait(event)
    except Exception as exc:
        _record_failure("enqueue")
        logger.warning("axiom_usage: enqueue failed: %s", exc)


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
        try:
            if _failure_counts:
                with _failure_lock:
                    summary = dict(_failure_counts)
                    _failure_counts.clear()
                logger.warning("axiom_usage: %d event(s) failed across %s since last successful flush", sum(summary.values()), summary)
        except Exception:
            pass
    except Exception as exc:
        _record_failure("flush")
        logger.warning("axiom_usage: flush of %d events failed: %s", len(batch), exc)


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


# src key candidates per output field, tried in order. OpenAI-chat-completions
# shaped usage (this deployment's live provider) uses prompt_tokens/
# completion_tokens; Anthropic-shaped usage uses input/output (+ cache_* variants).
_USAGE_ATTRS = (
    ("input_tokens", ("prompt_tokens", "input_tokens")),
    ("output_tokens", ("completion_tokens", "output_tokens")),
    ("cache_read_tokens", ("cache_read_input_tokens",)),
    ("cache_write_tokens", ("cache_creation_input_tokens",)),
    ("reasoning_tokens", ("reasoning_tokens",)),
)


def _usage_from(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    get = usage.get if isinstance(usage, dict) else lambda key, default=None: getattr(usage, key, default)
    out: dict[str, Any] = {}
    for out_key, src_keys in _USAGE_ATTRS:
        for src_key in src_keys:
            val = get(src_key)
            if val:
                out[out_key] = val
                break
    if "cache_read_tokens" not in out:
        # OpenAI chat shape nests cache reads at prompt_tokens_details.cached_tokens
        details = get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if cached:
            out["cache_read_tokens"] = cached
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
        _record_failure("on_pre_api_request")
        logger.warning("axiom_usage: on_pre_api_request failed: %s", exc)


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
        _record_failure("on_post_api_request")
        logger.warning("axiom_usage: on_post_api_request failed: %s", exc)


def on_post_llm_call(*, task_id: str = "", session_id: str = "", turn_id: str = "",
                     user_message: Any = None, assistant_response: Any = None, model: str = "",
                     platform: str = "", **_: Any) -> None:
    try:
        _ensure_started()
        _push({
            "event": "turn", "task_id": task_id, "session_id": session_id, "turn_id": turn_id,
            "model": model, "caller_channel": platform or None,
            "user_text": _text_of(user_message), "assistant_text": _text_of(assistant_response),
        })
    except Exception as exc:
        _record_failure("on_post_llm_call")
        logger.warning("axiom_usage: on_post_llm_call failed: %s", exc)


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
        _record_failure("on_post_tool_call")
        logger.warning("axiom_usage: on_post_tool_call failed: %s", exc)


def on_session_end(*, platform: str = "", **_: Any) -> None:
    """Force a synchronous flush when a session/turn ends.

    The background `_flush_loop` thread only drains the queue every
    `_FLUSH_INTERVAL` (5s). Short-lived invocations — `hermes -z`/--oneshot
    in particular — hard-exit via `os._exit()` right after the turn
    completes (deliberately skipping the atexit chain, see
    hermes_cli/main.py's `_exit_after_oneshot`), so a turn that finishes in
    under 5s drops its queued event silently: confirmed live (2026-09-20)
    via a monkeypatched `urllib.request.urlopen` trace showing
    `post_api_request` firing with full usage data but zero Axiom pushes
    for a real oneshot turn. `on_session_end` already fires synchronously
    before that hard exit (traced live), so draining the queue here
    closes the gap without touching the batching behavior long-running
    gateway/interactive sessions already rely on.
    """
    try:
        _ensure_started()
        _flush_once()
    except Exception as exc:
        _record_failure("on_session_end")
        logger.warning("axiom_usage: on_session_end flush failed: %s", exc)


def register(ctx) -> None:
    hooks = (
        ("pre_api_request", on_pre_api_request),
        ("post_api_request", on_post_api_request),
        ("post_llm_call", on_post_llm_call),
        ("post_tool_call", on_post_tool_call),
        ("on_session_end", on_session_end),
    )
    for name, fn in hooks:
        try:
            ctx.register_hook(name, fn)
        except Exception as exc:
            _record_failure("register")
            logger.warning("axiom_usage: failed to register hook %s: %s", name, exc)
