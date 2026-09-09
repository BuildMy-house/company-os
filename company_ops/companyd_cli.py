#!/usr/bin/env python3
"""
companyd CLI tool — interface for deployment management and status queries.

Connects to companyd daemon via Unix socket for remote commands.
Handles database initialization and deployment history queries via SQLite.
"""

import argparse
import json
import os
import socket
import sqlite3
import sys
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Dict, List


# Configuration
DEFAULT_SOCKET_PATH = "/var/run/companyd.sock"
DEFAULT_DB_PATH = Path.home() / ".company/companyd.db"
SOCKET_TIMEOUT = 5.0


# Exception classes
class SocketClientError(Exception):
    """Base exception for socket communication errors."""
    pass


class DaemonUnavailableError(SocketClientError):
    """Daemon cannot be reached via socket."""
    pass


class DeploymentError(Exception):
    """Deployment-related error."""
    pass


class DeploymentNotFoundError(DeploymentError):
    """Deployment record not found."""
    pass


class DatabaseError(Exception):
    """Database operation error."""
    pass


@dataclass
class DeploymentRecord:
    """Represents a deployment record."""
    id: str
    component: str
    generation: str
    git_sha: str
    status: str
    created_at: str
    docker_image: Optional[str] = None
    requested_by: Optional[str] = None
    reason: Optional[str] = None
    build_start: Optional[str] = None
    build_end: Optional[str] = None
    test_start: Optional[str] = None
    test_end: Optional[str] = None
    activate_time: Optional[str] = None
    drain_start: Optional[str] = None
    drain_end: Optional[str] = None
    previous_generation: Optional[str] = None
    rollback_required: bool = False
    rollback_reason: Optional[str] = None
    tests_passed: Optional[bool] = None
    test_summary: Optional[str] = None
    self_test_passed: Optional[bool] = None
    health_check_passed: Optional[bool] = None
    active_tasks_at_drain: Optional[int] = None
    drain_duration_seconds: Optional[int] = None
    human_intervention_required: bool = False
    notes: Optional[str] = None


