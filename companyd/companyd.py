#!/usr/bin/env python3
"""
companyd: Autonomous multi-generation deployment daemon for Hermees and Engineering containers.

Manages independent versioned instances (generations) for each component, handling
state transitions, health checks, synthetic tests, and atomic activation switching.
"""

import argparse
import dataclasses
import datetime
import fcntl
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__version__ = "0.1.0"

# =============================================================================
# Configuration
# =============================================================================

COMPANYD_HOME = Path("/var/lib/companyd")
COMPANYD_RUNTIME_CONTROL_FILE = COMPANYD_HOME / "runtime_control.json"
COMPANYD_SOCKET = Path("/var/run/companyd.sock")
COMPANYD_LOG_FILE = Path("/var/log/companyd/companyd.log")

logger = logging.getLogger(__name__)


# =============================================================================
# Exceptions
# =============================================================================

class CompanydError(Exception):
    """Base exception for companyd errors."""
    pass


class GenerationNotFoundError(CompanydError):
    """Raised when a generation cannot be found."""
    pass


class GenerationStateError(CompanydError):
    """Raised when a generation is in an invalid state for the requested operation."""
    pass


class DockerError(CompanydError):
    """Raised when a docker operation fails."""
    pass


class HealthCheckError(CompanydError):
    """Raised when a health check fails."""
    pass


class RuntimeControlError(CompanydError):
    """Raised when runtime control update fails."""
    pass


# =============================================================================
# Enums
# =============================================================================

class GenerationStatus(Enum):
    """Valid states in generation lifecycle."""
    BUILDING = "BUILDING"
    BUILD_FAILED = "BUILD_FAILED"
    STARTING = "STARTING"
    WARMING = "WARMING"
    TESTING = "TESTING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    RETIRED = "RETIRED"
    TEST_FAILED = "TEST_FAILED"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    DRAIN_TIMEOUT = "DRAIN_TIMEOUT"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class Component(Enum):
    """Supported components."""
    HERMEES = "hermees"
    ENGINEERING = "engineering"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class Generation:
    """Represents a versioned instance of a component."""

    name: str  # H18, E43, etc.
    component: Component
    status: GenerationStatus
    created_at: datetime.datetime
    build_start: Optional[datetime.datetime] = None
    build_end: Optional[datetime.datetime] = None
    test_start: Optional[datetime.datetime] = None
    test_end: Optional[datetime.datetime] = None
    activate_time: Optional[datetime.datetime] = None
    drain_start: Optional[datetime.datetime] = None
    drain_end: Optional[datetime.datetime] = None

    def transition(self, new_status: GenerationStatus) -> None:
        """Move generation to a new status with validation."""
        valid_transitions = {
            GenerationStatus.BUILDING: [
                GenerationStatus.STARTING,
                GenerationStatus.BUILD_FAILED,
                GenerationStatus.FAILED,
            ],
            GenerationStatus.BUILD_FAILED: [
                GenerationStatus.ROLLED_BACK,
            ],
            GenerationStatus.STARTING: [
                GenerationStatus.WARMING,
                GenerationStatus.ACTIVATION_FAILED,
                GenerationStatus.FAILED,
            ],
            GenerationStatus.WARMING: [
                GenerationStatus.TESTING,
                GenerationStatus.FAILED,
            ],
            GenerationStatus.TESTING: [
                GenerationStatus.READY,
                GenerationStatus.TEST_FAILED,
                GenerationStatus.FAILED,
            ],
            GenerationStatus.TEST_FAILED: [
                GenerationStatus.ROLLED_BACK,
            ],
            GenerationStatus.READY: [
                GenerationStatus.ACTIVE,
                GenerationStatus.ACTIVATION_FAILED,
            ],
            GenerationStatus.ACTIVATION_FAILED: [
                GenerationStatus.ROLLED_BACK,
            ],
            GenerationStatus.ACTIVE: [
                GenerationStatus.DRAINING,
            ],
            GenerationStatus.DRAINING: [
                GenerationStatus.RETIRED,
                GenerationStatus.DRAIN_TIMEOUT,
            ],
            GenerationStatus.DRAIN_TIMEOUT: [
                GenerationStatus.RETIRED,
            ],
        }

        if self.status not in valid_transitions:
            raise GenerationStateError(
                f"No valid transitions from {self.status} defined"
            )

        if new_status not in valid_transitions[self.status]:
            raise GenerationStateError(
                f"Cannot transition from {self.status} to {new_status}"
            )

        self.status = new_status
        logger.info(f"Generation {self.name} transitioned to {new_status}")

    def is_active(self) -> bool:
        """Check if generation is currently active."""
        return self.status == GenerationStatus.ACTIVE

    def is_retired(self) -> bool:
        """Check if generation is retired."""
        return self.status == GenerationStatus.RETIRED

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "component": self.component.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "build_start": self.build_start.isoformat() if self.build_start else None,
            "build_end": self.build_end.isoformat() if self.build_end else None,
            "test_start": self.test_start.isoformat() if self.test_start else None,
            "test_end": self.test_end.isoformat() if self.test_end else None,
            "activate_time": self.activate_time.isoformat() if self.activate_time else None,
            "drain_start": self.drain_start.isoformat() if self.drain_start else None,
            "drain_end": self.drain_end.isoformat() if self.drain_end else None,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Generation":
        """Create Generation from dictionary."""
        return cls(
            name=data["name"],
            component=Component(data["component"]),
            status=GenerationStatus(data["status"]),
            created_at=datetime.datetime.fromisoformat(data["created_at"]),
            build_start=datetime.datetime.fromisoformat(data["build_start"]) if data.get("build_start") else None,
            build_end=datetime.datetime.fromisoformat(data["build_end"]) if data.get("build_end") else None,
            test_start=datetime.datetime.fromisoformat(data["test_start"]) if data.get("test_start") else None,
            test_end=datetime.datetime.fromisoformat(data["test_end"]) if data.get("test_end") else None,
            activate_time=datetime.datetime.fromisoformat(data["activate_time"]) if data.get("activate_time") else None,
            drain_start=datetime.datetime.fromisoformat(data["drain_start"]) if data.get("drain_start") else None,
            drain_end=datetime.datetime.fromisoformat(data["drain_end"]) if data.get("drain_end") else None,
        )


