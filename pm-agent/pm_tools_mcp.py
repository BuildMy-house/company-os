"""Stdio MCP server exposing pm-agent's two tools to the Hermes runtime.

Newline-delimited JSON-RPC 2.0 (same framing as scripts/hive-member-mcp.js).
Hermes spawns this as a child process configured in
pm-agent/hermes-config.yaml; it is the only MCP server pm-agent has in v1.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import tools as pm_tools
from company_ops.pm_store import PmStore

PROTOCOL_VERSION = "2024-11-05"

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "find_similar_signal",
        "description": (
            "Look up whether product feedback on a topic was already reported. "
            "Read-only. Returns occurrence_count and status if known."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["topic"],
            "properties": {
                "topic": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Short normalised topic, e.g. 'dark mode' or 'crash on save'.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "file_feedback_work",
        "description": (
            "Record the end user's feedback on a topic. Deduplicates against known "
            "signals and files a work item with the team only for a new signal or "
            "one past the escalation threshold. Subject to rate limits. Returns an "
            "outcome: filed_new, already_known, escalated, recorded_unfiled, or "
            "rate_limited."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["topic", "summary"],  # all other fields optional
            "properties": {
                "topic": {"type": "string", "maxLength": 200},
                "summary": {
                    "type": "string",
                    "maxLength": 600,
                    "description": "One or two neutral sentences describing the feedback.",
                },
                "kind": {"type": "string", "enum": ["bug", "feature"]},
                "what_happened": {"type": "string", "maxLength": 400, "description": "Bug: what actually happened, in the user's words."},
                "expected": {"type": "string", "maxLength": 300, "description": "Bug: what they expected instead."},
                "steps": {"type": "string", "maxLength": 500, "description": "Bug only: short numbered steps to reproduce, in plain words."},
                "area": {"type": "string", "maxLength": 80, "description": "Where in the app, e.g. 2D plan, 3D view, furniture catalog, export, saving."},
                "frequency": {"type": "string", "maxLength": 60, "description": "always, sometimes, or once."},
                "severity": {"type": "string", "maxLength": 100, "description": "Impact in user words: blocked, annoying, cosmetic."},
                "environment": {"type": "string", "maxLength": 150, "description": "Device/browser, only if the user volunteered it."},
                "workaround": {"type": "string", "maxLength": 300, "description": "Workaround the user uses today, if any."},
                "goal": {"type": "string", "maxLength": 300, "description": "Feature only: what the user wants to be able to do."},
                "problem": {"type": "string", "maxLength": 300, "description": "Feature only: what is hard or missing today."},
                "who_when": {"type": "string", "maxLength": 250, "description": "Feature only: who needs it and in what situation."},
                "importance": {"type": "string", "maxLength": 100, "description": "Feature only: nice-to-have, important, or blocking, in user words."},
                "example": {"type": "string", "maxLength": 400, "description": "Feature only: how they imagine it working, in plain words."},
            },
            "additionalProperties": False,
        },
    },
]


def handle(request: dict[str, Any], tools: pm_tools.PmTools) -> dict[str, Any] | None:
    method = request.get("method")
    req_id = request.get("id")
    if req_id is None:
        return None  # notification
    if method == "initialize":
        params = request.get("params") or {}
        result: dict[str, Any] = {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "pm-tools", "version": "1.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOL_SCHEMAS}
    elif method == "tools/call":
        params = request.get("params") or {}
        arguments = params.get("arguments")
        outcome = tools.call(
            str(params.get("name")), arguments if isinstance(arguments, dict) else {}
        )
        result = {
            "content": [{"type": "text", "text": json.dumps(outcome)}],
            "isError": "error" in outcome,
        }
    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    store = PmStore(os.environ.get("PM_DATABASE_URL"))
    tools = pm_tools.PmTools(
        store,
        os.environ.get("HIVE_URL", "http://hive-coordinator:4100"),
        os.environ.get("PM_CONTEXT_PATH", pm_tools.DEFAULT_CONTEXT_PATH),
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle(request, tools) if isinstance(request, dict) else None
        except ValueError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
