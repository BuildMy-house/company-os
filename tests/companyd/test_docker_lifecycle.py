"""Tests for DockerLifecycle with mocked subprocess."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from companyd.companyd import (
    Component,
    DockerError,
    DockerLifecycle,
    Generation,
    GenerationStatus,
)
import datetime


@pytest.fixture
def eng_gen():
    return Generation(
        name="E42",
        component=Component.ENGINEERING,
        status=GenerationStatus.BUILDING,
        created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
    )


@pytest.fixture
def hrm_gen():
    return Generation(
        name="H18",
        component=Component.HERMEES,
        status=GenerationStatus.BUILDING,
        created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
    )


class TestDockerBuild:
    """Test DockerLifecycle.build()."""

    def test_build_constructs_correct_command(self, docker_lifecycle, eng_gen):
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("companyd.companyd.subprocess.run", return_value=mock_result) as mock_run:
            tag = docker_lifecycle.build(
                Component.ENGINEERING,
                "abc123",
                "E43",
                dockerfile=Path("/tmp/Dockerfile.engineering"),
            )
            assert tag == "engineering:E43"
            args = mock_run.call_args[0][0]
            assert args[0] == "docker"
            assert args[1] == "build"
            assert "-t" in args
            assert "engineering:E43" in args
            assert "--build-arg" in args
            assert "GIT_SHA=abc123" in args

    def test_build_raises_on_failure(self, docker_lifecycle):
        mock_result = MagicMock(returncode=1, stdout="", stderr="build error")
        with patch("companyd.companyd.subprocess.run", return_value=mock_result):
            with pytest.raises(DockerError, match="Failed to build"):
                docker_lifecycle.build(
                    Component.ENGINEERING,
                    "abc123",
                    "E43",
                    dockerfile=Path("/tmp/Dockerfile.engineering"),
                )

    def test_build_raises_on_timeout(self, docker_lifecycle):
        with patch(
            "companyd.companyd.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=1800),
        ):
            with pytest.raises(DockerError, match="Build timeout"):
                docker_lifecycle.build(
                    Component.ENGINEERING,
                    "abc123",
                    "E43",
                    dockerfile=Path("/tmp/Dockerfile.engineering"),
                )

    def test_build_hermees(self, docker_lifecycle, hrm_gen):
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("companyd.companyd.subprocess.run", return_value=mock_result) as mock_run:
            tag = docker_lifecycle.build(
                Component.HERMEES,
                "d821ab",
                "H19",
                dockerfile=Path("/tmp/Dockerfile.hermees"),
            )
            assert tag == "hermees:H19"


class TestDockerStart:
    """Test DockerLifecycle.start()."""

    def test_start_constructs_correct_command(self, docker_lifecycle, eng_gen):
        mock_result = MagicMock(returncode=0, stdout="abc123container\n", stderr="")
        with patch("companyd.companyd.subprocess.run", return_value=mock_result) as mock_run:
            container_id = docker_lifecycle.start(eng_gen, mode="WARMING", port=8080)
            assert container_id == "abc123container"
            args = mock_run.call_args[0][0]
            assert "docker" in args[0]
            assert "run" in args
            assert "-d" in args
            assert "GENERATION=E42" in args
            assert "MODE=WARMING" in args
            assert "COMPONENT=engineering" in args
            assert "8080:8000" in args

    def test_start_raises_on_failure(self, docker_lifecycle, eng_gen):
        mock_result = MagicMock(returncode=1, stdout="", stderr="start error")
        with patch("companyd.companyd.subprocess.run", return_value=mock_result):
            with pytest.raises(DockerError, match="Failed to start"):
                docker_lifecycle.start(eng_gen)

    def test_start_raises_on_timeout(self, docker_lifecycle, eng_gen):
        with patch(
            "companyd.companyd.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=30),
        ):
            with pytest.raises(DockerError, match="Start timeout"):
                docker_lifecycle.start(eng_gen)


class TestDockerStop:
    """Test DockerLifecycle.stop()."""

    def test_stop_calls_stop_and_rm(self, docker_lifecycle, eng_gen):
        mock_result = MagicMock(returncode=0)
        with patch("companyd.companyd.subprocess.run", return_value=mock_result) as mock_run:
            docker_lifecycle.stop(eng_gen)
            calls = mock_run.call_args_list
            stop_call = calls[0][0][0]
            rm_call = calls[1][0][0]
            assert stop_call == ["docker", "stop", "engineering-E42"]
            assert rm_call == ["docker", "rm", "engineering-E42"]

    def test_stop_does_not_raise_on_error(self, docker_lifecycle, eng_gen):
        mock_result = MagicMock(side_effect=Exception("docker error"))
        with patch("companyd.companyd.subprocess.run", return_value=mock_result):
            # Should not raise — stop swallows errors
            docker_lifecycle.stop(eng_gen)


class TestDockerLogs:
    """Test DockerLifecycle.logs()."""

    def test_logs_returns_stdout(self, docker_lifecycle, eng_gen):
        mock_result = MagicMock(returncode=0, stdout="line1\nline2\n", stderr="")
        with patch("companyd.companyd.subprocess.run", return_value=mock_result):
            output = docker_lifecycle.logs(eng_gen, lines=50)
            assert output == "line1\nline2\n"

    def test_logs_returns_error_on_failure(self, docker_lifecycle, eng_gen):
        mock_result = MagicMock(returncode=1, stdout="", stderr="no container")
        with patch("companyd.companyd.subprocess.run", return_value=mock_result):
            output = docker_lifecycle.logs(eng_gen)
            assert "Failed to get logs" in output

    def test_logs_handles_exception(self, docker_lifecycle, eng_gen):
        with patch(
            "companyd.companyd.subprocess.run",
            side_effect=Exception("connection refused"),
        ):
            output = docker_lifecycle.logs(eng_gen)
            assert "Error retrieving logs" in output
