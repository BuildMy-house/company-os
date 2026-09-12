"""Tests for Phase 2: Task Checkpointing, Drain Window, Hermees Reconciliation,
Atomic Switches, and Orchestration Handlers."""

import datetime
import json
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from companyd.companyd import (
    CheckpointStore,
    Component,
    CompanyDB,
    Deployment,
    DrainWindow,
    Generation,
    GenerationStatus,
    RuntimeControl,
    StateCheckpoint,
    StateManager,
    SyntheticTests,
)


# =============================================================================
# T2.1: Task Checkpointing
# =============================================================================


class TestStateCheckpoint:
    def test_creation(self):
        cp = StateCheckpoint(
            deployment_id="d1",
            component="engineering",
            generation="E42",
            status="BUILDING",
        )
        assert cp.deployment_id == "d1"
        assert cp.component == "engineering"
        assert cp.generation == "E42"
        assert cp.self_test_passed is False
        assert cp.health_check_passed is False

    def test_to_dict(self):
        cp = StateCheckpoint(
            deployment_id="d1",
            component="hermees",
            generation="H18",
            status="TESTING",
            self_test_passed=True,
            health_check_passed=True,
        )
        d = cp.to_dict()
        assert d["deployment_id"] == "d1"
        assert d["self_test_passed"] is True
        assert d["health_check_passed"] is True

    def test_roundtrip(self):
        cp = StateCheckpoint(
            deployment_id="d2",
            component="engineering",
            generation="E43",
            status="ACTIVE",
            active_tasks_at_drain=5,
            drain_duration_seconds=1800,
        )
        d = cp.to_dict()
        cp2 = StateCheckpoint(**d)
        assert cp2.deployment_id == "d2"
        assert cp2.active_tasks_at_drain == 5
        assert cp2.drain_duration_seconds == 1800


class TestCheckpointStore:
    def test_save_and_get(self, checkpoint_store):
        cp = StateCheckpoint(
            deployment_id="d1",
            component="engineering",
            generation="E42",
            status="TESTING",
        )
        checkpoint_store.save_checkpoint(cp)
        loaded = checkpoint_store.get_checkpoint("engineering", "E42")
        assert loaded is not None
        assert loaded.deployment_id == "d1"
        assert loaded.status == "TESTING"

    def test_get_nonexistent_returns_none(self, checkpoint_store):
        assert checkpoint_store.get_checkpoint("engineering", "E99") is None

    def test_remove_checkpoint(self, checkpoint_store):
        cp = StateCheckpoint(
            deployment_id="d1",
            component="engineering",
            generation="E42",
            status="ACTIVE",
        )
        checkpoint_store.save_checkpoint(cp)
        checkpoint_store.remove_checkpoint("engineering", "E42")
        assert checkpoint_store.get_checkpoint("engineering", "E42") is None

    def test_persists_to_file(self, checkpoint_store):
        cp = StateCheckpoint(
            deployment_id="d1",
            component="engineering",
            generation="E42",
            status="WARMING",
        )
        checkpoint_store.save_checkpoint(cp)
        # Verify file exists
        assert checkpoint_store._checkpoints_file.exists()
        # Load fresh store from same dir
        store2 = CheckpointStore(state_dir=checkpoint_store.state_dir)
        loaded = store2.get_checkpoint("engineering", "E42")
        assert loaded is not None
        assert loaded.status == "WARMING"

    def test_get_all_checkpoints(self, checkpoint_store):
        for i in range(3):
            cp = StateCheckpoint(
                deployment_id=f"d{i}",
                component="engineering",
                generation=f"E{42 + i}",
                status="ACTIVE",
            )
            checkpoint_store.save_checkpoint(cp)
        all_cps = checkpoint_store.get_all_checkpoints()
        assert len(all_cps) == 3

    def test_get_resumable_filters_terminal(self, checkpoint_store):
        for status in ["ACTIVE", "RETIRED", "ROLLED_BACK", "FAILED", "BUILDING", "TESTING", "WARMING"]:
            gen = status.lower()[:3]
            cp = StateCheckpoint(
                deployment_id=f"d-{status}",
                component="engineering",
                generation=f"E-{gen}",
                status=status,
            )
            checkpoint_store.save_checkpoint(cp)
        resumable = checkpoint_store.get_resumable()
        resumable_statuses = {cp.status for cp in resumable}
        assert "ACTIVE" not in resumable_statuses
        assert "RETIRED" not in resumable_statuses
        assert "ROLLED_BACK" not in resumable_statuses
        assert "FAILED" not in resumable_statuses
        assert "BUILDING" not in resumable_statuses
        assert "TESTING" in resumable_statuses
        assert "WARMING" in resumable_statuses

    def test_concurrent_saves(self, checkpoint_store):
        errors = []

        def save_cp(idx):
            try:
                cp = StateCheckpoint(
                    deployment_id=f"d{idx}",
                    component="engineering",
                    generation=f"E{idx}",
                    status="ACTIVE",
                )
                checkpoint_store.save_checkpoint(cp)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=save_cp, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(checkpoint_store.get_all_checkpoints()) == 20


