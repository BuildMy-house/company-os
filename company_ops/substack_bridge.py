"""Narrow, allowlisted bridge to the pinned substack-mcp server.

substack-mcp (github.com/marcomoauro/substack-mcp, pinned at v1.3.2 / commit
c148a3b176) is NOT an official Substack API: it drives the web endpoints with
the logged-in session cookie (SUBSTACK_SESSION_TOKEN), which is a password.
This module is the only place that spawns it. It speaks MCP over stdio
(newline-delimited JSON-RPC) and exposes exactly three kinds of call:

- READ_TOOLS: aggregate stats and the post list (used by the ingester).
- DRAFT_TOOLS: create a draft (publisher agent, no approval needed).
- PUBLISH_TOOLS: publish a draft (publisher agent, behind the approval gate
  enforced by the sidecar; ``send`` is forced to false here so no email is
  ever sent from code).

Everything else (export_subscribers, get_analytics, upload_image, the
social/reader tools, ...) is refused here, so subscriber-level data cannot be
requested through this module at all.
"""

from __future__ import annotations

import json
import os
import subprocess

READ_TOOLS = frozenset({"get_publication_stats", "get_post_stats", "list_posts"})
DRAFT_TOOLS = frozenset({"create_draft_post"})
PUBLISH_TOOLS = frozenset({"publish_draft"})
ALLOWED_TOOLS = READ_TOOLS | DRAFT_TOOLS | PUBLISH_TOOLS

SUBSTACK_ENV = ("SUBSTACK_PUBLICATION_URL", "SUBSTACK_SESSION_TOKEN", "SUBSTACK_USER_ID")
CALL_TIMEOUT = 60


class NotAllowed(Exception):
    """Tool is not exposed by this bridge."""


class AuthFailed(Exception):
    """Substack rejected the session cookie (expired or revoked)."""


def _is_auth_error(text: str) -> bool:
    return any(f" {c} " in f" {text} " for c in ("401", "403"))


def sanitize_args(tool: str, args: dict) -> dict:
    if tool not in ALLOWED_TOOLS:
        raise NotAllowed(f"substack tool {tool!r} is not exposed")
    args = dict(args or {})
    if tool == "publish_draft":
        args["send"] = False  # never email subscribers from code
    return args


def call_tool(tool: str, args: dict, env: dict, server_cmd: list[str] | None = None) -> dict:
    """Spawn the pinned server, initialize, call one allowlisted tool, return its JSON result."""
    args = sanitize_args(tool, args)
    missing = [n for n in SUBSTACK_ENV if not env.get(n)]
    if missing:
        raise RuntimeError("missing env: " + ", ".join(missing))
    cmd = server_cmd or json.loads(env.get("SUBSTACK_MCP_CMD") or '["substack-mcp"]')
    child_env = {k: env[k] for k in (*SUBSTACK_ENV, "PATH", "HOME") if k in env}
    child_env["SUBSTACK_MCP_LOG_LEVEL"] = "error"  # keep response bodies out of logs
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "company-ops-marketing", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}},
    ]
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True,
        text=True, timeout=CALL_TIMEOUT, env=child_env,
    )
    reply = None
    for line in proc.stdout.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == 2:
            reply = m
    if reply is None:
        raise RuntimeError(f"substack {tool}: no reply (exit {proc.returncode})")
    if "error" in reply:
        raise RuntimeError(f"substack {tool}: {str(reply['error'])[:200]}")
    result = reply.get("result", {})
    text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        if _is_auth_error(text):
            raise AuthFailed(f"substack {tool}: session cookie rejected (expired?): {text[:120]}")
        raise RuntimeError(f"substack {tool}: {text[:200]}")
    try:
        return json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"substack {tool}: non-JSON result") from e
