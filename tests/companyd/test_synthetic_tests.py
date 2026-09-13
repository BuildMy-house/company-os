"""Tests for SyntheticTests framework."""

from unittest.mock import MagicMock, patch

import pytest

from companyd.companyd import (
    Component,
    Generation,
    GenerationStatus,
    SyntheticTests,
)
import datetime


@pytest.fixture
def eng_gen():
    return Generation(
        name="E42",
        component=Component.ENGINEERING,
        status=GenerationStatus.TESTING,
        created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
    )


@pytest.fixture
def hrm_gen():
    return Generation(
        name="H18",
        component=Component.HERMEES,
        status=GenerationStatus.TESTING,
        created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
    )


class TestEngineeringSelfTest:
    """Test engineering_self_test."""

    def test_returns_dict_with_required_keys(self, synthetic_tests, eng_gen):
        mock_health = {"status": "OK", "response_time_ms": 5.0, "error_message": None}
        mock_logs = "log line 1\nlog line 2"

        with patch("companyd.companyd.HealthCheck.check", return_value=mock_health):
            with patch("companyd.companyd.DockerLifecycle.logs", return_value=mock_logs):
                result = synthetic_tests.engineering_self_test(eng_gen)

        assert result["test_name"] == "engineering_self_test"
        assert result["generation"] == "E42"
        assert "passed" in result
        assert "details" in result
        assert isinstance(result["checks"], dict)

    def test_passes_when_health_ok(self, synthetic_tests, eng_gen):
        mock_health = {"status": "OK", "response_time_ms": 5.0, "error_message": None}
        with patch("companyd.companyd.HealthCheck.check", return_value=mock_health):
            with patch("companyd.companyd.DockerLifecycle.logs", return_value="logs"):
                result = synthetic_tests.engineering_self_test(eng_gen)
        assert result["passed"] is True

    def test_fails_when_health_fails(self, synthetic_tests, eng_gen):
        mock_health = {"status": "FAILED", "response_time_ms": 5.0, "error_message": "refused"}
        with patch("companyd.companyd.HealthCheck.check", return_value=mock_health):
            with patch("companyd.companyd.DockerLifecycle.logs", return_value="logs"):
                result = synthetic_tests.engineering_self_test(eng_gen)
        assert result["passed"] is False

    def test_exception_caught_and_recorded(self, synthetic_tests, eng_gen):
        with patch("companyd.companyd.HealthCheck.check", side_effect=RuntimeError("boom")):
            result = synthetic_tests.engineering_self_test(eng_gen)
        assert result["passed"] is False
        assert "boom" in result["details"]


class TestHermeesReconciliation:
    """Test hermees_reconciliation."""

    def test_returns_dict_with_required_keys(self, synthetic_tests, hrm_gen):
        mock_health = {"status": "OK", "response_time_ms": 5.0, "error_message": None}
        with patch("companyd.companyd.HealthCheck.check", return_value=mock_health):
            result = synthetic_tests.hermees_reconciliation(hrm_gen)

        assert result["test_name"] == "hermees_reconciliation"
        assert result["generation"] == "H18"
        assert "handover_accepted" in result
        assert "details" in result
        assert isinstance(result["checks"], dict)

    def test_accepts_when_health_ok(self, synthetic_tests, hrm_gen):
        mock_health = {"status": "OK", "response_time_ms": 5.0, "error_message": None}
        with patch("companyd.companyd.HealthCheck.check", return_value=mock_health):
            result = synthetic_tests.hermees_reconciliation(hrm_gen)
        assert result["handover_accepted"] is True

    def test_rejects_when_health_fails(self, synthetic_tests, hrm_gen):
        mock_health = {"status": "FAILED", "response_time_ms": 5.0, "error_message": "timeout"}
        with patch("companyd.companyd.HealthCheck.check", return_value=mock_health):
            result = synthetic_tests.hermees_reconciliation(hrm_gen)
        assert result["handover_accepted"] is False

    def test_exception_caught_and_recorded(self, synthetic_tests, hrm_gen):
        with patch("companyd.companyd.HealthCheck.check", side_effect=RuntimeError("db down")):
            result = synthetic_tests.hermees_reconciliation(hrm_gen)
        assert result["handover_accepted"] is False
        assert "db down" in result["details"]
