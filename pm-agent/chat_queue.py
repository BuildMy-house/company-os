"""In-memory FIFO queue in front of the single pm-agent worker.

The tools read the active user from one context file, so exactly one exchange
may run at a time. Instead of holding HTTP requests open on a lock, requests
become tickets: one worker thread (the only thread that ever runs an exchange,
hence the only writer of the context file) drains them in order. Callers poll
`status()` with an unguessable ticket id; every lookup checks the owning user.

Rules enforced here: one live (queued or running) ticket per user, a bounded
queue, queued tickets expire after `queue_expiry` seconds, queued tickets whose
owner stopped polling for `poll_timeout` seconds are dropped, and finished
results are kept for `result_ttl` seconds. Logs carry counts and timings only,
never message content.
"""

from __future__ import annotations

import collections
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import guard

log = logging.getLogger("pm-agent")

MAX_QUEUE = 5
RESULT_TTL_SECONDS = 120
QUEUE_EXPIRY_SECONDS = 300
POLL_TIMEOUT_SECONDS = 90
WORKER_IDLE_POLL_SECONDS = 5

ProcessFn = Callable[["Ticket"], tuple[int, dict[str, Any]]]

QUEUED = "queued"
ACTIVE = "active"
DONE = "done"


@dataclass
class Ticket:
    id: str
    user: str
    message: str
    enqueued_at: float
    last_poll: float
    state: str = QUEUED
    started_at: float = 0.0
    done_at: float = 0.0
    result: tuple[int, dict[str, Any]] | None = None
    finished: threading.Event = field(default_factory=threading.Event)


@dataclass
class Submission:
    # accepted: new ticket; idle => position 0 (runs now), else 1-based place.
    # already_queued: user's existing queued ticket. processing: user's earlier
    # message is running. full: queue at capacity, nothing stored.
    kind: str
    ticket: Ticket | None = None
    position: int = 0


class ChatQueue:
    def __init__(
        self,
        process: ProcessFn,
        max_queue: int = MAX_QUEUE,
        result_ttl: float = RESULT_TTL_SECONDS,
        queue_expiry: float = QUEUE_EXPIRY_SECONDS,
        poll_timeout: float = POLL_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        start_worker: bool = True,
    ) -> None:
        self._process = process
        self.max_queue = max_queue
        self.result_ttl = result_ttl
        self.queue_expiry = queue_expiry
        self.poll_timeout = poll_timeout
        self._clock = clock
        self._cond = threading.Condition()
        self._pending: collections.deque[Ticket] = collections.deque()
        self._active: Ticket | None = None
        self._tickets: dict[str, Ticket] = {}
        self._live_by_user: dict[str, Ticket] = {}
        self._stop = False
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(
                target=self._worker, name="pm-chat-worker", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- public API --------------------------------------------------------

    def submit(self, user: str, message: str) -> Submission:
        with self._cond:
            self._reap()
            live = self._live_by_user.get(user)
            if live is not None:
                if live.state == ACTIVE:
                    return Submission("processing", live)
                return Submission("already_queued", live, self._position(live))
            if self._active is not None and len(self._pending) >= self.max_queue:
                self._log("rejected_full")
                return Submission("full")
            now = self._clock()
            idle = self._active is None and not self._pending
            ticket = Ticket(secrets.token_urlsafe(24), user, message, now, now)
            self._tickets[ticket.id] = ticket
            self._live_by_user[user] = ticket
            self._pending.append(ticket)
            self._cond.notify_all()
            position = 0 if idle else len(self._pending)
            self._log("enqueued", position=position)
            return Submission("accepted", ticket, position)

    def status(self, ticket_id: str, user: str) -> dict[str, Any]:
        """Poll a ticket. Unknown, expired and foreign tickets all look alike."""
        with self._cond:
            self._reap()
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket.user != user:
                return {"status": "expired"}
            if ticket.state == DONE:
                assert ticket.result is not None
                return {"status": "done", "reply": ticket.result[1].get("reply", "")}
            ticket.last_poll = self._clock()
            if ticket.state == ACTIVE:
                return {"status": "processing"}
            return {"status": "queued", "position": self._position(ticket)}

    def wait(self, ticket: Ticket, timeout: float) -> tuple[int, dict[str, Any]] | None:
        """Block until the ticket finished (result) or timeout (None)."""
        if ticket.finished.wait(timeout):
            return ticket.result
        return None

    def queue_length(self) -> int:
        with self._cond:
            return len(self._pending)

    def is_idle(self) -> bool:
        """No queued tickets and no exchange running."""
        with self._cond:
            return not self._pending and self._active is None

    # -- internals ---------------------------------------------------------

    def _position(self, ticket: Ticket) -> int:
        return list(self._pending).index(ticket) + 1

    def _reap(self) -> None:
        now = self._clock()
        for ticket in list(self._pending):
            if (
                now - ticket.enqueued_at > self.queue_expiry
                or now - ticket.last_poll > self.poll_timeout
            ):
                self._pending.remove(ticket)
                self._tickets.pop(ticket.id, None)
                if self._live_by_user.get(ticket.user) is ticket:
                    del self._live_by_user[ticket.user]
                self._log("dropped", reason="expired_or_abandoned")
        for ticket in list(self._tickets.values()):
            if ticket.state == DONE and now - ticket.done_at > self.result_ttl:
                del self._tickets[ticket.id]

    def _log(self, action: str, **extra: Any) -> None:
        log.info(
            json.dumps(
                {
                    "event": "chat_queue",
                    "action": action,
                    "queue_len": len(self._pending),
                    "busy": self._active is not None,
                    **extra,
                }
            )
        )

    def _worker(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        return
                    self._reap()
                    if self._pending:
                        ticket = self._pending.popleft()
                        break
                    self._cond.wait(timeout=WORKER_IDLE_POLL_SECONDS)
                ticket.state = ACTIVE
                ticket.started_at = self._clock()
                self._active = ticket
                self._log(
                    "started", wait_seconds=round(ticket.started_at - ticket.enqueued_at, 2)
                )
            try:
                result = self._process(ticket)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("chat worker failed")
                result = (503, {"error": "agent unavailable", "reply": guard.UNAVAILABLE_REPLY})
            with self._cond:
                now = self._clock()
                ticket.result = result
                ticket.state = DONE
                ticket.done_at = now
                self._active = None
                if self._live_by_user.get(ticket.user) is ticket:
                    del self._live_by_user[ticket.user]
                self._log("finished", run_seconds=round(now - ticket.started_at, 2))
            ticket.finished.set()
