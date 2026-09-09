"""Tests for Generation state machine."""

import datetime

import pytest

from companyd.companyd import (
    Component,
    Generation,
    GenerationStatus,
    GenerationStateError,
)


class TestGenerationCreation:
    """Test Generation instantiation."""

    def test_create_generation(self, gen_building):
        assert gen_building.name == "E42"
        assert gen_building.component == Component.ENGINEERING
        assert gen_building.status == GenerationStatus.BUILDING
        assert gen_building.created_at == datetime.datetime(2026, 1, 15, 10, 0, 0)

    def test_create_hermees_generation(self, hermees_gen):
        assert hermees_gen.name == "H18"
        assert hermees_gen.component == Component.HERMEES
        assert hermees_gen.status == GenerationStatus.BUILDING

    def test_optional_timestamps_default_none(self, gen_building):
        assert gen_building.build_start is None
        assert gen_building.build_end is None
        assert gen_building.test_start is None
        assert gen_building.test_end is None
        assert gen_building.activate_time is None
        assert gen_building.drain_start is None
        assert gen_building.drain_end is None


class TestGenerationTransition:
    """Test valid and invalid state transitions."""

    def test_happy_path_full_lifecycle(self, gen_building):
        """BUILDING → STARTING → WARMING → TESTING → READY → ACTIVE → DRAINING → RETIRED."""
        gen_building.transition(GenerationStatus.STARTING)
        assert gen_building.status == GenerationStatus.STARTING

        gen_building.transition(GenerationStatus.WARMING)
        assert gen_building.status == GenerationStatus.WARMING

        gen_building.transition(GenerationStatus.TESTING)
        assert gen_building.status == GenerationStatus.TESTING

        gen_building.transition(GenerationStatus.READY)
        assert gen_building.status == GenerationStatus.READY

        gen_building.transition(GenerationStatus.ACTIVE)
        assert gen_building.status == GenerationStatus.ACTIVE

        gen_building.transition(GenerationStatus.DRAINING)
        assert gen_building.status == GenerationStatus.DRAINING

        gen_building.transition(GenerationStatus.RETIRED)
        assert gen_building.status == GenerationStatus.RETIRED

    def test_build_failure_path(self, gen_building):
        """BUILDING → BUILD_FAILED → ROLLED_BACK."""
        gen_building.transition(GenerationStatus.BUILD_FAILED)
        assert gen_building.status == GenerationStatus.BUILD_FAILED

        gen_building.transition(GenerationStatus.ROLLED_BACK)
        assert gen_building.status == GenerationStatus.ROLLED_BACK

    def test_test_failure_path(self, gen_building):
        """BUILDING → STARTING → WARMING → TESTING → TEST_FAILED → ROLLED_BACK."""
        gen_building.transition(GenerationStatus.STARTING)
        gen_building.transition(GenerationStatus.WARMING)
        gen_building.transition(GenerationStatus.TESTING)
        gen_building.transition(GenerationStatus.TEST_FAILED)
        assert gen_building.status == GenerationStatus.TEST_FAILED

        gen_building.transition(GenerationStatus.ROLLED_BACK)
        assert gen_building.status == GenerationStatus.ROLLED_BACK

    def test_activation_failed_path(self, gen_building):
        """BUILDING → STARTING → WARMING → TESTING → READY → ACTIVATION_FAILED → ROLLED_BACK."""
        gen_building.transition(GenerationStatus.STARTING)
        gen_building.transition(GenerationStatus.WARMING)
        gen_building.transition(GenerationStatus.TESTING)
        gen_building.transition(GenerationStatus.READY)
        gen_building.transition(GenerationStatus.ACTIVATION_FAILED)
        assert gen_building.status == GenerationStatus.ACTIVATION_FAILED

        gen_building.transition(GenerationStatus.ROLLED_BACK)
        assert gen_building.status == GenerationStatus.ROLLED_BACK

    def test_drain_timeout_path(self, gen_building):
        """Full lifecycle then ACTIVE → DRAINING → DRAIN_TIMEOUT → RETIRED."""
        for s in [
            GenerationStatus.STARTING,
            GenerationStatus.WARMING,
            GenerationStatus.TESTING,
            GenerationStatus.READY,
            GenerationStatus.ACTIVE,
            GenerationStatus.DRAINING,
        ]:
            gen_building.transition(s)

        gen_building.transition(GenerationStatus.DRAIN_TIMEOUT)
        assert gen_building.status == GenerationStatus.DRAIN_TIMEOUT

        gen_building.transition(GenerationStatus.RETIRED)
        assert gen_building.status == GenerationStatus.RETIRED

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (GenerationStatus.BUILDING, GenerationStatus.ACTIVE),
            (GenerationStatus.BUILDING, GenerationStatus.READY),
            (GenerationStatus.BUILDING, GenerationStatus.DRAINING),
            (GenerationStatus.STARTING, GenerationStatus.ACTIVE),
            (GenerationStatus.WARMING, GenerationStatus.ACTIVE),
            (GenerationStatus.ACTIVE, GenerationStatus.RETIRED),
            (GenerationStatus.RETIRED, GenerationStatus.ACTIVE),
        ],
    )
    def test_invalid_transitions_raise(self, from_status, to_status):
        """Invalid transitions must raise GenerationStateError."""
        gen = Generation(
            name="X1",
            component=Component.ENGINEERING,
            status=from_status,
            created_at=datetime.datetime.utcnow(),
        )
        with pytest.raises(GenerationStateError):
            gen.transition(to_status)


class TestGenerationHelpers:
    """Test is_active, is_retired, to_dict, from_dict."""

    def test_is_active_true(self, gen_active):
        assert gen_active.is_active() is True

    def test_is_active_false(self, gen_building):
        assert gen_building.is_active() is False

    def test_is_retired_true(self, gen_retired):
        assert gen_retired.is_retired() is True

    def test_is_retired_false(self, gen_building):
        assert gen_building.is_retired() is False

    def test_to_dict_round_trip(self, gen_building):
        d = gen_building.to_dict()
        restored = Generation.from_dict(d)
        assert restored.name == gen_building.name
        assert restored.component == gen_building.component
        assert restored.status == gen_building.status
        assert restored.created_at == gen_building.created_at

    def test_to_dict_includes_optional_timestamps(self):
        gen = Generation(
            name="E50",
            component=Component.ENGINEERING,
            status=GenerationStatus.ACTIVE,
            created_at=datetime.datetime(2026, 1, 15, 10, 0, 0),
            build_start=datetime.datetime(2026, 1, 15, 10, 0, 30),
            activate_time=datetime.datetime(2026, 1, 15, 10, 5, 0),
        )
        d = gen.to_dict()
        assert d["build_start"] == "2026-01-15T10:00:30"
        assert d["activate_time"] == "2026-01-15T10:05:00"
        assert d["drain_start"] is None

    def test_from_dict_with_all_timestamps(self):
        data = {
            "name": "H19",
            "component": "hermees",
            "status": "ACTIVE",
            "created_at": "2026-01-15T11:00:00",
            "build_start": "2026-01-15T11:00:30",
            "build_end": "2026-01-15T11:02:00",
            "test_start": "2026-01-15T11:03:00",
            "test_end": "2026-01-15T11:04:00",
            "activate_time": "2026-01-15T11:05:00",
            "drain_start": None,
            "drain_end": None,
        }
        gen = Generation.from_dict(data)
        assert gen.name == "H19"
        assert gen.build_start == datetime.datetime(2026, 1, 15, 11, 0, 30)
        assert gen.drain_start is None
