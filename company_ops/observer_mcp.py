"""Stdio MCP server exposing the Observer evidence writers to Hermes.

Why this exists: hermes/SOUL.md tells the CEO to make a `record_decision`
call for every action, but `ObserverWriter.record_decision` is only a Python
method. No Hermes tool or MCP server ever exposed it, so the call had nothing
to bind to and the observer.decisions/failures/predictions/... tables stayed
empty (only human_requests, which spend_review.py writes directly, had rows).

Design rules:
- Every failure is loud: startup exits non-zero if the DSN is missing or the
  database is unreachable, and a failing tool call returns `isError: true`
  with the real exception text (plus a stderr line) instead of pretending to
  succeed. A successful call returns the new row id.
- Append-only: only ObserverWriter's INSERT paths are reachable.
- Hand-rolled JSON-RPC over stdio (newline-delimited), no extra dependency in
  the Hermes image. Run as `python -m company_ops.observer_mcp`.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from company_ops.observer import ObserverWriter

PROTOCOL_VERSION = "2024-11-05"

_STR = {"type": "string"}


def _schema(required: list[str], **props: dict) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOLS: dict[str, tuple[str, dict]] = {
    "record_decision": (
        "Append a decision to the Observer ledger (observer.decisions). Call "
        "for every action you decide on. Returns the new decision id.",
        _schema(
            ["problem", "decision"],
            problem=_STR, decision=_STR, evidence_refs=_STR,
            reasoning_summary=_STR, alternatives_considered=_STR,
            confidence=_STR, expected_outcome=_STR, initiated_by=_STR,
        ),
    ),
    "record_prediction": (
        "Append a prediction tied to a decision id. Returns the prediction id.",
        _schema(
            ["decision_id", "metric", "target_value"],
            decision_id=_STR, metric=_STR, target_value=_STR,
            confidence=_STR, evaluation_date=_STR, initiated_by=_STR,
        ),
    ),
    "evaluate_prediction": (
        "Append the evaluated outcome of an earlier prediction.",
        _schema(
            ["prediction_id", "actual_value", "outcome"],
            prediction_id=_STR, actual_value=_STR, outcome=_STR,
        ),
    ),
    "record_experiment": (
        "Append a started experiment. Returns the experiment id.",
        _schema(
            ["name", "hypothesis"],
            name=_STR, hypothesis=_STR, status=_STR, started_at=_STR,
            initiated_by=_STR,
        ),
    ),
    "decide_experiment": (
        "Append the decision for an earlier experiment.",
        _schema(
            ["experiment_id", "decision"], experiment_id=_STR, decision=_STR,
        ),
    ),
    "add_relationship": (
        "Link two ledger ids (e.g. decision -> failure).",
        _schema(
            ["from_id", "to_id", "relation_type"],
            from_id=_STR, to_id=_STR, relation_type=_STR,
        ),
    ),
    "record_failure": (
        "Append a failure. Returns the failure id.",
        _schema(
            ["description"], description=_STR, detected_by=_STR,
            severity=_STR, related_ids=_STR,
        ),
    ),
    "record_recovery": (
        "Append a recovery for a failure id.",
        _schema(
            ["failure_id", "description"], failure_id=_STR,
            description=_STR, recovered_by=_STR,
        ),
    ),
    "record_autonomy_event": (
        "Append an autonomy event (a dimension and what happened).",
        _schema(
            ["dimension", "event_type"], dimension=_STR, event_type=_STR,
            initiated_by=_STR, related_ids=_STR,
        ),
    ),
}


class ToolError(Exception):
    pass


def call_tool(writer: ObserverWriter, name: str, args: dict[str, Any]) -> str:
    """Invoke one writer method; raises (never swallows) on any failure."""
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name!r}")
    spec = TOOLS[name][1]
    missing = [k for k in spec["required"] if args.get(k) in (None, "")]
    if missing:
        raise ToolError(f"{name}: missing required argument(s): {', '.join(missing)}")
    unknown = sorted(set(args) - set(spec["properties"]))
    if unknown:
        raise ToolError(f"{name}: unknown argument(s): {', '.join(unknown)}")
    return str(getattr(writer, name)(**args))


def _result(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(
    writer_factory: Callable[[], ObserverWriter], msg: dict[str, Any]
) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    if req_id is None:  # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "observer", "version": "1.0.0"},
        })
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": [
            {"name": n, "description": d, "inputSchema": s}
            for n, (d, s) in TOOLS.items()
        ]})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        try:
            text = call_tool(writer_factory(), name, params.get("arguments") or {})
            return _result(req_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # loud: surfaced to the model and to stderr
            print(f"observer-mcp: {name} FAILED: {exc!r}", file=sys.stderr, flush=True)
            return _result(req_id, {
                "isError": True,
                "content": [{"type": "text", "text": f"NOT RECORDED: {name} failed: {exc}"}],
            })
    return _error(req_id, -32601, f"method not found: {method}")


def serve(writer_factory: Callable[[], ObserverWriter], stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            reply = _error(None, -32700, f"parse error: {exc}")
        else:
            reply = handle(writer_factory, msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main() -> int:
    holder: dict[str, ObserverWriter] = {}

    def factory() -> ObserverWriter:
        # Reconnect if the connection was dropped (e.g. Postgres restart).
        w = holder.get("w")
        if w is None or w.conn.closed:
            holder["w"] = w = ObserverWriter()
        return w

    try:
        factory()  # fail fast and loudly at startup, not at first decision
    except Exception as exc:
        print(f"observer-mcp: cannot start, Observer DB unavailable: {exc!r}", file=sys.stderr)
        return 1
    serve(factory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
