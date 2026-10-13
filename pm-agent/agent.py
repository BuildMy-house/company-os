"""buildmy.house pm-agent front door: POST /chat -> Hermes-runtime agent.

The app relay reaches this through pm-agent-gateway (nginx bearer gate). This
process owns everything that must NOT be left to the model:

  - request validation and hard size caps,
  - per-user chat rate limiting,
  - wrapping user text as delimited untrusted data,
  - plain-text-only, secret-scrubbed replies and the fixed out-of-scope
    refusal,
  - logging every exchange to company.pm_conversations,
  - Hive membership (hive_presence.py),
  - /healthz for probes.

The model itself runs in the Hermes runtime (hermes gateway, api_server on
127.0.0.1) with the per-project persona from PM_AGENT_CONTENT_DIR (default
pm-agent/content/buildmy-house/SOUL.md) and exactly one MCP server,
pm_tools_mcp.py (tools.py): dedupe lookup and gated Hive filing. See
docs/PM-AGENT-HERMES.md.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import guard
import hive_presence
import tools as pm_tools
from company_ops.pm_store import PmStore

ESCALATION_THRESHOLD = pm_tools.ESCALATION_THRESHOLD
DEFAULT_PORT = 4200
DEFAULT_HIVE_URL = "http://hive-coordinator:4100"
DEFAULT_HERMES_URL = "http://127.0.0.1:8642"
CHATS_PER_USER_PER_HOUR = 20
LLM_TIMEOUT_SECONDS = 90
OUT_OF_SCOPE_SENTINEL = "OUT_OF_SCOPE"
SECRET_ENV_NAMES = ("PM_DATABASE_URL", "PM_AGENT_LLM_API_KEY", "API_SERVER_KEY")

log = logging.getLogger("pm-agent")

LlmFn = Callable[[str], str]


def hermes_llm(base_url: str, api_key: str) -> LlmFn:
    """Return a function sending one stateless user turn to the Hermes runtime."""

    def ask(prompt: str) -> str:
        body = {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:  # noqa: S310
            payload = json.loads(resp.read())
        return str(payload["choices"][0]["message"]["content"] or "")

    return ask


def finalize_reply(raw: str, secrets_to_hide: tuple[str, ...] = ()) -> str:
    """Turn raw model output into the only thing the user ever sees."""
    if OUT_OF_SCOPE_SENTINEL in raw:
        return guard.REFUSAL
    for secret in secrets_to_hide:
        if len(secret) >= 8 and secret in raw:
            log.warning(json.dumps({"event": "reply_blocked", "reason": "secret_in_reply"}))
            return guard.REFUSAL
    reply = guard.plain_text(raw)
    return reply or guard.REFUSAL


class ChatService:
    """Everything behind POST /chat except HTTP framing."""

    def __init__(
        self,
        store: Any,
        llm: LlmFn,
        context_path: str = pm_tools.DEFAULT_CONTEXT_PATH,
        chat_limiter: guard.RateLimiter | None = None,
        secrets_to_hide: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.llm = llm
        self.context_path = context_path
        self.chat_limiter = chat_limiter or guard.RateLimiter(
            CHATS_PER_USER_PER_HOUR, 3600
        )
        self.secrets_to_hide = secrets_to_hide
        # The tools read the active user from a context file, so exactly one
        # exchange may be in flight at a time.
        self._lock = threading.Lock()

    def handle(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        app_user_id = payload.get("app_user_id")
        message = payload.get("message")
        if not isinstance(app_user_id, str) or not app_user_id.strip():
            return 400, {"error": "app_user_id is required"}
        if len(app_user_id) > guard.MAX_USER_ID_CHARS:
            return 400, {"error": "app_user_id too long"}
        if not isinstance(message, str) or not message.strip():
            return 400, {"error": "message is required"}
        if len(message) > guard.MAX_MESSAGE_CHARS:
            return 413, {
                "error": f"message exceeds {guard.MAX_MESSAGE_CHARS} characters",
                "reply": "That message is too long. Please send a shorter summary.",
            }
        if not self.chat_limiter.allow(app_user_id):
            return 429, {"error": "rate limited", "reply": guard.RATE_LIMITED_REPLY}

        clean = guard.normalize_user_text(message)
        if not clean:
            return 400, {"error": "message is required"}

        if not self._lock.acquire(timeout=LLM_TIMEOUT_SECONDS):
            return 503, {"error": "busy", "reply": guard.UNAVAILABLE_REPLY}
        try:
            conversation_id = self.store.get_or_create_conversation(app_user_id)
            self.store.record_message(conversation_id, app_user_id, "user", clean)
            pm_tools.write_context(
                self.context_path, app_user_id, conversation_id, clean
            )
            try:
                raw = self.llm(guard.wrap_untrusted(clean))
            except (OSError, ValueError, KeyError, IndexError, urllib.error.URLError):
                log.exception("hermes runtime call failed")
                self.store.record_message(
                    conversation_id, app_user_id, "assistant", "[runtime error]"
                )
                return 503, {"error": "agent unavailable", "reply": guard.UNAVAILABLE_REPLY}
            finally:
                pm_tools.clear_context(self.context_path)
            reply = finalize_reply(raw, self.secrets_to_hide)
            self.store.record_message(conversation_id, app_user_id, "assistant", reply)
            log.info(
                json.dumps(
                    {
                        "event": "chat",
                        "app_user_id": app_user_id,
                        "message_chars": len(clean),
                        "reply_chars": len(reply),
                        "refused": reply == guard.REFUSAL,
                    }
                )
            )
            return 200, {"reply": reply}
        finally:
            self._lock.release()


def make_handler(service: ChatService) -> type[BaseHTTPRequestHandler]:
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
            except ValueError:
                self._send(400, {"error": "invalid Content-Length"})
                return
            if length > guard.MAX_BODY_BYTES:
                self._send(413, {"error": "request body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"error": "invalid JSON body"})
                return
            status, body = service.handle(payload)
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
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = PmStore(os.environ.get("PM_DATABASE_URL"))
    hive_url = os.environ.get("HIVE_URL", DEFAULT_HIVE_URL)
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    llm = hermes_llm(
        os.environ.get("HERMES_URL", DEFAULT_HERMES_URL),
        os.environ.get("API_SERVER_KEY", ""),
    )
    secrets_to_hide = tuple(
        os.environ[name] for name in SECRET_ENV_NAMES if os.environ.get(name)
    )
    service = ChatService(
        store,
        llm,
        os.environ.get("PM_CONTEXT_PATH", pm_tools.DEFAULT_CONTEXT_PATH),
        secrets_to_hide=secrets_to_hide,
    )
    stop = hive_presence.start(hive_url)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(service))
    log.info(json.dumps({"event": "listening", "port": port}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