class SocketClient:
    """Handles communication with companyd daemon via Unix socket."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH) -> None:
        """Initialize socket client with path to companyd socket.

        Args:
            socket_path: Path to Unix domain socket.
        """
        self.socket_path = socket_path

    def send_command(self, command: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send command to daemon and return response.

        Args:
            command: Command name (e.g., 'status', 'build').
            args: Optional command arguments.

        Returns:
            Response dict from daemon.

        Raises:
            DaemonUnavailableError: If daemon socket is unreachable.
            SocketClientError: If communication fails.
        """
        if args is None:
            args = {}

        request = {"command": command, "args": args}
        request_json = json.dumps(request)

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect(self.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise DaemonUnavailableError(
                f"companyd daemon not running (socket {self.socket_path} unreachable)"
            ) from e

        try:
            sock.sendall(request_json.encode() + b"\n")
            response_data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_data += chunk
        except socket.timeout:
            raise SocketClientError(f"Socket timeout after {SOCKET_TIMEOUT}s") from None
        except OSError as e:
            raise SocketClientError(f"Socket communication error: {e}") from e
        finally:
            sock.close()

        try:
            response = json.loads(response_data.decode().strip())
            return response
        except json.JSONDecodeError as e:
            raise SocketClientError(f"Invalid JSON response from daemon: {e}") from e


class DeploymentClient:
    """High-level client for deployment operations."""

    def __init__(
        self,
        socket_client: Optional[SocketClient] = None,
        db_path: Optional[Path] = None,
    ) -> None:
        """Initialize deployment client.

        Args:
            socket_client: Optional SocketClient instance (created if None).
            db_path: Optional path to SQLite database.
        """
        self.socket_client = socket_client or SocketClient()
        self.db_path = db_path or DEFAULT_DB_PATH

    def _get_db_connection(self) -> sqlite3.Connection:
        """Get database connection with row factory.

        Returns:
            SQLite connection object.

        Raises:
            DatabaseError: If connection fails.
        """
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            raise DatabaseError(f"Database connection error: {e}") from e

    def init_database(self) -> None:
        """Initialize database with schema."""
        schema_path = Path(__file__).parent.parent / "sql" / "deployments.sql"
        if not schema_path.exists():
            raise DatabaseError(f"Schema file not found: {schema_path}")

        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            with open(schema_path) as f:
                schema_sql = f.read()
            cursor.executescript(schema_sql)
            conn.commit()
            conn.close()
        except (sqlite3.Error, OSError) as e:
            raise DatabaseError(f"Database initialization failed: {e}") from e

    def record_deployment(
        self,
        component: str,
        generation: str,
        git_sha: str,
        status: str,
        **kwargs: Any,
    ) -> str:
        """Record a deployment event in database.

        Args:
            component: 'hermees' or 'engineering'.
            generation: Generation identifier (e.g., 'H18', 'E43').
            git_sha: Full commit hash.
            status: Current deployment status.
            **kwargs: Additional columns (docker_image, requested_by, reason, etc.).

        Returns:
            Deployment ID.

        Raises:
            DatabaseError: If insert fails.
        """
        deployment_id = str(uuid.uuid4())
        created_at = datetime.utcnow().isoformat()

        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            columns = [
                "id",
                "component",
                "generation",
                "git_sha",
                "status",
                "created_at",
            ]
            values = [deployment_id, component, generation, git_sha, status, created_at]

            for key, val in kwargs.items():
                columns.append(key)
                values.append(val)

            placeholders = ",".join(["?"] * len(values))
            columns_str = ",".join(columns)

            cursor.execute(
                f"INSERT INTO deployments ({columns_str}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
            conn.close()

            return deployment_id
        except sqlite3.Error as e:
            raise DatabaseError(f"Failed to record deployment: {e}") from e

    def get_deployment_history(
        self, component: str, limit: int = 10
    ) -> List[DeploymentRecord]:
        """Retrieve deployment history for a component.

        Args:
            component: 'hermees' or 'engineering'.
            limit: Number of records to return.

        Returns:
            List of DeploymentRecord objects.

        Raises:
            DatabaseError: If query fails.
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT * FROM deployments
                WHERE component = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (component, limit),
            )

            rows = cursor.fetchall()
            conn.close()

            records = []
            for row in rows:
                record = DeploymentRecord(
                    id=row["id"],
                    component=row["component"],
                    generation=row["generation"],
                    git_sha=row["git_sha"],
                    status=row["status"],
                    created_at=row["created_at"],
                    docker_image=row["docker_image"],
                    requested_by=row["requested_by"],
                    reason=row["reason"],
                    build_start=row["build_start"],
                    build_end=row["build_end"],
                    test_start=row["test_start"],
                    test_end=row["test_end"],
                    activate_time=row["activate_time"],
                    drain_start=row["drain_start"],
                    drain_end=row["drain_end"],
                    previous_generation=row["previous_generation"],
                    rollback_required=bool(row["rollback_required"]),
                    rollback_reason=row["rollback_reason"],
                    tests_passed=row["tests_passed"],
                    test_summary=row["test_summary"],
                    self_test_passed=row["self_test_passed"],
                    health_check_passed=row["health_check_passed"],
                    active_tasks_at_drain=row["active_tasks_at_drain"],
                    drain_duration_seconds=row["drain_duration_seconds"],
                    human_intervention_required=bool(row["human_intervention_required"]),
                    notes=row["notes"],
                )
                records.append(record)
            return records
        except sqlite3.Error as e:
            raise DatabaseError(f"Failed to retrieve deployment history: {e}") from e

    def get_runtime_control(self) -> Dict[str, str]:
        """Get current active generations.

        Returns:
            Dict mapping component to active generation.

        Raises:
            DatabaseError: If query fails.
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT component, active_generation FROM runtime_control")
            rows = cursor.fetchall()
            conn.close()

            return {row["component"]: row["active_generation"] for row in rows}
        except sqlite3.Error as e:
            raise DatabaseError(f"Failed to retrieve runtime control: {e}") from e


class CommandHandler:
    """Handles individual CLI commands."""

    def __init__(
        self,
        socket_client: Optional[SocketClient] = None,
        db_path: Optional[Path] = None,
    ) -> None:
        """Initialize command handler.

        Args:
            socket_client: Optional SocketClient instance.
            db_path: Optional database path.
        """
        self.deployment_client = DeploymentClient(socket_client, db_path)

    def status(self) -> Dict[str, Any]:
        """Get current deployment status from daemon.

        Returns:
            Status dict with active generations and recent deployments.

        Raises:
            DaemonUnavailableError: If daemon cannot be reached.
        """
        return self.deployment_client.socket_client.send_command("status")

    def build(self, component: str, git_sha: str) -> Dict[str, Any]:
        """Trigger a build.

        Args:
            component: 'hermees' or 'engineering'.
            git_sha: Commit to build.

        Returns:
            Response from daemon.

        Raises:
            DaemonUnavailableError: If daemon unreachable.
        """
        return self.deployment_client.socket_client.send_command(
            "build",
            {"component": component, "git_sha": git_sha},
        )

    def deploy(self, component: str, git_sha: str) -> Dict[str, Any]:
        """Trigger a full deployment.

        Args:
            component: 'hermees' or 'engineering'.
            git_sha: Commit to deploy.

        Returns:
            Response from daemon.

        Raises:
            DaemonUnavailableError: If daemon unreachable.
        """
        return self.deployment_client.socket_client.send_command(
            "deploy",
            {"component": component, "git_sha": git_sha},
        )

    def rollback(self, component: str) -> Dict[str, Any]:
        """Trigger a rollback.

        Args:
            component: 'hermees' or 'engineering'.

        Returns:
            Response from daemon.

        Raises:
            DaemonUnavailableError: If daemon unreachable.
        """
        return self.deployment_client.socket_client.send_command(
            "rollback", {"component": component}
        )

    def get_logs(self, component: str, generation: str) -> Dict[str, Any]:
        """Retrieve logs for a generation.

        Args:
            component: 'hermees' or 'engineering'.
            generation: Generation identifier.

        Returns:
            Response from daemon.

        Raises:
            DaemonUnavailableError: If daemon unreachable.
        """
        return self.deployment_client.socket_client.send_command(
            "get-logs",
            {"component": component, "generation": generation},
        )

    def history(self, component: str, limit: int = 10) -> Dict[str, Any]:
        """Get deployment history.

        Args:
            component: 'hermees' or 'engineering'.
            limit: Number of records.

        Returns:
            Dict with history records.
        """
        records = self.deployment_client.get_deployment_history(component, limit)
        return {
            "component": component,
            "count": len(records),
            "deployments": [asdict(r) for r in records],
        }

    def db_schema(self) -> str:
        """Get database schema.

        Returns:
            Schema SQL as string.
        """
        schema_path = Path(__file__).parent.parent / "sql" / "deployments.sql"
        if not schema_path.exists():
            raise DatabaseError(f"Schema file not found: {schema_path}")
        with open(schema_path) as f:
            return f.read()

    def db_init(self) -> Dict[str, Any]:
        """Initialize database.

        Returns:
            Status dict.
        """
        self.deployment_client.init_database()
        return {"status": "OK", "message": f"Database initialized at {self.deployment_client.db_path}"}


def format_table(records: List[Dict[str, Any]], keys: List[str]) -> str:
    """Format records as ASCII table.

    Args:
        records: List of dicts to format.
        keys: Keys to include (in order).

    Returns:
        Formatted table string.
    """
    if not records:
        return "(no records)"

    # Calculate column widths
    widths = {k: len(k) for k in keys}
    for record in records:
        for key in keys:
            val = str(record.get(key, ""))
            widths[key] = max(widths[key], len(val))

    # Header
    header = " | ".join(k.ljust(widths[k]) for k in keys)
    separator = "-+-".join("-" * widths[k] for k in keys)

    # Rows
    rows = [header, separator]
    for record in records:
        row = " | ".join(str(record.get(k, "")).ljust(widths[k]) for k in keys)
        rows.append(row)

    return "\n".join(rows)


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="companyd CLI — deployment management and status"
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to companyd database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--socket",
        default=DEFAULT_SOCKET_PATH,
        help=f"Path to companyd socket (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON (for scripting)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # status command
    subparsers.add_parser("status", help="Show current deployment status")

    # build command
    build_parser = subparsers.add_parser("build", help="Trigger a build")
    build_parser.add_argument("component", choices=["hermees", "engineering"])
    build_parser.add_argument("git_sha", help="Git commit SHA to build")

    # deploy command
    deploy_parser = subparsers.add_parser("deploy", help="Trigger a full deployment")
    deploy_parser.add_argument("component", choices=["hermees", "engineering"])
    deploy_parser.add_argument("git_sha", help="Git commit SHA to deploy")

    # rollback command
    rollback_parser = subparsers.add_parser("rollback", help="Trigger a rollback")
    rollback_parser.add_argument("component", choices=["hermees", "engineering"])

    # get-logs command
    logs_parser = subparsers.add_parser("get-logs", help="Retrieve container logs")
    logs_parser.add_argument("component", choices=["hermees", "engineering"])
    logs_parser.add_argument("generation", help="Generation identifier (e.g., H18, E43)")

    # history command
    history_parser = subparsers.add_parser("history", help="Show deployment history")
    history_parser.add_argument("component", choices=["hermees", "engineering"])
    history_parser.add_argument(
        "--limit", type=int, default=10, help="Number of records (default: 10)"
    )

    # db-schema command
    subparsers.add_parser("db-schema", help="Print database schema")

    # db-init command
    db_init_parser = subparsers.add_parser("db-init", help="Initialize database")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Create socket client and command handler
    socket_client = SocketClient(args.socket)
    handler = CommandHandler(socket_client, args.db_path)

    try:
        result = None

        if args.command == "status":
            result = handler.status()

        elif args.command == "build":
            result = handler.build(args.component, args.git_sha)

        elif args.command == "deploy":
            result = handler.deploy(args.component, args.git_sha)

        elif args.command == "rollback":
            result = handler.rollback(args.component)

        elif args.command == "get-logs":
            result = handler.get_logs(args.component, args.generation)

        elif args.command == "history":
            result = handler.history(args.component, args.limit)

        elif args.command == "db-schema":
            schema = handler.db_schema()
            if args.json:
                result = {"schema": schema}
            else:
                print(schema)
                return 0

        elif args.command == "db-init":
            result = handler.db_init()

        # Output result
        if result is not None:
            if args.json:
                print(json.dumps(result, indent=2, default=str))
            else:
                if args.command == "history":
                    deployments = result.get("deployments", [])
                    keys = ["id", "generation", "status", "created_at", "git_sha"]
                    print(f"\n{result['component']} deployment history (latest {result['count']})")
                    print(format_table(deployments, keys))
                elif args.command == "status":
                    active = result.get("active_generations", {})
                    recent = result.get("recent_deployments", {})

                    if active:
                        print("\nActive generations:")
                        for comp, gen in active.items():
                            print(f"  {comp}: {gen}")
                    else:
                        print("(no active generations recorded)")

                    for component in ["hermees", "engineering"]:
                        deployments = recent.get(component, [])
                        if deployments:
                            keys = ["generation", "status", "created_at"]
                            print(f"\n{component} (recent)")
                            print(format_table(deployments[:5], keys))
                else:
                    print(json.dumps(result, indent=2, default=str))

        return 0

    except DaemonUnavailableError as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    except (SocketClientError, DatabaseError, DeploymentError) as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
