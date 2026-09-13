#!/usr/bin/env python3
"""
companyd MCP Server — HTTP wrapper exposing companyd Unix socket as MCP protocol.

Listens on configurable port (default 8001), forwards tool calls to companyd
via Unix socket at /var/run/companyd.sock with 10s timeout.
"""

import json
import os
import socket
import sys
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional

SOCKET_PATH = "/var/run/companyd.sock"
SOCKET_TIMEOUT = 10

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "companyd-mcp"

TOOLS = [
    {
        "name": "deploy",
        "description": "Deploy a new generation for a component",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "enum": ["ENGINEERING", "HERMEES"],
                    "description": "Component to deploy",
                },
                "git_sha": {
                    "type": "string",
                    "description": "Git commit SHA to deploy",
                },
                "docker_image": {
                    "type": "string",
                    "description": "Docker image to deploy",
                },
            },
            "required": ["component", "git_sha", "docker_image"],
        },
    },
    {
        "name": "status",
        "description": "Get current generation status",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "enum": ["ENGINEERING", "HERMEES"],
                },
                "generation_name": {
                    "type": "string",
                    "description": "Generation name (e.g. E42, H18)",
                },
            },
            "required": ["component", "generation_name"],
        },
    },
    {
        "name": "rollback",
        "description": "Trigger rollback to previous generation",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "enum": ["ENGINEERING", "HERMEES"],
                },
            },
            "required": ["component"],
        },
    },
    {
        "name": "list_generations",
        "description": "List all generations for a component",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "enum": ["ENGINEERING", "HERMEES"],
                },
            },
            "required": ["component"],
        },
    },
    {
        "name": "get_logs",
        "description": "Retrieve logs from companyd",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "enum": ["ENGINEERING", "HERMEES"],
                },
                "generation_name": {
                    "type": "string",
                },
                "log_type": {
                    "type": "string",
                    "enum": ["build", "runtime", "drain"],
                },
            },
            "required": ["component", "generation_name", "log_type"],
        },
    },
]


def send_to_companyd(command: Dict[str, Any]) -> Dict[str, Any]:
    """Send a JSON command to companyd via Unix socket and return the response."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT)
    try:
        sock.connect(SOCKET_PATH)
        payload = json.dumps(command) + "\n"
        sock.sendall(payload.encode("utf-8"))

        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\n" in response:
                break
        return json.loads(response.decode("utf-8").strip())
    except socket.timeout:
        return {"error": "companyd socket timeout after 10s"}
    except FileNotFoundError:
        return {"error": "companyd socket not found — daemon may not be running"}
    except ConnectionRefusedError:
        return {"error": "companyd socket connection refused — daemon may not be running"}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON from companyd: {e}"}
    finally:
        sock.close()


def handle_tool_call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Route a tool call to the appropriate companyd command."""
    if tool_name == "deploy":
        return send_to_companyd({
            "command": "deploy",
            "component": arguments["component"],
            "git_sha": arguments["git_sha"],
            "docker_image": arguments.get("docker_image", ""),
        })
    elif tool_name == "status":
        return send_to_companyd({
            "command": "status",
            "component": arguments["component"],
            "generation_name": arguments["generation_name"],
        })
    elif tool_name == "rollback":
        return send_to_companyd({
            "command": "rollback",
            "component": arguments["component"],
        })
    elif tool_name == "list_generations":
        return send_to_companyd({
            "command": "list_generations",
            "component": arguments["component"],
        })
    elif tool_name == "get_logs":
        return send_to_companyd({
            "command": "get_logs",
            "component": arguments["component"],
            "generation_name": arguments["generation_name"],
            "log_type": arguments["log_type"],
        })
    else:
        return {"error": f"Unknown tool: {tool_name}"}


class McpHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MCP protocol endpoints."""

    def _send_json(self, status: int, data: Dict[str, Any]):
        """Send a JSON response with the given HTTP status code."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Dict[str, Any]:
        """Read and parse the JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_POST(self):
        """Handle POST requests for MCP endpoints."""
        if self.path == "/mcp/initialize":
            self._handle_initialize()
        elif self.path == "/mcp/tools/list":
            self._handle_tools_list()
        elif self.path == "/mcp/tools/call":
            self._handle_tools_call()
        else:
            self._send_json(404, {"error": f"Unknown endpoint: {self.path}"})

    def _handle_initialize(self):
        """Handle MCP initialize request."""
        self._send_json(200, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {
                "name": SERVER_NAME,
                "version": "1.0.0",
            },
            "capabilities": {
                "tools": {},
            },
        })

    def _handle_tools_list(self):
        """Handle MCP tools/list request."""
        self._send_json(200, {"tools": TOOLS})

    def _handle_tools_call(self):
        """Handle MCP tools/call request."""
        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        tool_name = body.get("name")
        arguments = body.get("arguments", {})

        if not tool_name:
            self._send_json(400, {"error": "Missing 'name' in request body"})
            return

        result = handle_tool_call(tool_name, arguments)

        if "error" in result:
            self._send_json(200, {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": True,
            })
        else:
            self._send_json(200, {
                "content": [{"type": "text", "text": json.dumps(result)}],
            })

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass


def run_server(host: str = "0.0.0.0", port: int = 8001):
    """Start the MCP HTTP server."""
    server = HTTPServer((host, port), McpHandler)
    print(f"companyd MCP server listening on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="companyd MCP Server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", 8001)))
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)