@dataclass
class Deployment:
    """Records deployment events for history and rollback."""

    id: str
    component: Component
    generation: Generation
    git_sha: str
    docker_image: str
    status: GenerationStatus
    created_at: datetime.datetime
    build_start: Optional[datetime.datetime] = None
    build_end: Optional[datetime.datetime] = None
    test_start: Optional[datetime.datetime] = None
    test_end: Optional[datetime.datetime] = None
    activate_time: Optional[datetime.datetime] = None
    drain_start: Optional[datetime.datetime] = None
    drain_end: Optional[datetime.datetime] = None
    test_passed: bool = False
    test_summary: str = ""
    previous_generation: Optional[str] = None
    rollback_required: bool = False
    rollback_reason: str = ""
    self_test_passed: bool = False
    health_check_passed: bool = False
    active_tasks_at_drain: int = 0
    drain_duration_seconds: int = 0
    human_intervention_required: bool = False

    def update_status(self, new_status: GenerationStatus) -> None:
        """Update deployment status."""
        self.status = new_status
        logger.info(f"Deployment {self.id} status updated to {new_status}")

    def record_event(self, event_name: str, details: str = "") -> None:
        """Record a deployment event."""
        timestamp = datetime.datetime.utcnow().isoformat()
        logger.info(f"Deployment {self.id}: {event_name} - {details} [{timestamp}]")

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "component": self.component.value,
            "generation": self.generation.name,
            "git_sha": self.git_sha,
            "docker_image": self.docker_image,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "build_start": self.build_start.isoformat() if self.build_start else None,
            "build_end": self.build_end.isoformat() if self.build_end else None,
            "test_start": self.test_start.isoformat() if self.test_start else None,
            "test_end": self.test_end.isoformat() if self.test_end else None,
            "activate_time": self.activate_time.isoformat() if self.activate_time else None,
            "drain_start": self.drain_start.isoformat() if self.drain_start else None,
            "drain_end": self.drain_end.isoformat() if self.drain_end else None,
            "test_passed": self.test_passed,
            "test_summary": self.test_summary,
            "previous_generation": self.previous_generation,
            "rollback_required": self.rollback_required,
            "rollback_reason": self.rollback_reason,
            "self_test_passed": self.self_test_passed,
            "health_check_passed": self.health_check_passed,
            "active_tasks_at_drain": self.active_tasks_at_drain,
            "drain_duration_seconds": self.drain_duration_seconds,
            "human_intervention_required": self.human_intervention_required,
        }


# =============================================================================
# Runtime Control — Atomic Generation Pointer
# =============================================================================

@dataclass
class StateCheckpoint:
    """Snapshot of deployment state at a lifecycle milestone for crash recovery."""

    deployment_id: str
    component: str
    generation: str
    status: str
    self_test_passed: bool = False
    health_check_passed: bool = False
    active_tasks_at_drain: int = 0
    drain_duration_seconds: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict:
        return dataclasses.asdict(self)


