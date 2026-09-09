"""Tests for RuntimeControl atomic generation pointer updates."""

import datetime
import threading

import pytest

from companyd.companyd import Component, RuntimeControl


class TestRuntimeControlBasics:
    """Test basic set/get operations."""

    def test_get_active_returns_none_initially(self, runtime_control):
        assert runtime_control.get_active(Component.ENGINEERING) is None

    def test_set_then_get_active(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E42")
        assert runtime_control.get_active(Component.ENGINEERING) == "E42"

    def test_set_active_updates_timestamp(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E42")
        entry = runtime_control.state[Component.ENGINEERING.value]
        assert "updated_at" in entry
        assert entry["active_generation"] == "E42"

    def test_set_active_overwrites(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E42")
        runtime_control.set_active(Component.ENGINEERING, "E43")
        assert runtime_control.get_active(Component.ENGINEERING) == "E43"

    def test_independent_components(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E42")
        runtime_control.set_active(Component.HERMEES, "H18")
        assert runtime_control.get_active(Component.ENGINEERING) == "E42"
        assert runtime_control.get_active(Component.HERMEES) == "H18"

    def test_persists_to_file(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E42")
        loaded = RuntimeControl(state_file=runtime_control.state_file)
        assert loaded.get_active(Component.ENGINEERING) == "E42"


class TestRuntimeControlConcurrency:
    """Simulate concurrent set_active calls."""

    def test_concurrent_set_active_no_crash(self, runtime_control):
        """Multiple threads setting active should not crash or deadlock."""
        errors = []

        def set_gen(name):
            try:
                runtime_control.set_active(Component.ENGINEERING, name)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=set_gen, args=(f"E{i}",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
        final = runtime_control.get_active(Component.ENGINEERING)
        assert final is not None

    def test_concurrent_read_write_no_crash(self, runtime_control):
        """Simultaneous reads and writes should not deadlock."""
        runtime_control.set_active(Component.ENGINEERING, "E42")
        errors = []
        results = []

        def reader():
            try:
                for _ in range(10):
                    results.append(runtime_control.get_active(Component.ENGINEERING))
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(10):
                    runtime_control.set_active(Component.ENGINEERING, f"E{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads += [threading.Thread(target=writer) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
        assert all(r is not None for r in results)

    def test_lock_released_after_set(self, runtime_control):
        """After set_active completes, the lock should be released (no deadlock on next call)."""
        runtime_control.set_active(Component.ENGINEERING, "E42")
        # This should not hang if lock was properly released
        runtime_control.set_active(Component.ENGINEERING, "E43")
        assert runtime_control.get_active(Component.ENGINEERING) == "E43"
