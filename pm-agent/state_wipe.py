"""Idle wipe of Hermes on-disk state so nothing carries across conversations.

The api_server chat path is stateless (no session header is ever sent) and
memory/skills/curator are disabled in hermes-config.yaml; this is the belt and
braces: while the front door is idle, delete any per-session or learned-state
directories Hermes may have written under $HERMES_HOME. Config.yaml, SOUL.md
and state.db (held open by Hermes, pruned via sessions.retention_days) are
never touched.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("pm-agent.wipe")

WIPE_DIRS = ("sessions", "memories", "skills", "cron", "checkpoints")
IDLE_SECONDS = 300
CHECK_INTERVAL_SECONDS = 60

_last_activity = time.monotonic()


def touch_activity() -> None:
    global _last_activity
    _last_activity = time.monotonic()


def seconds_idle() -> float:
    return time.monotonic() - _last_activity


def wipe(home: str | Path) -> list[str]:
    """Remove the per-session dirs under `home`; return what was removed."""
    removed: list[str] = []
    base = Path(home)
    for name in WIPE_DIRS:
        target = base / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
    return removed


def run_when_idle(
    home: str,
    context_path: str,
    stop: threading.Event,
    sleep: Callable[[float], bool] | None = None,
    is_idle: Callable[[], bool] = lambda: True,
) -> None:
    """Background loop: wipe once per idle period.

    Idle means: no request for IDLE_SECONDS, the chat queue is empty with no
    exchange running (`is_idle`), and no request context file exists.
    """
    wait = sleep or stop.wait
    wiped_since_activity = False
    while not wait(CHECK_INTERVAL_SECONDS):
        if (
            seconds_idle() < IDLE_SECONDS
            or not is_idle()
            or Path(context_path).exists()
        ):
            wiped_since_activity = False
            continue
        if wiped_since_activity:
            continue
        removed = wipe(home)
        wiped_since_activity = True
        if removed:
            log.info("wiped hermes state dirs: %s", ",".join(removed))
