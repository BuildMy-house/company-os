from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class AxiomClient:
    """Minimal Axiom query client using only stdlib (no requests/httpx)."""

    def __init__(
        self,
        token: str | None = None,
        endpoint: str | None = None,
        dataset: str | None = None,
    ) -> None:
        self.token = token or os.environ.get("AXIOM_TOKEN", "")
        self.endpoint = (endpoint or os.environ.get("AXIOM_ENDPOINT", "https://api.axiom.co")).rstrip("/")
        self.dataset = dataset or os.environ.get("AXIOM_DATASET", "homely-telemetry")

    def query(self, aql: str, *, timeframe: str = "24h") -> dict:
        """Run an AQL query and return the raw result dict."""
        url = f"{self.endpoint}/api/v1/datasets/{self.dataset}/_search"
        payload = json.dumps({"query": aql, "timeframe": timeframe}).encode()
        req = Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"Axiom query failed ({exc.code}): {body}") from exc

    def summary(self, metric: str = "errors", *, timeframe: str = "7d") -> dict:
        """Pre-built summary queries for common metrics."""
        queries = {
            "errors": (
                "['error.caught'] | stats count() as cnt by message | sort cnt desc | head 20"
            ),
            "performance": (
                "['perf.frame_time'] | stats "
                "avg(p50) as avg_p50, avg(p95) as avg_p95, avg(p99) as avg_p99 "
                "by bin(1h, ts)"
            ),
            "usage": (
                "['tool.switch', 'feature.undo', 'feature.redo', 'feature.room_add'] "
                "| stats count() as cnt by event | sort cnt desc"
            ),
        }
        aql = queries.get(metric)
        if not aql:
            raise ValueError(f"Unknown metric: {metric}. Choose from: {', '.join(queries)}")
        return self.query(aql, timeframe=timeframe)

    def list_datasets(self) -> list[str]:
        """List available Axiom datasets."""
        url = f"{self.endpoint}/api/v1/datasets"
        req = Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                return [ds["name"] for ds in data.get("datasets", [])]
        except HTTPError as exc:
            raise RuntimeError(f"Failed to list datasets: {exc.code}") from exc
