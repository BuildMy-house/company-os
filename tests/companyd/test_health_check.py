"""Tests for HealthCheck format and response handling."""

from unittest.mock import MagicMock, patch

import pytest

from companyd.companyd import HealthCheck


class TestHealthCheckSuccess:
    """Test successful health check responses."""

    def test_check_200_returns_ok(self, health_check):
        mock_response = MagicMock()
        mock_response.status = 200
        with patch("companyd.companyd.urllib.request.urlopen", return_value=mock_response):
            result = health_check.check()
            assert result["status"] == "OK"
            assert result["error_message"] is None
            assert "response_time_ms" in result

    def test_response_time_is_positive(self, health_check):
        mock_response = MagicMock()
        mock_response.status = 200
        with patch("companyd.companyd.urllib.request.urlopen", return_value=mock_response):
            result = health_check.check()
            assert result["response_time_ms"] >= 0


class TestHealthCheckFailure:
    """Test failed health check responses."""

    def test_non_200_returns_failed(self, health_check):
        mock_response = MagicMock()
        mock_response.status = 503
        with patch("companyd.companyd.urllib.request.urlopen", return_value=mock_response):
            result = health_check.check()
            assert result["status"] == "FAILED"
            assert "503" in result["error_message"]

    def test_timeout_returns_failed(self, health_check):
        import urllib.error

        with patch(
            "companyd.companyd.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            result = health_check.check()
            assert result["status"] == "FAILED"
            assert "timed out" in result["error_message"]

    def test_connection_refused_returns_failed(self, health_check):
        import urllib.error

        with patch(
            "companyd.companyd.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = health_check.check()
            assert result["status"] == "FAILED"
            assert "Connection refused" in result["error_message"]

    def test_generic_exception_returns_failed(self, health_check):
        with patch(
            "companyd.companyd.urllib.request.urlopen",
            side_effect=RuntimeError("unexpected"),
        ):
            result = health_check.check()
            assert result["status"] == "FAILED"
            assert "unexpected" in result["error_message"]


class TestHealthCheckParameters:
    """Test custom host, port, timeout."""

    def test_custom_host_and_port(self, health_check):
        mock_response = MagicMock()
        mock_response.status = 200
        with patch("companyd.companyd.urllib.request.urlopen", return_value=mock_response) as mock_open:
            health_check.check(host="10.0.0.1", port=9000, timeout_sec=5)
            url = mock_open.call_args[0][0]
            assert url == "http://10.0.0.1:9000/health"

    def test_response_time_measured(self, health_check):
        mock_response = MagicMock()
        mock_response.status = 200
        with patch("companyd.companyd.urllib.request.urlopen", return_value=mock_response):
            result = health_check.check()
            assert isinstance(result["response_time_ms"], float)
