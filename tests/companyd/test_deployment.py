"""Tests for Deployment record creation and updates."""

import datetime
import uuid

import pytest

from companyd.companyd import (
    Component,
    Deployment,
    Generation,
    GenerationStatus,
)


class TestDeploymentCreation:
    """Test Deployment instantiation."""

    def test_create_deployment(self, deployment):
        assert deployment.id is not None
        assert deployment.component == Component.ENGINEERING
        assert deployment.generation.name == "E42"
        assert deployment.git_sha == "abc123def456"
        assert deployment.docker_image == "engineering:E42"
        assert deployment.status == GenerationStatus.BUILDING

    def test_deployment_id_is_unique(self):
        d1_id = str(uuid.uuid4())
        d2_id = str(uuid.uuid4())
        assert d1_id != d2_id

    def test_optional_fields_default(self, deployment):
        assert deployment.test_passed is False
        assert deployment.test_summary == ""
        assert deployment.previous_generation is None
        assert deployment.rollback_required is False
        assert deployment.rollback_reason == ""

    def test_timestamps_optional(self):
        gen = Generation(
            name="E1",
            component=Component.ENGINEERING,
            status=GenerationStatus.BUILDING,
            created_at=datetime.datetime.utcnow(),
        )
        d = Deployment(
            id="test-id",
            component=Component.ENGINEERING,
            generation=gen,
            git_sha="abc",
            docker_image="engineering:E1",
            status=GenerationStatus.BUILDING,
            created_at=datetime.datetime.utcnow(),
        )
        assert d.build_start is None
        assert d.build_end is None
        assert d.test_start is None
        assert d.test_end is None
        assert d.activate_time is None
        assert d.drain_start is None
        assert d.drain_end is None


class TestDeploymentUpdates:
    """Test update_status and record_event."""

    def test_update_status(self, deployment):
        deployment.update_status(GenerationStatus.STARTING)
        assert deployment.status == GenerationStatus.STARTING

    def test_update_status_full_lifecycle(self, deployment):
        for status in [
            GenerationStatus.STARTING,
            GenerationStatus.WARMING,
            GenerationStatus.TESTING,
            GenerationStatus.READY,
            GenerationStatus.ACTIVE,
            GenerationStatus.DRAINING,
            GenerationStatus.RETIRED,
        ]:
            deployment.update_status(status)
        assert deployment.status == GenerationStatus.RETIRED


class TestDeploymentToDict:
    """Test serialization."""

    def test_to_dict(self, deployment):
        d = deployment.to_dict()
        assert d["id"] == deployment.id
        assert d["component"] == "engineering"
        assert d["generation"] == "E42"
        assert d["git_sha"] == "abc123def456"
        assert d["docker_image"] == "engineering:E42"
        assert d["status"] == "BUILDING"
        assert d["test_passed"] is False

    def test_to_dict_with_timestamps(self):
        gen = Generation(
            name="E2",
            component=Component.ENGINEERING,
            status=GenerationStatus.ACTIVE,
            created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
        )
        d = Deployment(
            id="d1",
            component=Component.ENGINEERING,
            generation=gen,
            git_sha="abc",
            docker_image="engineering:E2",
            status=GenerationStatus.ACTIVE,
            created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
            build_start=datetime.datetime(2026, 1, 15, 10, 0, 30),
            build_end=datetime.datetime(2026, 1, 15, 10, 2, 0),
            test_start=datetime.datetime(2026, 1, 15, 10, 2, 30),
            test_end=datetime.datetime(2026, 1, 15, 10, 4, 0),
            activate_time=datetime.datetime(2026, 1, 15, 10, 5, 0),
        )
        result = d.to_dict()
        assert result["build_start"] == "2026-01-15T10:00:30"
        assert result["build_end"] == "2026-01-15T10:02:00"
        assert result["activate_time"] == "2026-01-15T10:05:00"
        assert result["drain_start"] is None


class TestDeploymentRollback:
    """Test rollback fields."""

    def test_rollback_defaults(self, deployment):
        assert deployment.rollback_required is False
        assert deployment.rollback_reason == ""

    def test_rollback_flag(self, deployment):
        deployment.rollback_required = True
        deployment.rollback_reason = "health check failed"
        assert deployment.rollback_required is True
        assert deployment.rollback_reason == "health check failed"
