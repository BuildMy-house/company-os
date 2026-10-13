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
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import guard
import hive_presence
import state_wipe
import tools as pm_tools
from company_ops.pm_store import PmStore

ESCALATION_THRESHOLD = pm_tools.ESCALATION_THRESHOLD
DEFAULT_PORT = 4200
DEFAULT_HIVE_URL = "http://hive-coordinator:4100"
DEFAULT_HERMES_URL = "http://127.0.0.1:8642"
CHATS_PER_USER_PER_HOUR = 20
# Must stay below the app proxy's 60s (app/server/src/chat.ts PM_AGENT_TIMEOUT_MS)
# so the front door answers (reply or fallback) before the app gives up.
LLM_TIMEOUT_SECONDS = 50
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

    def _history(self, conversation_id: str, app_user_id: str) -> list[tuple[str, str]]:
        """This user's own recent turns (oldest first), refusals/errors skipped.

        Read through the user-scoped store call, so another user's messages
        can never be returned. Failure degrades to no history, never an error.
        """
        try:
            rows = self.store.recent_messages(
                conversation_id, app_user_id, guard.HISTORY_MAX_MESSAGES * 4
            )
        except Exception:  # noqa: BLE001 - history is best-effort context
            log.exception("failed to load conversation history")
            return []
        turns: list[tuple[str, str]] = []
        for role, content in rows:
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            if role == "assistant" and (
                content == "[runtime error]" or content == guard.REFUSAL
            ):
                continue
            turns.append((role, content))
        return turns[-guard.HISTORY_MAX_MESSAGES :]

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

        state_wipe.touch_activity()
        clean = guard.normalize_user_text(message)
        if not clean:
            return 400, {"error": "message is required"}

        if not self._lock.acquire(timeout=LLM_TIMEOUT_SECONDS):
            return 503, {"error": "busy", "reply": guard.UNAVAILABLE_REPLY}
        try:
            conversation_id = self.store.get_or_create_conversation(app_user_id)
            history = self._history(conversation_id, app_user_id)
            self.store.record_message(conversation_id, app_user_id, "user", clean)
            pm_tools.write_context(
                self.context_path, app_user_id, conversation_id, clean
            )
            try:
                raw = self.llm(guard.wrap_untrusted(clean, history))
            except (OSError, ValueError, KeyError, IndexError, urllib.error.URLError):
                log.exception("hermes runtime call failed")
                if looks_like_model_unavailable(sys.exc_info()[1] or Exception()):
                    request_re_resolve()
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


RE_RESOLVE_INTERVAL_SECONDS = 6 * 3600
RE_RESOLVE_COOLDOWN_SECONDS = 300
EXIT_MODEL_CHANGED = 75
_resolve_event = threading.Event()


def request_re_resolve() -> None:
    """Wake the model watchdog (called when the LLM reports a dead model)."""
    _resolve_event.set()


def model_watchdog() -> None:
    """Every 6h, or on a model-unavailable error, re-run resolve_model.py --check.

    If the best working model changed (or the current one died), exit the
    process: entrypoint.sh's `wait -n` then stops Hermes and the container
    exits, k8s restarts the pod, and the entrypoint re-resolves and re-renders
    the config. Restart-on-change is simpler and more robust than hot-reloading
    Hermes. A resolver failure (nothing works) is logged and retried later.
    """
    import resolve_model  # lazy: only the watchdog needs it
    last = 0.0
    while True:
        triggered = _resolve_event.wait(timeout=RE_RESOLVE_INTERVAL_SECONDS)
        if triggered and time.monotonic() - last < RE_RESOLVE_COOLDOWN_SECONDS:
            _resolve_event.clear()
            continue
        _resolve_event.clear()
        last = time.monotonic()
        rc = resolve_model.main(["--check"])
        if rc == EXIT_MODEL_CHANGED:
            log.info(json.dumps({"event": "model_changed_restarting"}))
            logging.shutdown()
            os._exit(EXIT_MODEL_CHANGED)
        if rc != 0:
            log.error(json.dumps({"event": "model_recheck_failed", "rc": rc}))


def looks_like_model_unavailable(exc: BaseException) -> bool:
    text = str(exc).lower()
    code = getattr(exc, "code", None)
    return code in (404, 410, 500, 502) or any(
        k in text for k in ("no longer free", "not found", "404", "model_not_found")
    )


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
    threading.Thread(
        target=state_wipe.run_when_idle,
        args=(os.environ.get("HERMES_HOME", "/home/pmagent/.hermes"), service.context_path, stop),
        name="state-wipe",
        daemon=True,
    ).start()
    if os.environ.get("PM_AGENT_LLM_API_KEY"):
        threading.Thread(target=model_watchdog, name="model-watchdog", daemon=True).start()
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
