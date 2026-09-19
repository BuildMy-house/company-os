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


def on_session_end(**_: Any) -> None:
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
        logger.debug("axiom_usage: on_session_end flush failed: %s", exc)


def register(ctx) -> None:
    # Both hook-name variants, same reasoning as the langfuse plugin: *_api_request
    # fires per API call (preferred); *_llm_call fires once per turn on older Hermes versions.
    hooks = (
        ("post_api_request", on_post_llm_call),
        ("post_llm_call", on_post_llm_call),
        ("post_tool_call", on_post_tool_call),
        ("on_session_end", on_session_end),
    )
    for name, fn in hooks:
        try:
            ctx.register_hook(name, fn)
        except Exception as exc:
            logger.debug("axiom_usage: failed to register hook %s: %s", name, exc)
