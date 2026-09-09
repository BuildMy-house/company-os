"""Shared fixtures for companyd unit tests."""

import datetime
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from companyd.companyd import (
    Component,
    Deployment,
    DockerLifecycle,
    Generation,
    GenerationStatus,
    HealthCheck,
    RuntimeControl,
    SyntheticTests,
)


@pytest.fixture
def gen_building():
    """A Generation in BUILDING state."""
    return Generation(
        name="E42",
        component=Component.ENGINEERING,
        status=GenerationStatus.BUILDING,
        created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
    )


@pytest.fixture
def gen_active():
    """A Generation in ACTIVE state."""
    return Generation(
        name="E42",
        component=Component.ENGINEERING,
        status=GenerationStatus.ACTIVE,
        created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
    )


@pytest.fixture
def gen_retired():
    """A Generation in RETIRED state."""
    return Generation(
        name="E41",
        component=Component.ENGINEERING,
        status=GenerationStatus.RETIRED,
        created_at=datetime.datetime(2026, 1, 14, 9, 0, 0),
    )


@pytest.fixture
def hermees_gen():
    """A Hermees Generation."""
    return Generation(
        name="H18",
        component=Component.HERMEES,
        status=GenerationStatus.BUILDING,
        created_at=datetime.datetime(2026, 1, 15, 11, 0, 0),
    )


@pytest.fixture
def deployment():
    """A Deployment record."""
    gen = Generation(
        name="E42",
        component=Component.ENGINEERING,
        status=GenerationStatus.BUILDING,
        created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
    )
    return Deployment(
        id=str(uuid.uuid4()),
        component=Component.ENGINEERING,
        generation=gen,
        git_sha="abc123def456",
        docker_image="engineering:E42",
        status=GenerationStatus.BUILDING,
        created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
    )


@pytest.fixture
def docker_lifecycle():
    """DockerLifecycle instance."""
    return DockerLifecycle()


@pytest.fixture
def health_check():
    """HealthCheck instance."""
    return HealthCheck()


@pytest.fixture
def synthetic_tests():
    """SyntheticTests instance."""
    return SyntheticTests()


@pytest.fixture
def runtime_control(tmp_path):
    """RuntimeControl backed by a temp file."""
    state_file = tmp_path / "runtime_control.json"
    return RuntimeControl(state_file=state_file)