class CheckpointStore:
    """Persists deployment checkpoints for crash recovery."""

    def __init__(self, state_dir: Path = COMPANYD_HOME):
        self.state_dir = state_dir
        self.logger = logging.getLogger(f"{__name__}.CheckpointStore")
        self._checkpoints_file = state_dir / "checkpoints.json"
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        self._checkpoints: Dict[str, Dict] = {}
        if self._checkpoints_file.exists():
            try:
                with open(self._checkpoints_file, "r") as f:
                    self._checkpoints = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._checkpoints = {}

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self._checkpoints_file.with_suffix(".lock")
        with open(lock_file, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                with open(self._checkpoints_file, "w") as f:
                    json.dump(self._checkpoints, f, indent=2)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def save_checkpoint(self, checkpoint: StateCheckpoint) -> None:
        """Persist a deployment checkpoint atomically."""
        with self._lock:
            key = f"{checkpoint.component}:{checkpoint.generation}"
            checkpoint.updated_at = datetime.datetime.utcnow().isoformat()
            self._checkpoints[key] = checkpoint.to_dict()
            self._save()
            self.logger.info(
                f"Checkpoint saved: {key} -> {checkpoint.status}"
            )

    def get_checkpoint(self, component: str, generation: str) -> Optional[StateCheckpoint]:
        """Retrieve a checkpoint by component and generation."""
        key = f"{component}:{generation}"
        data = self._checkpoints.get(key)
        if data:
            return StateCheckpoint(**data)
        return None

    def get_all_checkpoints(self) -> List[StateCheckpoint]:
        """Return all stored checkpoints."""
        return [StateCheckpoint(**v) for v in self._checkpoints.values()]

    def remove_checkpoint(self, component: str, generation: str) -> None:
        """Remove a checkpoint (e.g. after successful retirement)."""
        with self._lock:
            key = f"{component}:{generation}"
            self._checkpoints.pop(key, None)
            self._save()

    def get_resumable(self) -> List[StateCheckpoint]:
        """Return checkpoints for deployments that were interrupted mid-flight.

        A deployment is resumable if it's not in a terminal state (ACTIVE, RETIRED,
        ROLLED_BACK, FAILED) and not in BUILDING (which must restart from scratch).
        """
        terminal = {
            GenerationStatus.ACTIVE.value,
            GenerationStatus.RETIRED.value,
            GenerationStatus.ROLLED_BACK.value,
            GenerationStatus.FAILED.value,
        }
        return [
            cp for cp in self.get_all_checkpoints()
            if cp.status not in terminal and cp.status != GenerationStatus.BUILDING.value
        ]


# =============================================================================
# Runtime Control — Atomic Generation Pointer
# =============================================================================


class RuntimeControl:
    """Manages atomic active-generation pointer per component with file-based locking."""

    def __init__(self, state_file: Path = COMPANYD_RUNTIME_CONTROL_FILE):
        self.state_file = state_file
        self.logger = logging.getLogger(f"{__name__}.RuntimeControl")
        self._lock = threading.RLock()
        self._load_or_init()

    def _load_or_init(self) -> None:
        """Load state from file or initialize with empty dict."""
        self.state = {}
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    self.state = json.load(f)
                logger.info(f"Loaded runtime control from {self.state_file}")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load runtime control: {e}, initializing empty")
                self.state = {}

    def _save(self) -> None:
        """Persist state to file with write lock."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # Use file lock for atomic writes
        lock_file = self.state_file.with_suffix(".lock")
        with open(lock_file, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                with open(self.state_file, "w") as f:
                    json.dump(self.state, f, indent=2)
                logger.info(f"Persisted runtime control to {self.state_file}")
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def set_active(self, component: Component, generation_name: str) -> None:
        """Atomically set active generation for a component."""
        with self._lock:
            self.state[component.value] = {
                "active_generation": generation_name,
                "updated_at": datetime.datetime.utcnow().isoformat(),
            }
            self._save()
            logger.info(f"Set {component.value} active generation to {generation_name}")

    def get_active(self, component: Component) -> Optional[str]:
        """Get current active generation for a component."""
        with self._lock:
            if component.value in self.state:
                return self.state[component.value]["active_generation"]
        return None

    def get_previous(self, component: Component) -> Optional[str]:
        """Get the generation that was active before the current one."""
        with self._lock:
            entry = self.state.get(component.value)
            if entry:
                return entry.get("previous_generation")
        return None

    def atomic_switch(
        self,
        component: Component,
        new_generation: str,
    ) -> Dict:
        """Atomically switch active generation from old to new.

        Uses fcntl.flock on runtime_control.json for zero-downtime pointer swap.
        Returns the switch record with previous generation for rollback.
        """
        with self._lock:
            old_generation = self.get_active(component)
            now = datetime.datetime.utcnow().isoformat()

            self.state[component.value] = {
                "active_generation": new_generation,
                "previous_generation": old_generation,
                "updated_at": now,
                "switched_at": now,
            }
            self._save()

            self.logger.info(
                f"Atomic switch: {component.value} {old_generation} -> {new_generation}"
            )
            return {
                "component": component.value,
                "previous_generation": old_generation,
                "new_generation": new_generation,
                "switched_at": now,
            }

    def rollback(self, component: Component) -> Optional[Dict]:
        """Rollback to the previous generation.

        Swaps the pointer back to previous_generation. Returns rollback info
        or None if no previous generation exists.
        """
        with self._lock:
            entry = self.state.get(component.value)
            if not entry or not entry.get("previous_generation"):
                self.logger.warning(
                    f"No previous generation to rollback for {component.value}"
                )
                return None

            previous = entry["previous_generation"]
            now = datetime.datetime.utcnow().isoformat()

            self.state[component.value] = {
                "active_generation": previous,
                "previous_generation": entry["active_generation"],
                "updated_at": now,
                "rolled_back_at": now,
            }
            self._save()

            self.logger.info(
                f"Rollback: {component.value} {entry['active_generation']} -> {previous}"
            )
            return {
                "component": component.value,
                "rolled_back_from": entry["active_generation"],
                "rolled_back_to": previous,
                "rolled_back_at": now,
            }


# =============================================================================
# Drain Window — Graceful Drain with Timeout
# =============================================================================


class DrainWindow:
    """Manages the drain period for an active generation being replaced.

    During a drain window:
    - No new work is routed to the old generation
    - Existing tasks are allowed to complete
    - After timeout, remaining tasks are forcefully abandoned
    """

    def __init__(
        self,
        generation_name: str,
        component: Component,
        max_duration_seconds: int = 1800,
    ):
        self.generation_name = generation_name
        self.component = component
        self.max_duration_seconds = max_duration_seconds
        self.logger = logging.getLogger(f"{__name__}.DrainWindow")
        self.start_time: Optional[datetime.datetime] = None
        self.end_time: Optional[datetime.datetime] = None
        self.active_tasks_at_start: int = 0
        self.completed_tasks: int = 0
        self.timed_out: bool = False
        self._lock = threading.Lock()

    def begin(self, active_tasks: int = 0) -> Dict:
        """Start the drain window. Returns initial drain state."""
        with self._lock:
            self.start_time = datetime.datetime.utcnow()
            self.active_tasks_at_start = active_tasks
            self.completed_tasks = 0
            self.timed_out = False
            self.logger.info(
                f"Drain started for {self.component.value}/{self.generation_name} "
                f"with {active_tasks} active tasks, timeout={self.max_duration_seconds}s"
            )
            return {
                "status": "draining",
                "generation": self.generation_name,
                "component": self.component.value,
                "start_time": self.start_time.isoformat(),
                "max_duration_seconds": self.max_duration_seconds,
                "active_tasks_at_start": active_tasks,
            }

    def record_task_completed(self) -> None:
        """Record that one task has completed during drain."""
        with self._lock:
            self.completed_tasks += 1
            self.logger.info(
                f"Task completed during drain: {self.completed_tasks}/{self.active_tasks_at_start}"
            )

    def is_complete(self) -> bool:
        """Check if all tasks have drained."""
        with self._lock:
            if self.active_tasks_at_start == 0:
                return True
            return self.completed_tasks >= self.active_tasks_at_start

    def is_timed_out(self) -> bool:
        """Check if the drain window has exceeded max duration."""
        with self._lock:
            if self.start_time is None:
                return False
            elapsed = (datetime.datetime.utcnow() - self.start_time).total_seconds()
            if elapsed >= self.max_duration_seconds and not self.timed_out:
                self.timed_out = True
                self.logger.warning(
                    f"Drain timed out for {self.component.value}/{self.generation_name} "
                    f"after {elapsed:.0f}s ({self.completed_tasks}/{self.active_tasks_at_start} tasks completed)"
                )
            return self.timed_out

    def finish(self) -> Dict:
        """End the drain window and return summary."""
        with self._lock:
            self.end_time = datetime.datetime.utcnow()
            # Check timeout before finishing
            if self.start_time and not self.timed_out:
                elapsed_check = (self.end_time - self.start_time).total_seconds()
                if elapsed_check >= self.max_duration_seconds:
                    self.timed_out = True
            elapsed = 0
            if self.start_time:
                elapsed = (self.end_time - self.start_time).total_seconds()
            self.logger.info(
                f"Drain finished: {self.component.value}/{self.generation_name} "
                f"timed_out={self.timed_out}, tasks={self.completed_tasks}/{self.active_tasks_at_start}, "
                f"elapsed={elapsed:.0f}s"
            )
            return {
                "status": "drain_timeout" if self.timed_out else "drained",
                "generation": self.generation_name,
                "component": self.component.value,
                "start_time": self.start_time.isoformat() if self.start_time else None,
                "end_time": self.end_time.isoformat(),
                "elapsed_seconds": elapsed,
                "active_tasks_at_start": self.active_tasks_at_start,
                "completed_tasks": self.completed_tasks,
                "timed_out": self.timed_out,
            }


# =============================================================================
# Docker Lifecycle Management
# =============================================================================

class DockerLifecycle:
    """Wraps docker CLI operations for building and running containers."""

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.DockerLifecycle")

    def build(
        self,
        component: Component,
        git_sha: str,
        generation_id: str,
        dockerfile: Optional[Path] = None,
    ) -> str:
        """Build docker image tagged with generation_id."""
        if dockerfile is None:
            dockerfile = Path("/home/nahar/Documents/code/house_designer/company-ops").joinpath(
                f"Dockerfile.{component.value}"
            )

        image_tag = f"{component.value}:{generation_id}"
        self.logger.info(f"Building {image_tag} from {dockerfile} (git_sha={git_sha})")

        try:
            result = subprocess.run(
                [
                    "docker",
                    "build",
                    "-t",
                    image_tag,
                    "-f",
                    str(dockerfile),
                    "--build-arg",
                    f"GIT_SHA={git_sha}",
                    str(dockerfile.parent),
                ],
                capture_output=True,
                text=True,
                timeout=1800,
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout
                self.logger.error(f"Docker build failed: {error_msg}")
                raise DockerError(f"Failed to build {image_tag}: {error_msg}")

            self.logger.info(f"Successfully built {image_tag}")
            return image_tag

        except subprocess.TimeoutExpired:
            raise DockerError(f"Build timeout for {image_tag}")
        except Exception as e:
            raise DockerError(f"Build error for {image_tag}: {str(e)}")

    def start(
        self,
        generation: Generation,
        mode: str = "WARMING",
        port: int = 8000,
    ) -> str:
        """Start container for generation in specified mode."""
        container_name = f"{generation.component.value}-{generation.name}"
        image_tag = f"{generation.component.value}:{generation.name}"

        self.logger.info(f"Starting {container_name} in {mode} mode on port {port}")

        try:
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container_name,
                    "-p",
                    f"{port}:8000",
                    "-e",
                    f"GENERATION={generation.name}",
                    "-e",
                    f"MODE={mode}",
                    "-e",
                    f"COMPONENT={generation.component.value}",
                    "-v",
                    "/company:/company",
                    image_tag,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout
                self.logger.error(f"Docker start failed: {error_msg}")
                raise DockerError(f"Failed to start {container_name}: {error_msg}")

            container_id = result.stdout.strip()
            self.logger.info(f"Started {container_name} (id={container_id})")
            return container_id

        except subprocess.TimeoutExpired:
            raise DockerError(f"Start timeout for {container_name}")
        except Exception as e:
            raise DockerError(f"Start error for {container_name}: {str(e)}")

    def stop(self, generation: Generation) -> None:
        """Stop and remove container for generation."""
        container_name = f"{generation.component.value}-{generation.name}"
        self.logger.info(f"Stopping {container_name}")

        try:
            subprocess.run(
                ["docker", "stop", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )

            subprocess.run(
                ["docker", "rm", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.logger.info(f"Stopped and removed {container_name}")

        except Exception as e:
            self.logger.warning(f"Error stopping {container_name}: {str(e)}")

    def logs(self, generation: Generation, lines: int = 100) -> str:
        """Get recent container logs."""
        container_name = f"{generation.component.value}-{generation.name}"

        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(lines), container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                return result.stdout
            else:
                return f"Failed to get logs: {result.stderr}"

        except Exception as e:
            return f"Error retrieving logs: {str(e)}"


# =============================================================================
# Health Check Framework
# =============================================================================

class HealthCheck:
    """Validates that a running container is healthy."""

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.HealthCheck")

    def check(
        self,
        host: str = "localhost",
        port: int = 8000,
        timeout_sec: int = 30,
    ) -> Dict:
        """GET /health endpoint and return result."""
        url = f"http://{host}:{port}/health"
        start = time.time()

        try:
            response = urllib.request.urlopen(url, timeout=timeout_sec)
            elapsed_ms = (time.time() - start) * 1000

            if response.status == 200:
                return {
                    "status": "OK",
                    "response_time_ms": elapsed_ms,
                    "error_message": None,
                }
            else:
                return {
                    "status": "FAILED",
                    "response_time_ms": elapsed_ms,
                    "error_message": f"HTTP {response.status}",
                }

        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            return {
                "status": "FAILED",
                "response_time_ms": elapsed_ms,
                "error_message": str(e),
            }


# =============================================================================
# Company Database — Hermees Reconciliation Queries
# =============================================================================


class CompanyDB:
    """Lightweight Postgres connector for Hermees state reconciliation checks.

    Uses standard psycopg2 if available; falls back to no-op stubs if not installed.
    """

    def __init__(self, dsn: Optional[str] = None):
        self.logger = logging.getLogger(f"{__name__}.CompanyDB")
        self._dsn = dsn or os.environ.get("COMPANY_DATABASE_URL")
        self._conn = None

    def connect(self) -> bool:
        """Attempt to connect to Postgres. Returns True if successful."""
        if not self._dsn:
            self.logger.info("No COMPANY_DATABASE_URL set, using stub checks")
            return False
        try:
            import psycopg2
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
            self.logger.info("Connected to Company database")
            return True
        except ImportError:
            self.logger.warning("psycopg2 not installed, using stub checks")
            return False
        except Exception as e:
            self.logger.warning(f"Failed to connect to database: {e}")
            return False

    def check_active_deployments(self, component: str) -> Dict:
        """Check if there are other active deployments for this component."""
        if not self._conn:
            return {"connected": False, "active_count": 0, "status": "stub"}
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM deployments WHERE component = %s AND status = 'ACTIVE'",
                (component,),
            )
            count = cur.fetchone()[0]
            return {"connected": True, "active_count": count, "status": "ok"}
        except Exception as e:
            return {"connected": True, "active_count": 0, "status": f"error: {e}"}

    def check_recent_decisions(self, component: str, hours: int = 24) -> Dict:
        """Check recent decisions from the observer schema."""
        if not self._conn:
            return {"connected": False, "count": 0, "status": "stub"}
        try:
            cur = self._conn.cursor()
            cur.execute(
                """SELECT COUNT(*) FROM observer.decisions
                   WHERE created_at > NOW() - INTERVAL '%s hours'""",
                (hours,),
            )
            count = cur.fetchone()[0]
            return {"connected": True, "count": count, "status": "ok"}
        except Exception as e:
            return {"connected": True, "count": 0, "status": f"error: {e}"}

    def check_active_experiments(self) -> Dict:
        """Check for active experiments that might conflict with deployment."""
        if not self._conn:
            return {"connected": False, "count": 0, "status": "stub"}
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM observer.experiments WHERE status = 'active'"
            )
            count = cur.fetchone()[0]
            return {"connected": True, "count": count, "status": "ok"}
        except Exception as e:
            return {"connected": True, "count": 0, "status": f"error: {e}"}

    def check_recent_failures(self, component: str, hours: int = 1) -> Dict:
        """Check for recent failures that might indicate instability."""
        if not self._conn:
            return {"connected": False, "count": 0, "status": "stub"}
        try:
            cur = self._conn.cursor()
            cur.execute(
                """SELECT COUNT(*) FROM observer.failures
                   WHERE created_at > NOW() - INTERVAL '%s hours'""",
                (hours,),
            )
            count = cur.fetchone()[0]
            return {"connected": True, "count": count, "status": "ok"}
        except Exception as e:
            return {"connected": True, "count": 0, "status": f"error: {e}"}

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# =============================================================================
# Synthetic Tests
# =============================================================================


class SyntheticTests:
    """Runs end-to-end validation of a new generation."""

    def __init__(self, company_db: Optional[CompanyDB] = None):
        self.logger = logging.getLogger(f"{__name__}.SyntheticTests")
        self.company_db = company_db

    def engineering_self_test(self, generation: Generation) -> Dict:
        """Simulate a small task dispatch to validate Engineering generation."""
        self.logger.info(f"Running engineering self-test for {generation.name}")

        test_results = {
            "test_name": "engineering_self_test",
            "generation": generation.name,
            "passed": False,
            "details": "",
            "checks": {},
        }

        try:
            # Check 1: Health check
            health_check = HealthCheck()
            health_result = health_check.check()
            test_results["checks"]["health_check"] = health_result["status"] == "OK"
            test_results["details"] += f"Health check: {health_result['status']}\n"

            # Check 2: File system access (simulate by checking logs accessible)
            docker_lifecycle = DockerLifecycle()
            logs = docker_lifecycle.logs(generation)
            test_results["checks"]["filesystem_access"] = bool(logs)
            test_results["details"] += f"Filesystem access: {'OK' if logs else 'FAILED'}\n"

            # Check 3: Git access (would normally verify git commands work)
            # For Phase 1, stub this as passed if health check passed
            test_results["checks"]["git_access"] = test_results["checks"]["health_check"]
            test_results["details"] += "Git access: STUB (would verify git commands)\n"

            # Check 4: Manager start capability (would normally verify manager process can start)
            # For Phase 1, stub this as passed
            test_results["checks"]["manager_start"] = True
            test_results["details"] += "Manager start: STUB (would verify process can start)\n"

            # Overall: pass if health check passed
            test_results["passed"] = test_results["checks"]["health_check"]

            self.logger.info(
                f"Engineering self-test for {generation.name}: "
                f"{'PASSED' if test_results['passed'] else 'FAILED'}"
            )

        except Exception as e:
            test_results["details"] = f"Test error: {str(e)}\n{traceback.format_exc()}"
            self.logger.error(f"Engineering self-test error: {test_results['details']}")

        return test_results

    def hermees_reconciliation(self, generation: Generation) -> Dict:
        """Validate Hermees state coherence and readiness for handover.

        Checks Company PG for active deployments, recent decisions, active
        experiments, and recent failures. If CompanyDB is not connected,
        falls back to stub checks that always pass.
        """
        self.logger.info(f"Running Hermees reconciliation for {generation.name}")

        test_results = {
            "test_name": "hermees_reconciliation",
            "generation": generation.name,
            "handover_accepted": False,
            "details": "",
            "checks": {},
        }

        try:
            # Check 1: Health check
            health_check = HealthCheck()
            health_result = health_check.check()
            test_results["checks"]["health_check"] = health_result["status"] == "OK"
            test_results["details"] += f"Health check: {health_result['status']}\n"

            # Check 2: Company state loading — verify PG connectivity
            if self.company_db and self.company_db._conn:
                try:
                    cur = self.company_db._conn.cursor()
                    cur.execute("SELECT 1")
                    test_results["checks"]["company_state_load"] = True
                    test_results["details"] += "Company state load: PG connected\n"
                except Exception as e:
                    test_results["checks"]["company_state_load"] = False
                    test_results["details"] += f"Company state load: FAILED ({e})\n"
            else:
                test_results["checks"]["company_state_load"] = True
                test_results["details"] += "Company state load: STUB (no PG configured)\n"

            # Check 3: Active deployments — no conflicting active deployments
            if self.company_db:
                deploy_check = self.company_db.check_active_deployments("engineering")
                if deploy_check["connected"]:
                    conflict = deploy_check["active_count"] > 0
                    test_results["checks"]["active_deployments"] = not conflict
                    test_results["details"] += (
                        f"Active deployments: {deploy_check['active_count']} "
                        f"({'CONFLICT' if conflict else 'OK'})\n"
                    )
                else:
                    test_results["checks"]["active_deployments"] = True
                    test_results["details"] += "Active deployments: STUB (no PG)\n"
            else:
                test_results["checks"]["active_deployments"] = True
                test_results["details"] += "Active deployments: STUB (no CompanyDB)\n"

            # Check 4: Recent failures — reject if critical failures in last hour
            if self.company_db:
                fail_check = self.company_db.check_recent_failures("hermees", hours=1)
                if fail_check["connected"]:
                    has_failures = fail_check["count"] > 0
                    test_results["checks"]["recent_failures"] = not has_failures
                    test_results["details"] += (
                        f"Recent failures: {fail_check['count']} "
                        f"({'REJECT' if has_failures else 'OK'})\n"
                    )
                else:
                    test_results["checks"]["recent_failures"] = True
                    test_results["details"] += "Recent failures: STUB (no PG)\n"
            else:
                test_results["checks"]["recent_failures"] = True
                test_results["details"] += "Recent failures: STUB (no CompanyDB)\n"

            # Check 5: Active experiments — warn but don't block
            if self.company_db:
                exp_check = self.company_db.check_active_experiments()
                if exp_check["connected"]:
                    test_results["checks"]["active_experiments"] = True
                    test_results["details"] += (
                        f"Active experiments: {exp_check['count']} (advisory)\n"
                    )
                else:
                    test_results["checks"]["active_experiments"] = True
                    test_results["details"] += "Active experiments: STUB (no PG)\n"
            else:
                test_results["checks"]["active_experiments"] = True
                test_results["details"] += "Active experiments: STUB (no CompanyDB)\n"

            # Overall: handover accepted if health check AND core PG checks pass
            test_results["handover_accepted"] = all([
                test_results["checks"]["health_check"],
                test_results["checks"]["company_state_load"],
                test_results["checks"]["active_deployments"],
                test_results["checks"]["recent_failures"],
            ])

            self.logger.info(
                f"Hermees reconciliation for {generation.name}: "
                f"{'ACCEPTED' if test_results['handover_accepted'] else 'REJECTED'}"
            )

        except Exception as e:
            test_results["details"] = f"Reconciliation error: {str(e)}\n{traceback.format_exc()}"
            self.logger.error(f"Hermees reconciliation error: {test_results['details']}")

        return test_results


# =============================================================================
# State Manager — Orchestrates Lifecycle
# =============================================================================

class StateManager:
    """Orchestrates generation lifecycle and state transitions."""

    def __init__(self, runtime_control: RuntimeControl, checkpoint_store: Optional[CheckpointStore] = None):
        self.runtime_control = runtime_control
        self.checkpoint_store = checkpoint_store or CheckpointStore()
        self.logger = logging.getLogger(f"{__name__}.StateManager")
        self.generations: Dict[str, Generation] = {}
        self.deployments: Dict[str, Deployment] = {}

    def create_generation(
        self,
        component: Component,
        git_sha: str,
        generation_name: Optional[str] = None,
    ) -> Generation:
        """Create a new generation in BUILDING state."""
        if generation_name is None:
            # Auto-generate name (e.g., H1, H2, E1, E2)
            prefix = component.value[0].upper()
            existing = [g for g in self.generations.values() if g.component == component]
            generation_name = f"{prefix}{len(existing) + 1}"

        gen = Generation(
            name=generation_name,
            component=component,
            status=GenerationStatus.BUILDING,
            created_at=datetime.datetime.utcnow(),
        )

        self.generations[generation_name] = gen
        self.logger.info(f"Created generation {generation_name} ({component.value}, git_sha={git_sha})")

        return gen

    def transition(self, generation: Generation, new_status: GenerationStatus) -> None:
        """Move generation to a new status."""
        generation.transition(new_status)
        self.logger.info(f"Generation {generation.name} transitioned to {new_status}")

    def get_deployment_history(self, component: Component) -> List[Deployment]:
        """Get all deployments for a component."""
        return [d for d in self.deployments.values() if d.component == component]

    def create_deployment(
        self,
        component: Component,
        git_sha: str,
        docker_image: str = "",
        generation_name: Optional[str] = None,
        previous_generation: Optional[str] = None,
    ) -> Tuple[Generation, Deployment]:
        """Create a new generation and its deployment record, save checkpoint."""
        gen = self.create_generation(component, git_sha, generation_name)
        deployment = Deployment(
            id=str(uuid.uuid4()),
            component=component,
            generation=gen,
            git_sha=git_sha,
            docker_image=docker_image or f"{component.value}:{gen.name}",
            status=gen.status,
            created_at=datetime.datetime.utcnow(),
            previous_generation=previous_generation,
        )
        self.deployments[deployment.id] = deployment

        checkpoint = StateCheckpoint(
            deployment_id=deployment.id,
            component=component.value,
            generation=gen.name,
            status=gen.status.value,
            created_at=datetime.datetime.utcnow().isoformat(),
        )
        self.checkpoint_store.save_checkpoint(checkpoint)

        self.logger.info(f"Created deployment {deployment.id} for {gen.name}")
        return gen, deployment

    def resume_interrupted_deployments(self) -> List[Dict]:
        """On startup, resume deployments that were interrupted mid-flight.

        Returns a list of resume actions taken.
        """
        resumable = self.checkpoint_store.get_resumable()
        actions = []

        for cp in resumable:
            component = Component(cp.component)
            self.logger.info(
                f"Resuming interrupted deployment: {cp.component}/{cp.generation} "
                f"at status {cp.status}"
            )

            # Recreate the generation from checkpoint
            gen = Generation(
                name=cp.generation,
                component=component,
                status=GenerationStatus(cp.status),
                created_at=datetime.datetime.fromisoformat(cp.created_at),
            )
            self.generations[cp.generation] = gen

            # Determine resume action based on where it stopped
            action = {
                "generation": cp.generation,
                "component": cp.component,
                "stopped_at": cp.status,
                "action": "none",
            }

            status = GenerationStatus(cp.status)
            if status in (GenerationStatus.STARTING, GenerationStatus.WARMING):
                # Was mid-start: retry from STARTING
                action["action"] = "retry_start"
            elif status == GenerationStatus.TESTING:
                # Was mid-test: retry tests
                action["action"] = "retry_test"
            elif status == GenerationStatus.DRAINING:
                # Was mid-drain: check if timed out or finish drain
                if cp.drain_duration_seconds > 0:
                    action["action"] = "check_drain_timeout"
                else:
                    action["action"] = "retry_drain"
            elif status in (
                GenerationStatus.TEST_FAILED,
                GenerationStatus.ACTIVATION_FAILED,
                GenerationStatus.BUILD_FAILED,
            ):
                # Failed states: rollback
                action["action"] = "rollback"
                gen.transition(GenerationStatus.ROLLED_BACK)

            actions.append(action)
            self.logger.info(f"Resume action for {cp.generation}: {action['action']}")

        return actions

    def persist_to_db(
        self,
        generation: Generation,
        deployment: Deployment,
    ) -> None:
        """Persist generation and deployment to database (T1.2 will implement actual DB)."""
        # Phase 1: In-memory storage only; T1.2 will add actual DB persistence
        self.deployments[deployment.id] = deployment
        self.logger.info(f"Persisted deployment {deployment.id} to state")

    def load_from_db(
        self,
        component: Component,
        generation_id: str,
    ) -> Optional[Generation]:
        """Restore generation state from database (T1.2 will implement actual DB)."""
        # Phase 1: In-memory lookup only; T1.2 will add actual DB load
        if generation_id in self.generations:
            return self.generations[generation_id]
        return None


# =============================================================================
# Systemd Socket Listener
# =============================================================================

class SystemdSocketListener:
    """Listens on Unix domain socket for requests from CLI/Hermees/Engineering."""

    def __init__(
        self,
        state_manager: StateManager,
        socket_path: Path = COMPANYD_SOCKET,
        host: str = "127.0.0.1",
        port: int = 8000,
    ):
        self.state_manager = state_manager
        self.socket_path = socket_path
        self.host = host
        self.port = port
        self.logger = logging.getLogger(f"{__name__}.SystemdSocketListener")
        self.running = False

    def start(self) -> None:
        """Start listening for socket connections."""
        self.logger.info(f"Starting socket listener on {self.socket_path}")
        self.running = True

        # Remove old socket file if it exists
        if self.socket_path.exists():
            self.socket_path.unlink()

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.bind(str(self.socket_path))
        server_socket.listen(1)

        self.logger.info(f"Socket listener ready on {self.socket_path}")

        try:
            while self.running:
                try:
                    conn, _ = server_socket.accept()
                    thread = threading.Thread(
                        target=self._handle_client,
                        args=(conn,),
                        daemon=True,
                    )
                    thread.start()
                except KeyboardInterrupt:
                    break
        finally:
            server_socket.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            self.logger.info("Socket listener stopped")

    def _handle_client(self, conn: socket.socket) -> None:
        """Handle a single client request."""
        try:
            data = conn.recv(4096).decode("utf-8")
            request = json.loads(data)

            response = self._process_request(request)

            conn.send(json.dumps(response).encode("utf-8"))
        except json.JSONDecodeError:
            response = {"status": "error", "message": "Invalid JSON"}
            conn.send(json.dumps(response).encode("utf-8"))
        except Exception as e:
            self.logger.error(f"Client handler error: {str(e)}")
            response = {"status": "error", "message": str(e)}
            conn.send(json.dumps(response).encode("utf-8"))
        finally:
            conn.close()

    def _process_request(self, request: Dict) -> Dict:
        """Process a client request and return response."""
        command = request.get("command")
        args = request.get("args", {})

        self.logger.info(f"Processing command: {command} with args: {args}")

        # Phase 1: Handlers are stubs returning success
        # Phase 2 will flesh out actual logic

        if command == "build":
            return self._handle_build(args)
        elif command == "start":
            return self._handle_start(args)
        elif command == "test":
            return self._handle_test(args)
        elif command == "activate":
            return self._handle_activate(args)
        elif command == "drain":
            return self._handle_drain(args)
        elif command == "status":
            return self._handle_status(args)
        elif command == "logs":
            return self._handle_logs(args)
        elif command == "rollback":
            return self._handle_rollback(args)
        else:
            return {"status": "error", "message": f"Unknown command: {command}"}

    def _handle_build(self, args: Dict) -> Dict:
        """Build a new generation image from a git SHA."""
        component_str = args.get("component")
        git_sha = args.get("git_sha")
        if not component_str or not git_sha:
            return {"status": "error", "message": "Requires 'component' and 'git_sha'"}

        try:
            component = Component(component_str)
        except ValueError:
            return {"status": "error", "message": f"Unknown component: {component_str}"}

        try:
            gen, deployment = self.state_manager.create_deployment(
                component=component,
                git_sha=git_sha,
                previous_generation=self.state_manager.runtime_control.get_active(component),
            )

            docker = DockerLifecycle()
            image_tag = docker.build(component, git_sha, gen.name)

            gen.build_start = datetime.datetime.utcnow()
            deployment.build_start = gen.build_start
            self.state_manager.transition(gen, GenerationStatus.STARTING)
            deployment.update_status(GenerationStatus.STARTING)

            self.state_manager.checkpoint_store.save_checkpoint(StateCheckpoint(
                deployment_id=deployment.id,
                component=component.value,
                generation=gen.name,
                status=gen.status.value,
                created_at=deployment.created_at.isoformat(),
            ))

            return {
                "status": "success",
                "command": "build",
                "generation": gen.name,
                "image_tag": image_tag,
                "git_sha": git_sha,
            }
        except DockerError as e:
            return {"status": "error", "command": "build", "message": str(e)}
        except Exception as e:
            return {"status": "error", "command": "build", "message": str(e)}

    def _handle_start(self, args: Dict) -> Dict:
        """Start a generation container in WARMING mode."""
        generation_name = args.get("generation")
        mode = args.get("mode", "WARMING")
        port = args.get("port", 8000)

        if not generation_name:
            return {"status": "error", "message": "Requires 'generation'"}

        gen = self.state_manager.generations.get(generation_name)
        if not gen:
            return {"status": "error", "message": f"Generation {generation_name} not found"}

        try:
            docker = DockerLifecycle()
            container_id = docker.start(gen, mode=mode, port=port)

            gen.build_end = datetime.datetime.utcnow()
            gen.build_start = gen.build_start or gen.build_end
            self.state_manager.transition(gen, GenerationStatus.WARMING)

            deployment = self._find_deployment(gen)
            if deployment:
                deployment.build_end = gen.build_end
                deployment.update_status(GenerationStatus.WARMING)
                self.state_manager.checkpoint_store.save_checkpoint(StateCheckpoint(
                    deployment_id=deployment.id,
                    component=gen.component.value,
                    generation=gen.name,
                    status=gen.status.value,
                    health_check_passed=False,
                    created_at=deployment.created_at.isoformat(),
                ))

            return {
                "status": "success",
                "command": "start",
                "generation": generation_name,
                "container_id": container_id,
                "mode": mode,
            }
        except DockerError as e:
            return {"status": "error", "command": "start", "message": str(e)}
        except Exception as e:
            return {"status": "error", "command": "start", "message": str(e)}

    def _handle_test(self, args: Dict) -> Dict:
        """Run synthetic tests on a generation."""
        generation_name = args.get("generation")
        if not generation_name:
            return {"status": "error", "message": "Requires 'generation'"}

        gen = self.state_manager.generations.get(generation_name)
        if not gen:
            return {"status": "error", "message": f"Generation {generation_name} not found"}

        try:
            self.state_manager.transition(gen, GenerationStatus.TESTING)
            gen.test_start = datetime.datetime.utcnow()

            tests = SyntheticTests()
            if gen.component == Component.ENGINEERING:
                results = tests.engineering_self_test(gen)
            else:
                results = tests.hermees_reconciliation(gen)

            gen.test_end = datetime.datetime.utcnow()
            passed = results.get("passed", False) or results.get("handover_accepted", False)

            if passed:
                self.state_manager.transition(gen, GenerationStatus.READY)
            else:
                self.state_manager.transition(gen, GenerationStatus.TEST_FAILED)

            deployment = self._find_deployment(gen)
            if deployment:
                deployment.test_start = gen.test_start
                deployment.test_end = gen.test_end
                deployment.test_passed = passed
                deployment.self_test_passed = results.get("checks", {}).get("health_check", False)
                deployment.health_check_passed = results.get("checks", {}).get("health_check", False)
                deployment.test_summary = json.dumps(results.get("checks", {}))
                deployment.update_status(gen.status)
                self.state_manager.checkpoint_store.save_checkpoint(StateCheckpoint(
                    deployment_id=deployment.id,
                    component=gen.component.value,
                    generation=gen.name,
                    status=gen.status.value,
                    self_test_passed=deployment.self_test_passed,
                    health_check_passed=deployment.health_check_passed,
                    created_at=deployment.created_at.isoformat(),
                ))

            return {
                "status": "success",
                "command": "test",
                "generation": generation_name,
                "passed": passed,
                "checks": results.get("checks", {}),
                "new_status": gen.status.value,
            }
        except GenerationStateError as e:
            return {"status": "error", "command": "test", "message": str(e)}
        except Exception as e:
            return {"status": "error", "command": "test", "message": str(e)}

    def _handle_activate(self, args: Dict) -> Dict:
        """Atomically switch active generation pointer."""
        generation_name = args.get("generation")
        if not generation_name:
            return {"status": "error", "message": "Requires 'generation'"}

        gen = self.state_manager.generations.get(generation_name)
        if not gen:
            return {"status": "error", "message": f"Generation {generation_name} not found"}

        try:
            # Atomic switch via RuntimeControl
            switch_info = self.state_manager.runtime_control.atomic_switch(
                gen.component, gen.name
            )

            gen.activate_time = datetime.datetime.utcnow()
            self.state_manager.transition(gen, GenerationStatus.ACTIVE)

            deployment = self._find_deployment(gen)
            if deployment:
                deployment.activate_time = gen.activate_time
                deployment.update_status(GenerationStatus.ACTIVE)
                self.state_manager.checkpoint_store.save_checkpoint(StateCheckpoint(
                    deployment_id=deployment.id,
                    component=gen.component.value,
                    generation=gen.name,
                    status=gen.status.value,
                    health_check_passed=deployment.health_check_passed,
                    created_at=deployment.created_at.isoformat(),
                ))

            # Start draining the old generation
            old_name = switch_info.get("previous_generation")
            if old_name and old_name in self.state_manager.generations:
                old_gen = self.state_manager.generations[old_name]
                if old_gen.status == GenerationStatus.ACTIVE:
                    old_gen.transition(GenerationStatus.DRAINING)
                    old_gen.drain_start = datetime.datetime.utcnow()

            return {
                "status": "success",
                "command": "activate",
                "generation": generation_name,
                "previous_generation": switch_info.get("previous_generation"),
                "switched_at": switch_info.get("switched_at"),
            }
        except GenerationStateError as e:
            return {"status": "error", "command": "activate", "message": str(e)}
        except Exception as e:
            return {"status": "error", "command": "activate", "message": str(e)}

    def _handle_drain(self, args: Dict) -> Dict:
        """Drain an active generation with timeout."""
        generation_name = args.get("generation")
        max_duration = args.get("max_duration", 1800)

        if not generation_name:
            return {"status": "error", "message": "Requires 'generation'"}

        gen = self.state_manager.generations.get(generation_name)
        if not gen:
            return {"status": "error", "message": f"Generation {generation_name} not found"}

        try:
            # Start drain if not already draining
            if gen.status == GenerationStatus.ACTIVE:
                self.state_manager.transition(gen, GenerationStatus.DRAINING)
                gen.drain_start = datetime.datetime.utcnow()

            drain_window = DrainWindow(
                gen.name, gen.component, max_duration_seconds=max_duration
            )
            drain_info = drain_window.begin(active_tasks=args.get("active_tasks", 0))

            # Check if drain already timed out
            if drain_window.is_timed_out():
                self.state_manager.transition(gen, GenerationStatus.DRAIN_TIMEOUT)
                drain_info = drain_window.finish()

            deployment = self._find_deployment(gen)
            if deployment:
                deployment.drain_start = gen.drain_start
                deployment.active_tasks_at_drain = drain_info.get("active_tasks_at_start", 0)
                deployment.drain_duration_seconds = max_duration
                deployment.update_status(gen.status)

            return {
                "status": "success",
                "command": "drain",
                "generation": generation_name,
                "drain_info": drain_info,
            }
        except GenerationStateError as e:
            return {"status": "error", "command": "drain", "message": str(e)}
        except Exception as e:
            return {"status": "error", "command": "drain", "message": str(e)}

    def _handle_status(self, args: Dict) -> Dict:
        """Return status of a generation, component, or full system overview."""
        component = args.get("component")
        generation_name = args.get("generation")
        overview = args.get("overview", False)

        if overview:
            result = {"status": "success", "overview": {}}
            for comp in Component:
                active = self.state_manager.runtime_control.get_active(comp)
                gens = [
                    g.to_dict()
                    for g in self.state_manager.generations.values()
                    if g.component == comp
                ]
                result["overview"][comp.value] = {
                    "active_generation": active,
                    "total_generations": len(gens),
                    "generations": gens,
                }
            return result

        if generation_name:
            if generation_name in self.state_manager.generations:
                gen = self.state_manager.generations[generation_name]
                deployment = self._find_deployment(gen)
                result = {
                    "status": "success",
                    "generation": gen.to_dict(),
                }
                if deployment:
                    result["deployment"] = deployment.to_dict()
                return result
            else:
                return {
                    "status": "error",
                    "message": f"Generation {generation_name} not found",
                }

        if component:
            component_obj = Component(component)
            gens = [
                g.to_dict()
                for g in self.state_manager.generations.values()
                if g.component == component_obj
            ]
            active = self.state_manager.runtime_control.get_active(component_obj)
            return {
                "status": "success",
                "component": component,
                "active_generation": active,
                "generations": gens,
            }

        return {
            "status": "error",
            "message": "Must specify component, generation, or overview=true",
        }

    def _handle_logs(self, args: Dict) -> Dict:
        """Get recent container logs for a generation."""
        generation_name = args.get("generation")
        lines = args.get("lines", 100)

        if not generation_name:
            return {"status": "error", "message": "Requires 'generation'"}

        gen = self.state_manager.generations.get(generation_name)
        if not gen:
            return {"status": "error", "message": f"Generation {generation_name} not found"}

        try:
            docker = DockerLifecycle()
            logs = docker.logs(gen, lines=lines)
            return {
                "status": "success",
                "command": "logs",
                "generation": generation_name,
                "lines": lines,
                "logs": logs,
            }
        except Exception as e:
            return {"status": "error", "command": "logs", "message": str(e)}

    def _find_deployment(self, gen: Generation) -> Optional[Deployment]:
        """Find the deployment record for a generation."""
        for d in self.state_manager.deployments.values():
            if d.generation.name == gen.name and d.component == gen.component:
                return d
        return None

    def _handle_rollback(self, args: Dict) -> Dict:
        """Rollback to the previous generation."""
        component_str = args.get("component")
        generation_name = args.get("generation")

        if not component_str:
            return {"status": "error", "message": "Requires 'component'"}

        try:
            component = Component(component_str)
        except ValueError:
            return {"status": "error", "message": f"Unknown component: {component_str}"}

        # If a specific generation is named, roll back that one
        if generation_name:
            gen = self.state_manager.generations.get(generation_name)
            if gen:
                try:
                    gen.transition(GenerationStatus.ROLLED_BACK)
                except GenerationStateError:
                    # Force rollback for terminal failed states
                    if gen.status in (
                        GenerationStatus.TEST_FAILED,
                        GenerationStatus.ACTIVATION_FAILED,
                        GenerationStatus.BUILD_FAILED,
                        GenerationStatus.DRAIN_TIMEOUT,
                    ):
                        gen.status = GenerationStatus.ROLLED_BACK
                    else:
                        return {"status": "error", "message": f"Cannot rollback {generation_name} from {gen.status}"}

        # Atomic rollback via RuntimeControl
        rollback_info = self.state_manager.runtime_control.rollback(component)
        if not rollback_info:
            return {"status": "error", "message": f"No previous generation to rollback for {component_str}"}

        return {
            "status": "success",
            "command": "rollback",
            "rollback": rollback_info,
        }


AVAILABLE_COMMANDS = {
    "build": "Build a new generation image from a git SHA",
    "start": "Start a generation container in WARMING mode",
    "test": "Run synthetic tests on a generation",
    "activate": "Atomically switch active generation pointer",
    "drain": "Drain an active generation (finish existing tasks, then retire)",
    "status": "Get status of a generation or all generations for a component",
    "logs": "Get recent container logs for a generation",
    "rollback": "Rollback to the previous generation",
}


def setup_logging(daemon: bool = False) -> None:
    """Configure logging for companyd. Call once from main()."""
    COMPANYD_HOME.mkdir(parents=True, exist_ok=True)
    COMPANYD_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if daemon:
        handlers.insert(0, logging.FileHandler(COMPANYD_LOG_FILE))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> int:
    """Main companyd entry point."""
    parser = argparse.ArgumentParser(
        prog="companyd",
        description="companyd: Autonomous multi-generation deployment daemon",
        epilog=(
            "Socket commands (via Unix domain socket):\n"
            + "\n".join(
                f"  {cmd:12s} {desc}" for cmd, desc in AVAILABLE_COMMANDS.items()
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"companyd {__version__}",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as background daemon (log to file)",
    )

    args = parser.parse_args()

    setup_logging(daemon=args.daemon)

    # Initialize state management
    runtime_control = RuntimeControl()
    checkpoint_store = CheckpointStore()
    state_manager = StateManager(runtime_control, checkpoint_store)

    # Resume any interrupted deployments from crash recovery
    resume_actions = state_manager.resume_interrupted_deployments()
    if resume_actions:
        logger.info(f"Resumed {len(resume_actions)} interrupted deployment(s)")
        for action in resume_actions:
            logger.info(f"  {action['generation']}: {action['action']}")

    # Load runtime control state
    for component in Component:
        active = runtime_control.get_active(component)
        if active:
            logger.info(f"Active {component.value} generation: {active}")

    # Start socket listener
    listener = SystemdSocketListener(state_manager)
    logger.info(f"Starting companyd {__version__}")

    try:
        listener.start()
    except KeyboardInterrupt:
        logger.info("companyd shutting down")
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}", exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