# =============================================================================
# T2.2: Drain Window Logic
# =============================================================================


class TestDrainWindow:
    def test_begin(self, drain_window):
        info = drain_window.begin(active_tasks=10)
        assert info["status"] == "draining"
        assert info["active_tasks_at_start"] == 10
        assert drain_window.start_time is not None

    def test_is_complete_no_tasks(self, drain_window):
        drain_window.begin(active_tasks=0)
        assert drain_window.is_complete() is True

    def test_is_not_complete_with_pending(self, drain_window):
        drain_window.begin(active_tasks=5)
        assert drain_window.is_complete() is False
        drain_window.record_task_completed()
        assert drain_window.is_complete() is False

    def test_is_complete_after_all_done(self, drain_window):
        drain_window.begin(active_tasks=3)
        drain_window.record_task_completed()
        drain_window.record_task_completed()
        drain_window.record_task_completed()
        assert drain_window.is_complete() is True

    def test_timeout_detection(self):
        dw = DrainWindow("E42", Component.ENGINEERING, max_duration_seconds=0)
        dw.begin(active_tasks=1)
        assert dw.is_timed_out() is True

    def test_not_timed_out_within_window(self, drain_window):
        drain_window.begin(active_tasks=1)
        assert drain_window.is_timed_out() is False

    def test_finish_summary(self, drain_window):
        drain_window.begin(active_tasks=5)
        drain_window.record_task_completed()
        drain_window.record_task_completed()
        info = drain_window.finish()
        assert info["completed_tasks"] == 2
        assert info["active_tasks_at_start"] == 5
        assert info["timed_out"] is False
        assert info["end_time"] is not None

    def test_finish_timeout_summary(self):
        dw = DrainWindow("E42", Component.ENGINEERING, max_duration_seconds=0)
        dw.begin(active_tasks=5)
        info = dw.finish()
        assert info["timed_out"] is True
        assert info["status"] == "drain_timeout"

    def test_concurrent_task_completions(self, drain_window):
        drain_window.begin(active_tasks=20)
        errors = []

        def complete_one():
            try:
                drain_window.record_task_completed()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=complete_one) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert drain_window.completed_tasks == 20
        assert drain_window.is_complete() is True


# =============================================================================
# T2.3: Hermees State Reconciliation
# =============================================================================


