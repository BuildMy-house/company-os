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
        }


# =============================================================================
# Runtime Control — Atomic Generation Pointer
# =============================================================================

class RuntimeControl:
    """Manages atomic active-generation pointer per component with file-based locking."""

    def __init__(self, state_file: Path = COMPANYD_RUNTIME_CONTROL_FILE):
        self.state_file = state_file
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
            import urllib.request

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
# Synthetic Tests
# =============================================================================

class SyntheticTests:
    """Runs end-to-end validation of a new generation."""

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.SyntheticTests")

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
        """Validate Hermees state coherence and readiness for handover."""
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

            # Check 2: Company state loading (would verify state can be loaded from PG)
            test_results["checks"]["company_state_load"] = True
            test_results["details"] += "Company state load: STUB (would verify PG connectivity)\n"

            # Check 3: Active projects verification
            test_results["checks"]["active_projects"] = True
            test_results["details"] += "Active projects: STUB (would enumerate projects)\n"

            # Check 4: Engineering state verification
            test_results["checks"]["engineering_state"] = True
            test_results["details"] += "Engineering state: STUB (would verify compatibility)\n"

            # Overall: handover accepted if health check passed
            test_results["handover_accepted"] = test_results["checks"]["health_check"]

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

    def __init__(self, runtime_control: RuntimeControl):
        self.runtime_control = runtime_control
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
        else:
            return {"status": "error", "message": f"Unknown command: {command}"}

    def _handle_build(self, args: Dict) -> Dict:
        """Phase 1: Stub for build command."""
        return {
            "status": "success",
            "command": "build",
            "message": "Build command received (stub for Phase 1)",
            "component": args.get("component"),
            "git_sha": args.get("git_sha"),
        }

    def _handle_start(self, args: Dict) -> Dict:
        """Phase 1: Stub for start command."""
        return {
            "status": "success",
            "command": "start",
            "message": "Start command received (stub for Phase 1)",
            "generation": args.get("generation"),
            "mode": args.get("mode", "WARMING"),
        }

    def _handle_test(self, args: Dict) -> Dict:
        """Phase 1: Stub for test command."""
        return {
            "status": "success",
            "command": "test",
            "message": "Test command received (stub for Phase 1)",
            "generation": args.get("generation"),
        }

    def _handle_activate(self, args: Dict) -> Dict:
        """Phase 1: Stub for activate command."""
        return {
            "status": "success",
            "command": "activate",
            "message": "Activate command received (stub for Phase 1)",
            "generation": args.get("generation"),
        }

    def _handle_drain(self, args: Dict) -> Dict:
        """Phase 1: Stub for drain command."""
        return {
            "status": "success",
            "command": "drain",
            "message": "Drain command received (stub for Phase 1)",
            "generation": args.get("generation"),
            "max_duration": args.get("max_duration", 1800),
        }

    def _handle_status(self, args: Dict) -> Dict:
        """Phase 1: Return status of a generation or component."""
        component = args.get("component")
        generation_name = args.get("generation")

        if generation_name:
            if generation_name in self.state_manager.generations:
                gen = self.state_manager.generations[generation_name]
                return {
                    "status": "success",
                    "generation": gen.to_dict(),
                }
            else:
                return {
                    "status": "error",
                    "message": f"Generation {generation_name} not found",
                }

        if component:
            component_obj = Component(component)
            gens = [g for g in self.state_manager.generations.values() if g.component == component_obj]
            return {
                "status": "success",
                "component": component,
                "generations": [g.to_dict() for g in gens],
            }

        return {
            "status": "error",
            "message": "Must specify either component or generation",
        }

    def _handle_logs(self, args: Dict) -> Dict:
        """Phase 1: Stub for logs command."""
        return {
            "status": "success",
            "command": "logs",
            "message": "Logs command received (stub for Phase 1)",
            "generation": args.get("generation"),
            "lines": args.get("lines", 100),
        }


AVAILABLE_COMMANDS = {
    "build": "Build a new generation image from a git SHA",
    "start": "Start a generation container in WARMING mode",
    "test": "Run synthetic tests on a generation",
    "activate": "Atomically switch active generation pointer",
    "drain": "Drain an active generation (finish existing tasks, then retire)",
    "status": "Get status of a generation or all generations for a component",
    "logs": "Get recent container logs for a generation",
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
    state_manager = StateManager(runtime_control)

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
