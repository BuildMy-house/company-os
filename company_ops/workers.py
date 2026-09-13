from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .mcp_client import MCPClient, MCPServer
from .routing import choose_provider


@dataclass(frozen=True)
class WorkerResult:
    worker: str
    task_type: str
    status: str
    provider: str
    model_tier: str
    result: object | None = None
    error: str | None = None
    dry_run: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def load_workers(path: str | Path) -> dict[str, MCPServer]:
    """Load role names, allowing the same MCP worker to back many roles."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        name: MCPServer(tuple(spec["command"]), spec["tool"], spec.get("env"),
                        spec.get("arguments"), spec.get("poll_tool"), spec.get("poll_interval", 2))
        for name, spec in data.items()
    }


def run_worker(worker: str, task_type: str, prompt: str, workers: dict[str, MCPServer],
               free_available: bool = True, dry_run: bool = True, timeout: float = 120) -> WorkerResult:
    route = choose_provider(task_type, free_available)
    selected = route["provider"] if worker == "auto" else worker
    server = workers.get(selected)
    if server is None:
        return WorkerResult(selected, task_type, "failed", route["provider"], route["model_tier"], error="worker is not configured")
    if dry_run:
        return WorkerResult(selected, task_type, "planned", selected, route["model_tier"], dry_run=True)
    try:
        arguments = {"task_type": task_type, "prompt": prompt}
        if server.arguments:
            arguments = _render(server.arguments, task_type=task_type, prompt=prompt)
        result = MCPClient(server, timeout).call(arguments)
        return WorkerResult(selected, task_type, "success", selected, route["model_tier"], result=result)
    except Exception as exc:
        return WorkerResult(selected, task_type, "failed", selected, route["model_tier"], error=str(exc))


def _render(value: object, **fields: str) -> object:
    if isinstance(value, str):
        return value.format(**fields)
    if isinstance(value, list):
        return [_render(item, **fields) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, **fields) for key, item in value.items()}
    return value