class TestCompanyDB:
    def test_stub_mode_without_dsn(self):
        db = CompanyDB(dsn=None)
        assert db.connect() is False
        result = db.check_active_deployments("engineering")
        assert result["connected"] is False
        assert result["status"] == "stub"

    def test_stub_checks_all_return_stub(self):
        db = CompanyDB(dsn=None)
        db.connect()
        assert db.check_active_deployments("hermees")["status"] == "stub"
        assert db.check_recent_decisions("hermees")["status"] == "stub"
        assert db.check_active_experiments()["status"] == "stub"
        assert db.check_recent_failures("hermees")["status"] == "stub"

    def test_close_without_connection(self):
        db = CompanyDB(dsn=None)
        db.close()  # should not raise


class TestHermeesReconciliation:
    def test_stub_mode_all_pass(self, synthetic_tests):
        gen = Generation(
            name="H18",
            component=Component.HERMEES,
            status=GenerationStatus.TESTING,
            created_at=datetime.datetime(2026, 1, 15),
        )
        with patch.object(SyntheticTests, "__init__", lambda self, **kw: None):
            st = SyntheticTests.__new__(SyntheticTests)
            st.logger = MagicMock()
            st.company_db = None
            with patch("companyd.companyd.HealthCheck") as MockHC:
                mock_hc = MockHC.return_value
                mock_hc.check.return_value = {"status": "OK", "response_time_ms": 10, "error_message": None}
                result = st.hermees_reconciliation(gen)
        assert result["handover_accepted"] is True
        assert result["checks"]["health_check"] is True

    def test_rejects_on_health_failure(self, synthetic_tests):
        gen = Generation(
            name="H18",
            component=Component.HERMEES,
            status=GenerationStatus.TESTING,
            created_at=datetime.datetime(2026, 1, 15),
        )
        st = SyntheticTests.__new__(SyntheticTests)
        st.logger = MagicMock()
        st.company_db = None
        with patch("companyd.companyd.HealthCheck") as MockHC:
            mock_hc = MockHC.return_value
            mock_hc.check.return_value = {"status": "FAILED", "response_time_ms": 10, "error_message": "refused"}
            result = st.hermees_reconciliation(gen)
        assert result["handover_accepted"] is False

    def test_rejects_on_conflicting_deployments(self):
        gen = Generation(
            name="H18",
            component=Component.HERMEES,
            status=GenerationStatus.TESTING,
            created_at=datetime.datetime(2026, 1, 15),
        )
        mock_db = MagicMock()
        mock_db._conn = True  # pretend connected
        mock_db.check_active_deployments.return_value = {"connected": True, "active_count": 1, "status": "ok"}
        mock_db.check_recent_failures.return_value = {"connected": True, "count": 0, "status": "ok"}
        mock_db.check_active_experiments.return_value = {"connected": True, "count": 0, "status": "ok"}

        st = SyntheticTests.__new__(SyntheticTests)
        st.logger = MagicMock()
        st.company_db = mock_db
        with patch("companyd.companyd.HealthCheck") as MockHC:
            mock_hc = MockHC.return_value
            mock_hc.check.return_value = {"status": "OK", "response_time_ms": 10, "error_message": None}
            result = st.hermees_reconciliation(gen)
        assert result["handover_accepted"] is False
        assert result["checks"]["active_deployments"] is False

    def test_rejects_on_recent_failures(self):
        gen = Generation(
            name="H18",
            component=Component.HERMEES,
            status=GenerationStatus.TESTING,
            created_at=datetime.datetime(2026, 1, 15),
        )
        mock_db = MagicMock()
        mock_db._conn = True
        mock_db.check_active_deployments.return_value = {"connected": True, "active_count": 0, "status": "ok"}
        mock_db.check_recent_failures.return_value = {"connected": True, "count": 3, "status": "ok"}
        mock_db.check_active_experiments.return_value = {"connected": True, "count": 0, "status": "ok"}

        st = SyntheticTests.__new__(SyntheticTests)
        st.logger = MagicMock()
        st.company_db = mock_db
        with patch("companyd.companyd.HealthCheck") as MockHC:
            mock_hc = MockHC.return_value
            mock_hc.check.return_value = {"status": "OK", "response_time_ms": 10, "error_message": None}
            result = st.hermees_reconciliation(gen)
        assert result["handover_accepted"] is False
        assert result["checks"]["recent_failures"] is False


