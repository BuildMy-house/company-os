from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class Telemetry:
    def __init__(self, path: str | Path = "telemetry.jsonl") -> None:
        self.path = Path(path)

    def emit(self, event: str, **fields: object) -> str:
        trace_id = str(fields.pop("trace_id", uuid.uuid4()))
        record = {"trace_id": trace_id, "event": event, "timestamp": time.time(), **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        return trace_id
