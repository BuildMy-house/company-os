"""Tests for companyd MCP server HTTP endpoints and tool dispatch."""

import json
import socket
import sys
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest

_company_ops = str(Path(__file__).resolve().parent.parent.parent)
if _company_ops not in sys.path:
    sys.path.insert(0, _company_ops)

from companyd_mcp_server import McpHandler, TOOLS, SOCKET_TIMEOUT


def _make_mock_socket(response: dict):
    """Create a mock socket that returns a JSON response."""
    mock = MagicMock(spec=socket.socket)
    response_bytes = (json.dumps(response) + "\n").encode("utf-8")
    mock.recv.side_effect = [response_bytes, b""]
    return mock


@pytest.fixture
def server():
    """Start MCP server on a random port and yield (host, port)."""
    from http.server import HTTPServer

    httpd = HTTPServer(("127.0.0.1", 0), McpHandler)
    host, port = httpd.server_address
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield host, port
    httpd.shutdown()


def _post(conn, path, body=None):
    """Send a POST request and return parsed JSON."""
    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else b""
    conn.request("POST", path, body=data, headers=headers)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read().decode())


class TestInitialize:
    def test_initialize_returns_protocol_version(self, server):
        host, port = server
        conn = HTTPConnection(host, port, timeout=5)
        status, body = _post(conn, "/mcp/initialize")
        conn.close()
        assert status == 200
        assert body["protocolVersion"] == "2024-11-05"
        assert body["serverInfo"]["name"] == "companyd-mcp"
        assert "tools" in body["capabilities"]


class TestToolsList:
    def test_tools_list_returns_all_five(self, server):
        host, port = server
        conn = HTTPConnection(host, port, timeout=5)
        status, body = _post(conn, "/mcp/tools/list")
        conn.close()
        assert status == 200
        assert len(body["tools"]) == 5
        tool_names = {t["name"] for t in body["tools"]}
        assert tool_names == {"deploy", "status", "rollback", "list_generations", "get_logs"}

    def test_tools_list_has_descriptions(self, server):
        host, port = server
        conn = HTTPConnection(host, port, timeout=5)
        status, body = _post(conn, "/mcp/tools/list")
        conn.close()
        for tool in body["tools"]:
            assert "description" in tool
            assert len(tool["description"]) > 0
            assert "inputSchema" in tool


class TestDeployTool:
    def test_deploy_tool_calls_socket(self, server):
        host, port = server
        mock_resp = {"generation_id": "H19", "status": "BUILDING", "success": True}
        with patch("companyd_mcp_server.send_to_companyd", return_value=mock_resp):
            conn = HTTPConnection(host, port, timeout=5)
            status, body = _post(conn, "/mcp/tools/call", {
                "name": "deploy",
                "arguments": {
                    "component": "HERMEES",
                    "git_sha": "abc123",
                    "docker_image": "hermees:H19",
                },
            })
            conn.close()
            assert status == 200
            assert body.get("isError") is not True
            content = json.loads(body["content"][0]["text"])
            assert content["generation_id"] == "H19"
            assert content["success"] is True


class TestStatusTool:
    def test_status_tool_calls_socket(self, server):
        host, port = server
        mock_resp = {"component": "ENGINEERING", "active_generation": "E42", "status": "ACTIVE"}
        with patch("companyd_mcp_server.send_to_companyd", return_value=mock_resp):
            conn = HTTPConnection(host, port, timeout=5)
            status, body = _post(conn, "/mcp/tools/call", {
                "name": "status",
                "arguments": {
                    "component": "ENGINEERING",
                    "generation_name": "E42",
                },
            })
            conn.close()
            assert status == 200
            content = json.loads(body["content"][0]["text"])
            assert content["active_generation"] == "E42"
            assert content["status"] == "ACTIVE"


class TestSocketTimeout:
    def test_socket_timeout_returns_error(self, server):
        host, port = server
        with patch("companyd_mcp_server.send_to_companyd",
                    return_value={"error": "companyd socket timeout after 10s"}):
            conn = HTTPConnection(host, port, timeout=5)
            status, body = _post(conn, "/mcp/tools/call", {
                "name": "status",
                "arguments": {
                    "component": "ENGINEERING",
                    "generation_name": "E42",
                },
            })
            conn.close()
            assert status == 200
            assert body["isError"] is True
            content = json.loads(body["content"][0]["text"])
            assert "timeout" in content["error"].lower()


class TestErrorHandling:
    def test_missing_tool_name(self, server):
        host, port = server
        conn = HTTPConnection(host, port, timeout=5)
        status, body = _post(conn, "/mcp/tools/call", {"arguments": {}})
        conn.close()
        assert status == 400
        assert "error" in body

    def test_unknown_tool(self, server):
        host, port = server
        with patch("companyd_mcp_server.send_to_companyd",
                    return_value={"error": "Unknown tool: nonexistent"}):
            conn = HTTPConnection(host, port, timeout=5)
            status, body = _post(conn, "/mcp/tools/call", {
                "name": "nonexistent",
                "arguments": {},
            })
            conn.close()
            assert status == 200
            assert body["isError"] is True

    def test_unknown_endpoint(self, server):
        host, port = server
        conn = HTTPConnection(host, port, timeout=5)
        status, body = _post(conn, "/mcp/unknown")
        conn.close()
        assert status == 404
        assert "error" in body


class TestRollbackTool:
    def test_rollback_tool(self, server):
        host, port = server
        mock_resp = {"success": True, "previous_generation": "H17"}
        with patch("companyd_mcp_server.send_to_companyd", return_value=mock_resp):
            conn = HTTPConnection(host, port, timeout=5)
            status, body = _post(conn, "/mcp/tools/call", {
                "name": "rollback",
                "arguments": {"component": "HERMEES"},
            })
            conn.close()
            assert status == 200
            content = json.loads(body["content"][0]["text"])
            assert content["success"] is True
            assert content["previous_generation"] == "H17"


class TestListGenerationsTool:
    def test_list_generations_tool(self, server):
        host, port = server
        mock_resp = {"generations": [{"name": "E42", "status": "ACTIVE"}, {"name": "E41", "status": "RETIRED"}]}
        with patch("companyd_mcp_server.send_to_companyd", return_value=mock_resp):
            conn = HTTPConnection(host, port, timeout=5)
            status, body = _post(conn, "/mcp/tools/call", {
                "name": "list_generations",
                "arguments": {"component": "ENGINEERING"},
            })
            conn.close()
            assert status == 200
            content = json.loads(body["content"][0]["text"])
            assert len(content["generations"]) == 2


class TestGetLogsTool:
    def test_get_logs_tool(self, server):
        host, port = server
        mock_resp = {"logs": "2026-01-15 build started\n2026-01-15 build complete"}
        with patch("companyd_mcp_server.send_to_companyd", return_value=mock_resp):
            conn = HTTPConnection(host, port, timeout=5)
            status, body = _post(conn, "/mcp/tools/call", {
                "name": "get_logs",
                "arguments": {
                    "component": "ENGINEERING",
                    "generation_name": "E42",
                    "log_type": "build",
                },
            })
            conn.close()
            assert status == 200
            content = json.loads(body["content"][0]["text"])
            assert "build started" in content["logs"]