# =============================================================================
# T2.4: Atomic Active-Generation Switches
# =============================================================================


class TestAtomicSwitch:
    def test_atomic_switch(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E41")
        result = runtime_control.atomic_switch(Component.ENGINEERING, "E42")
        assert result["previous_generation"] == "E41"
        assert result["new_generation"] == "E42"
        assert runtime_control.get_active(Component.ENGINEERING) == "E42"

    def test_atomic_switch_preserves_previous(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E41")
        runtime_control.atomic_switch(Component.ENGINEERING, "E42")
        assert runtime_control.get_previous(Component.ENGINEERING) == "E41"

    def test_atomic_switch_from_empty(self, runtime_control):
        result = runtime_control.atomic_switch(Component.HERMEES, "H1")
        assert result["previous_generation"] is None
        assert result["new_generation"] == "H1"

    def test_rollback(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E41")
        runtime_control.atomic_switch(Component.ENGINEERING, "E42")
        result = runtime_control.rollback(Component.ENGINEERING)
        assert result["rolled_back_from"] == "E42"
        assert result["rolled_back_to"] == "E41"
        assert runtime_control.get_active(Component.ENGINEERING) == "E41"

    def test_rollback_no_previous(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E41")
        result = runtime_control.rollback(Component.ENGINEERING)
        assert result is None

    def test_rollback_no_entry(self, runtime_control):
        result = runtime_control.rollback(Component.HERMEES)
        assert result is None

    def test_concurrent_atomic_switches(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E0")
        errors = []

        def switch_to(idx):
            try:
                runtime_control.atomic_switch(Component.ENGINEERING, f"E{idx}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=switch_to, args=(i,)) for i in range(1, 21)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        final = runtime_control.get_active(Component.ENGINEERING)
        assert final.startswith("E")

    def test_rollback_switch_rollback_cycle(self, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E41")
        runtime_control.atomic_switch(Component.ENGINEERING, "E42")
        runtime_control.atomic_switch(Component.ENGINEERING, "E43")
        assert runtime_control.get_active(Component.ENGINEERING) == "E43"
        # Rollback goes to previous (E42), not all the way back to E41
        result = runtime_control.rollback(Component.ENGINEERING)
        assert result["rolled_back_to"] == "E42"
        # Rollback again goes to E43 (previous was E43 after first rollback)
        result = runtime_control.rollback(Component.ENGINEERING)
        assert result["rolled_back_to"] == "E43"


# =============================================================================
# T2.5: Orchestration Handlers (via SystemdSocketListener)
# =============================================================================


class TestOrchestrationHandlers:
    """Test orchestration handlers by calling them directly on a StateManager."""

    @pytest.fixture
    def sm(self, runtime_control, checkpoint_store):
        from companyd.companyd import StateManager
        return StateManager(runtime_control, checkpoint_store)

    @pytest.fixture
    def listener(self, sm):
        from companyd.companyd import SystemdSocketListener
        return SystemdSocketListener(sm, socket_path=Path("/tmp/test-companyd.sock"))

    def test_handle_build_missing_args(self, listener):
        result = listener._handle_build({})
        assert result["status"] == "error"

    def test_handle_build_bad_component(self, listener):
        result = listener._handle_build({"component": "bogus", "git_sha": "abc"})
        assert result["status"] == "error"

    @patch("companyd.companyd.DockerLifecycle")
    def test_handle_build_success(self, MockDocker, listener):
        mock_docker = MockDocker.return_value
        mock_docker.build.return_value = "engineering:E42"
        result = listener._handle_build({
            "component": "engineering",
            "git_sha": "abc123",
        })
        assert result["status"] == "success"
        assert "generation" in result
        assert result["generation"] in listener.state_manager.generations

    def test_handle_start_missing_generation(self, listener):
        result = listener._handle_start({})
        assert result["status"] == "error"

    def test_handle_start_unknown_generation(self, listener):
        result = listener._handle_start({"generation": "E99"})
        assert result["status"] == "error"

    def test_handle_test_missing_generation(self, listener):
        result = listener._handle_test({})
        assert result["status"] == "error"

    def test_handle_activate_missing_generation(self, listener):
        result = listener._handle_activate({})
        assert result["status"] == "error"

    def test_handle_drain_missing_generation(self, listener):
        result = listener._handle_drain({})
        assert result["status"] == "error"

    def test_handle_status_overview(self, listener):
        result = listener._handle_status({"overview": True})
        assert result["status"] == "success"
        assert "overview" in result
        assert "engineering" in result["overview"]
        assert "hermees" in result["overview"]

    def test_handle_status_component(self, listener):
        result = listener._handle_status({"component": "engineering"})
        assert result["status"] == "success"
        assert result["component"] == "engineering"

    def test_handle_status_generation_not_found(self, listener):
        result = listener._handle_status({"generation": "E99"})
        assert result["status"] == "error"

    def test_handle_status_no_args(self, listener):
        result = listener._handle_status({})
        assert result["status"] == "error"

    def test_handle_logs_missing_generation(self, listener):
        result = listener._handle_logs({})
        assert result["status"] == "error"

    def test_handle_rollback_missing_component(self, listener):
        result = listener._handle_rollback({})
        assert result["status"] == "error"

    def test_handle_rollback_bad_component(self, listener):
        result = listener._handle_rollback({"component": "bogus"})
        assert result["status"] == "error"

    def test_handle_rollback_no_previous(self, listener, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E41")
        result = listener._handle_rollback({"component": "engineering"})
        assert result["status"] == "error"

    def test_handle_rollback_success(self, listener, runtime_control):
        runtime_control.set_active(Component.ENGINEERING, "E41")
        runtime_control.atomic_switch(Component.ENGINEERING, "E42")
        result = listener._handle_rollback({"component": "engineering"})
        assert result["status"] == "success"
        assert result["rollback"]["rolled_back_to"] == "E41"

    def test_handle_activate_full_flow(self, listener, runtime_control):
        # Set up an E41 as active, create E42 in READY state
        runtime_control.set_active(Component.ENGINEERING, "E41")
        gen = Generation(
            name="E42",
            component=Component.ENGINEERING,
            status=GenerationStatus.READY,
            created_at=datetime.datetime.utcnow(),
        )
        listener.state_manager.generations["E42"] = gen

        result = listener._handle_activate({"generation": "E42"})
        assert result["status"] == "success"
        assert result["previous_generation"] == "E41"
        assert runtime_control.get_active(Component.ENGINEERING) == "E42"
        assert gen.status == GenerationStatus.ACTIVE

        # E41 should now be DRAINING
        e41 = listener.state_manager.generations.get("E41")
        if e41:
            assert e41.status == GenerationStatus.DRAINING

    def test_process_request_routes_correctly(self, listener):
        result = listener._process_request({"command": "status", "args": {"overview": True}})
        assert result["status"] == "success"

    def test_process_request_unknown_command(self, listener):
        result = listener._process_request({"command": "bogus"})
        assert result["status"] == "error"
        assert "Unknown command" in result["message"]


# =============================================================================
# T2.6: Checkpoint Generation (checkpoint_generation / restore_tasks)
# =============================================================================


class TestCheckpointGeneration:
    @pytest.fixture
    def sm(self, runtime_control, checkpoint_store):
        return StateManager(runtime_control, checkpoint_store)

    def _make_gen(self, sm, name, status):
        gen = Generation(
            name=name,
            component=Component.ENGINEERING,
            status=status,
            created_at=datetime.datetime.utcnow(),
        )
        sm.generations[name] = gen
        return gen

    def test_checkpoint_saves_active_tasks(self, sm):
        self._make_gen(sm, "E10", GenerationStatus.DRAINING)

        cp = StateCheckpoint(
            deployment_id="task-0",
            component="engineering",
            generation="E10",
            status="TESTING",
            metadata={"branch": "feat/0", "commit": "sha0"},
        )
        sm.checkpoint_store.save_checkpoint(cp)

        result = sm.checkpoint_generation("E10")
        assert result["status"] == "checkpointed"
        assert result["checkpoints_saved"] == 1

        stored = sm.checkpoint_store.get_checkpoint("engineering", "E10")
        assert stored is not None
        assert stored.status == "INTERRUPTED"

    def test_checkpoint_updates_pg_status(self, sm):
        self._make_gen(sm, "E10", GenerationStatus.DRAINING)

        cp = StateCheckpoint(
            deployment_id="task-pg",
            component="engineering",
            generation="E10",
            status="STARTING",
        )
        sm.checkpoint_store.save_checkpoint(cp)

        sm.checkpoint_generation("E10")

        stored = sm.checkpoint_store.get_checkpoint("engineering", "E10")
        assert stored.status == "INTERRUPTED"

    def test_checkpoint_nonexistent_gen_returns_error(self, sm):
        result = sm.checkpoint_generation("E999")
        assert result["status"] == "error"
        assert "not found" in result["error_message"]

    def test_checkpoint_wrong_status_returns_error(self, sm):
        self._make_gen(sm, "E10", GenerationStatus.BUILDING)
        result = sm.checkpoint_generation("E10")
        assert result["status"] == "error"
        assert "not DRAINING" in result["error_message"]

    def test_checkpoint_concurrent_saves(self, sm):
        self._make_gen(sm, "E10", GenerationStatus.DRAINING)

        errors = []

        def save_checkpoint(idx):
            try:
                cp = StateCheckpoint(
                    deployment_id=f"conc-{idx}",
                    component="engineering",
                    generation=f"E10-{idx}",
                    status="WARMING",
                )
                sm.checkpoint_store.save_checkpoint(cp)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=save_checkpoint, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        all_cps = sm.checkpoint_store.get_all_checkpoints()
        eng_cps = [cp for cp in all_cps if cp.component == "engineering" and cp.generation.startswith("E10-")]
        assert len(eng_cps) == 10


class TestRestoreTasks:
    @pytest.fixture
    def sm(self, runtime_control, checkpoint_store):
        return StateManager(runtime_control, checkpoint_store)

    def _make_gen(self, sm, name, component, status):
        gen = Generation(
            name=name,
            component=component,
            status=status,
            created_at=datetime.datetime.utcnow(),
        )
        sm.generations[name] = gen
        return gen

    def test_restore_loads_from_store(self, sm):
        self._make_gen(sm, "E9", Component.ENGINEERING, GenerationStatus.DRAINING)

        cp = StateCheckpoint(
            deployment_id="task-restore",
            component="engineering",
            generation="E9",
            status="INTERRUPTED",
            metadata={"branch": "feat/restore", "commit": "sha9"},
        )
        sm.checkpoint_store.save_checkpoint(cp)

        self._make_gen(sm, "E10", Component.ENGINEERING, GenerationStatus.READY)

        result = sm.restore_tasks("E10")
        assert result["status"] == "restored"
        assert result["checkpoints_found"] == 1

    def test_restore_creates_recovery_manifest(self, sm):
        self._make_gen(sm, "E9", Component.ENGINEERING, GenerationStatus.DRAINING)

        cp = StateCheckpoint(
            deployment_id="task-manifest",
            component="engineering",
            generation="E9",
            status="INTERRUPTED",
            metadata={
                "branch": "feat/login",
                "commit": "abc123def",
                "worktree_path": "/tmp/worktree",
                "last_message": "Implement cache layer",
            },
        )
        sm.checkpoint_store.save_checkpoint(cp)

        self._make_gen(sm, "E10", Component.ENGINEERING, GenerationStatus.READY)

        result = sm.restore_tasks("E10")
        assert len(result["recovery_manifest"]) == 1
        entry = result["recovery_manifest"][0]
        assert entry["task_id"] == "task-manifest"
        assert entry["branch"] == "feat/login"
        assert entry["commit"] == "abc123def"
        assert entry["worktree_path"] == "/tmp/worktree"
        assert entry["last_message"] == "Implement cache layer"

    def test_restore_filters_by_component(self, sm):
        self._make_gen(sm, "E9", Component.ENGINEERING, GenerationStatus.DRAINING)
        self._make_gen(sm, "H5", Component.HERMEES, GenerationStatus.DRAINING)

        for comp, gen, task_id in [
            ("engineering", "E9", "eng-task"),
            ("hermees", "H5", "herm-task"),
        ]:
            cp = StateCheckpoint(
                deployment_id=task_id,
                component=comp,
                generation=gen,
                status="INTERRUPTED",
            )
            sm.checkpoint_store.save_checkpoint(cp)

        self._make_gen(sm, "E10", Component.ENGINEERING, GenerationStatus.READY)

        result = sm.restore_tasks("E10")
        assert result["checkpoints_found"] == 1
        assert result["recovery_manifest"][0]["task_id"] == "eng-task"

    def test_restore_mark_as_restored_in_pg(self, sm):
        self._make_gen(sm, "E9", Component.ENGINEERING, GenerationStatus.DRAINING)

        cp = StateCheckpoint(
            deployment_id="task-pg-restore",
            component="engineering",
            generation="E9",
            status="INTERRUPTED",
        )
        sm.checkpoint_store.save_checkpoint(cp)

        self._make_gen(sm, "E10", Component.ENGINEERING, GenerationStatus.READY)

        sm.restore_tasks("E10")

        restored_cp = sm.checkpoint_store.get_checkpoint("engineering", "E10")
        assert restored_cp is not None
        assert restored_cp.status == "RESTORED"

    def test_restore_nonexistent_gen_returns_error(self, sm):
        result = sm.restore_tasks("E999")
        assert result["status"] == "error"
        assert "not found" in result["error_message"]

    def test_checkpoint_restore_roundtrip(self, sm):
        self._make_gen(sm, "E9", Component.ENGINEERING, GenerationStatus.DRAINING)

        task_data = {
            "deployment_id": "roundtrip-task",
            "component": "engineering",
            "generation": "E9",
            "status": "TESTING",
            "metadata": {
                "branch": "feat/roundtrip",
                "commit": "round123",
                "worktree_path": "/tmp/rt-worktree",
                "last_message": "Roundtrip test",
            },
        }
        cp = StateCheckpoint(**task_data)
        sm.checkpoint_store.save_checkpoint(cp)

        result_cp = sm.checkpoint_generation("E9")
        assert result_cp["status"] == "checkpointed"
        assert result_cp["checkpoints_saved"] == 1

        stored = sm.checkpoint_store.get_checkpoint("engineering", "E9")
        assert stored.status == "INTERRUPTED"
        assert stored.metadata["branch"] == "feat/roundtrip"
        assert stored.metadata["commit"] == "round123"

        self._make_gen(sm, "E10", Component.ENGINEERING, GenerationStatus.READY)

        result_restore = sm.restore_tasks("E10")
        assert result_restore["status"] == "restored"
        assert result_restore["checkpoints_restored"] == 1

        entry = result_restore["recovery_manifest"][0]
        assert entry["task_id"] == "roundtrip-task"
        assert entry["branch"] == "feat/roundtrip"
        assert entry["commit"] == "round123"
        assert entry["worktree_path"] == "/tmp/rt-worktree"
        assert entry["last_message"] == "Roundtrip test"
