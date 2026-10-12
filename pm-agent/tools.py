"""pm-agent's tool implementations: the ONLY actions the model can take.

Two tools, both bound server-side to the in-flight request:

- find_similar_signal(topic): read-only dedupe lookup (PmStore).
- file_feedback_work(topic, summary): the gated filing path. Dedupe ->
  escalation threshold -> rate limits -> length caps and "user-reported
  (untrusted)" marking -> company_ops.hive_client.file_feedback_work.

Who the user is, and what they actually said, come from the request context
written by the front door (agent.py), never from model-supplied arguments, so
injected text cannot impersonate another user or forge the quote. Every tool
call is appended to company.pm_conversations (role "tool").
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import guard
from company_ops.hive_client import HiveClientError, file_feedback_work

ESCALATION_THRESHOLD = 5
CONTEXT_MAX_AGE_SECONDS = 180
DEFAULT_CONTEXT_PATH = "/run/pm-agent/context.json"
FILINGS_PER_USER_PER_HOUR = 3
FILINGS_GLOBAL_PER_HOUR = 20

# The complete model-visible tool surface. tests/test_pm_agent.py asserts the
# MCP server advertises exactly these names (plus the optional read-only
# Steward pair, which is not wired in v1 -- see docs/PM-AGENT-HERMES.md).
TOOL_NAMES = ("find_similar_signal", "file_feedback_work")
STEWARD_READONLY_TOOLS = ("query_memories", "query_specs")

log = logging.getLogger("pm-agent.tools")


class ContextError(Exception):
    """No fresh request context: refuse rather than guess who is asking."""


def write_context(path: str, app_user_id: str, conversation_id: str, message: str) -> None:
    payload = {
        "app_user_id": app_user_id,
        "conversation_id": conversation_id,
        "message": message,
        "at": time.time(),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, target)


def clear_context(path: str) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def read_context(path: str, now: float | None = None) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ContextError("no active request context") from exc
    age = (now if now is not None else time.time()) - float(data.get("at") or 0)
    if age > CONTEXT_MAX_AGE_SECONDS or age < -5:
        raise ContextError("request context expired")
    for key in ("app_user_id", "conversation_id", "message"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ContextError(f"request context missing {key}")
    return data


class PmTools:
    def __init__(
        self,
        store: Any,
        hive_url: str,
        context_path: str = DEFAULT_CONTEXT_PATH,
        user_limiter: guard.RateLimiter | None = None,
        global_limiter: guard.RateLimiter | None = None,
    ) -> None:
        self.store = store
        self.hive_url = hive_url
        self.context_path = context_path
        self.user_limiter = user_limiter or guard.RateLimiter(
            FILINGS_PER_USER_PER_HOUR, 3600
        )
        self.global_limiter = global_limiter or guard.RateLimiter(
            FILINGS_GLOBAL_PER_HOUR, 3600
        )

    # -- dispatch -----------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call, log it, and return a JSON-serialisable result."""
        if name not in TOOL_NAMES:
            return {"error": f"unknown tool {name!r}"}
        try:
            ctx = read_context(self.context_path)
        except ContextError as exc:
            return {"error": str(exc)}
        try:
            if name == "find_similar_signal":
                result = self.find_similar_signal(arguments.get("topic"))
            else:
                result = self.file_feedback(
                    ctx, arguments.get("topic"), arguments.get("summary")
                )
        except ValueError as exc:
            result = {"error": str(exc)}
        self._log(ctx, name, arguments, result)
        return result

    def _log(
        self, ctx: dict[str, Any], name: str, arguments: dict[str, Any], result: dict[str, Any]
    ) -> None:
        entry = {
            "tool": name,
            "arguments": {k: guard.cap(str(v), 300) for k, v in arguments.items()},
            "result": result,
        }
        # Structured stdout log (stdout is the only telemetry channel).
        log.info(json.dumps({"event": "tool_call", "app_user_id": ctx["app_user_id"], **entry}))
        try:
            self.store.record_message(
                ctx["conversation_id"], ctx["app_user_id"], "tool", json.dumps(entry)
            )
        except Exception:  # noqa: BLE001 - audit failure must not crash the tool
            log.exception("failed to record tool call in pm_conversations")

    # -- tools --------------------------------------------------------------

    def find_similar_signal(self, topic: Any) -> dict[str, Any]:
        key = _topic(topic)
        signal = self.store.find_similar_signal(key)
        if signal is None:
            return {"found": False, "topic": key}
        return {
            "found": True,
            "topic": key,
            "occurrence_count": int(signal.get("occurrence_count") or 0),
            "status": str(signal.get("status") or "new"),
            "filed": bool(signal.get("linked_hive_work_id")),
            "escalation_threshold": ESCALATION_THRESHOLD,
        }

    def file_feedback(self, ctx: dict[str, Any], topic: Any, summary: Any) -> dict[str, Any]:
        key = _topic(topic)
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary is required")
        summary = guard.normalize_user_text(summary)
        app_user_id = ctx["app_user_id"]
        quote = guard.cap(ctx["message"], guard.MAX_QUOTE_CHARS)

        signal = self.store.find_similar_signal(key)
        if signal is not None:
            count = int(signal.get("occurrence_count") or 0)
            if count < ESCALATION_THRESHOLD:
                # Known and below threshold: record the occurrence, never
                # re-file with Hive.
                self.store.upsert_signal(key, quote)
                return {
                    "outcome": "already_known",
                    "occurrence_count": count + 1,
                    "status": str(signal.get("status") or "new"),
                }

        # Genuinely new, or threshold crossed: this would file a Hive item.
        if not (
            self.user_limiter.allow(app_user_id) and self.global_limiter.allow("*")
        ):
            self.store.upsert_signal(key, quote)
            return {"outcome": "rate_limited"}

        title = guard.untrusted_title(key)
        description = guard.untrusted_description(app_user_id, summary, quote)
        metadata = {
            "source": "pm-agent",
            "app_user_id": guard.cap(app_user_id, guard.MAX_USER_ID_CHARS),
            "untrusted": True,
            "escalation": signal is not None,
        }
        hive_work_id = ""
        try:
            result = file_feedback_work(self.hive_url, title, description, metadata)
            hive_work_id = str(result.get("work_id") or result.get("id") or "")
        except HiveClientError:
            log.exception("failed to file feedback work with Hive")
        signal_id = self.store.upsert_signal(key, quote)
        if hive_work_id:
            self.store.mark_signal_filed(signal_id, hive_work_id)
        if not hive_work_id:
            return {"outcome": "recorded_unfiled"}
        if signal is None:
            return {"outcome": "filed_new", "work_id": hive_work_id}
        return {
            "outcome": "escalated",
            "work_id": hive_work_id,
            "occurrence_count": int(signal.get("occurrence_count") or 0) + 1,
        }


def _topic(topic: Any) -> str:
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("topic is required")
    key = guard.clean_topic(topic)
    if not key:
        raise ValueError("topic is required")
    return key
