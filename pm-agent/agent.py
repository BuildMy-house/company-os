"""buildmy.house pm-agent: narrow product-feedback chat service.

Records end-user messages (PmStore), deduplicates them against
company.feedback_signals, and files Hive work items only for genuinely new
signals or escalations past ESCALATION_THRESHOLD. Persona and boundaries:
pm-agent/SOUL.md.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from company_ops.hive_client import HiveClientError, file_feedback_work
from company_ops.pm_store import PmStore

ESCALATION_THRESHOLD = 5
DEFAULT_PORT = 4200
DEFAULT_HIVE_URL = "http://hive-coordinator:4100"

log = logging.getLogger("pm-agent")


def _reply_new(topic: str) -> str:
    return (
        "Thanks — that's the first report of this I've seen, so I've filed "
        "it as a new signal and opened a work item for the team. "
        f"Logged as: {topic}"
    )


def _reply_known(occurrence_count: int, status: str) -> str:
    return (
        "Already known — this has been reported before and is being "
        f"tracked. Current status: {status}, with {occurrence_count} "
        "similar report(s) so far. No new ticket needed."
    )


def _reply_escalated(occurrence_count: int) -> str:
    return (
        "Thanks — this has now been reported enough times "
        f"({occurrence_count} similar reports) that I've escalated it with "
        "a fresh work item for the team."
    )


def _reply_recorded() -> str:
    return "Noted — I've recorded your message."


def ask_hermes_bounded_question(question: str) -> str:
    """Bounded Hermes Q&A — integration point deliberately NOT wired.

    The real interface exists and landed with PM-D:

        company_ops.agent_query_interface.receive_agent_query(
            writer, asker_id="pm-agent", question=question)

    It is not called from this service because receive_agent_query requires
    an ObserverWriter (observer.decisions write access). pm_agent_writer --
    the only Postgres role this service holds -- is scoped to
    company.pm_conversations and company.feedback_signals only. Enabling
    this means granting observer credentials, a deliberate escalation
    outside this service's least-privilege boundary. Until then, this
    raises rather than silently skipping the audit record.
    """
    raise NotImplementedError(
        "Hermes Q&A integration point is "
        "company_ops.agent_query_interface.receive_agent_query; not wired "
        "because it requires observer-schema credentials outside the "
        "pm_agent_writer least-privilege scope"
    )


def _file_signal(
    store: PmStore,
    hive_url: str,
    app_user_id: str,
    message: str,
    topic: str,
    signal: dict[str, Any] | None,
) -> str:
    """File a Hive work item, then record the signal occurrence.

    The occurrence is recorded (upsert_signal) even if Hive filing fails,
    so the feedback is durable; without a work id the signal stays unfiled
    and the caller gets a plain acknowledgement.
    """
    title = f"Product feedback: {topic[:80]}"
    description = f"End-user feedback from app user {app_user_id}:\n\n{message}"
    metadata = {"source": "pm-agent", "app_user_id": app_user_id}
    hive_work_id = ""
    try:
        result = file_feedback_work(hive_url, title, description, metadata)
        hive_work_id = str(result.get("work_id") or result.get("id") or "")
    except HiveClientError:
        log.exception("failed to file feedback work with Hive")
    signal_id = store.upsert_signal(topic, message)
    if hive_work_id:
        store.mark_signal_filed(signal_id, hive_work_id)
    if not hive_work_id:
        return _reply_recorded()
    if signal is None:
        return _reply_new(topic)
    return _reply_escalated(int(signal.get("occurrence_count") or 0) + 1)


def handle_chat(
    store: Any, hive_url: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Handle one POST /chat payload. `store` duck-types PmStore."""
    app_user_id = payload.get("app_user_id")
    message = payload.get("message")
    if not isinstance(app_user_id, str) or not app_user_id.strip():
        return 400, {"error": "app_user_id is required"}
    if not isinstance(message, str) or not message.strip():
        return 400, {"error": "message is required"}

    conversation_id = store.get_or_create_conversation(app_user_id)
    store.record_message(conversation_id, app_user_id, "user", message)

    topic = message.strip().lower()
    signal = store.find_similar_signal(topic)
    if signal is None:
        # Genuinely new signal: file it.
        reply = _file_signal(store, hive_url, app_user_id, message, topic, None)
    else:
        count = int(signal.get("occurrence_count") or 0)
        if count < ESCALATION_THRESHOLD:
            # Known signal below threshold: record the occurrence, never
            # re-file with Hive.
            store.upsert_signal(topic, message)
            reply = _reply_known(count + 1, str(signal.get("status") or "new"))
        else:
            # Threshold crossed: escalate with a fresh work item.
            reply = _file_signal(store, hive_url, app_user_id, message, topic, signal)

    store.record_message(conversation_id, app_user_id, "assistant", reply)
    return 200, {"reply": reply}


def make_handler(store: Any, hive_url: str) -> type[BaseHTTPRequestHandler]:
    class ChatHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?")[0] == "/healthz":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.split("?")[0] != "/chat":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"error": "invalid JSON body"})
                return
            status, body = handle_chat(store, hive_url, payload)
            self._send(status, body)

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info(fmt, *args)

    return ChatHandler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    store = PmStore(os.environ.get("PM_DATABASE_URL"))
    hive_url = os.environ.get("HIVE_URL", DEFAULT_HIVE_URL)
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(store, hive_url))
    log.info("pm-agent listening on port %d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
