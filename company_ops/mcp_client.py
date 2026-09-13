from __future__ import annotations

import json
import selectors
import subprocess
import time
from dataclasses import dataclass


class MCPError(RuntimeError):
    pass


@dataclass(frozen=True)
class MCPServer:
    command: tuple[str, ...]
    tool: str
    env: dict[str, str] | None = None
    arguments: dict | None = None
    poll_tool: str | None = None
    poll_interval: float = 2


class MCPClient:
    """Small stdio JSON-RPC MCP client for one request per worker process."""

    def __init__(self, server: MCPServer, timeout: float = 120) -> None:
        if not server.command:
            raise ValueError("MCP server command cannot be empty")
        self.server = server
        self.timeout = timeout

    def call(self, arguments: dict) -> object:
        process = subprocess.Popen(
            self.server.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**__import__("os").environ, **(self.server.env or {})},
        )
        try:
            self._send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "homely-company-ops", "version": "0.1.0"},
            }})
            self._read_response(process, 1)
            self._send(process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            result = self._call_tool(process, 2, self.server.tool, arguments)
            if not self.server.poll_tool:
                return result
            start = self._json_result(result)
            session_id = start.get("sessionId")
            if not session_id:
                raise MCPError("MCP worker did not return a sessionId")
            cursor = 0
            request_id = 3
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                time.sleep(self.server.poll_interval)
                polled = self._call_tool(process, request_id, self.server.poll_tool, {
                    "action": "poll", "sessionId": session_id, "cursor": cursor,
                    "pollOptions": {"includeProgressEvents": False},
                })
                request_id += 1
                data = self._json_result(polled)
                cursor = data.get("nextCursor", cursor)
                if data.get("status") in {"idle", "error", "cancelled"}:
                    return data.get("result", data)
                if data.get("status") == "waiting_permission":
                    raise MCPError("MCP worker requested permission; configure allowedTools explicitly")
            raise MCPError(f"MCP worker session timed out after {self.timeout:g}s")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def _send(self, process: subprocess.Popen[str], message: dict) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def _call_tool(self, process: subprocess.Popen[str], request_id: int, tool: str, arguments: dict) -> object:
        self._send(process, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
            "name": tool, "arguments": arguments,
        }})
        return self._read_response(process, request_id)

    @staticmethod
    def _json_result(result: object) -> dict:
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            for item in result["content"]:
                if item.get("type") == "text":
                    try:
                        decoded = json.loads(item["text"])
                        if isinstance(decoded, dict):
                            return decoded
                    except (KeyError, TypeError, json.JSONDecodeError):
                        pass
        if isinstance(result, dict):
            return result
        raise MCPError("MCP worker returned a non-object result")

    def _read_response(self, process: subprocess.Popen[str], request_id: int) -> object:
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                ready = selector.select(max(0.01, deadline - time.monotonic()))
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line:
                    raise MCPError(f"MCP server exited before response {request_id}")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise MCPError(str(message["error"]))
                return message.get("result")
        finally:
            selector.close()
        raise MCPError(f"MCP request {request_id} timed out after {self.timeout:g}s")
