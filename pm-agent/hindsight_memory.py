"""pm-agent's long-term memory: a narrow client for its OWN Hindsight bank.

Hindsight (k8s/base/hindsight) has ONE shared API key for every bank, so bank
separation is by NAME ONLY and is enforced here: the bank is fixed by
configuration (HINDSIGHT_BANK_ID, default ``pm-lead``) and is never a tool
argument, so injected text cannot read or write another agent's bank.

What goes in is deliberately small and safe: short, neutral, user-free lessons
(what worked, what was corrected, the outcome). Content is normalised with
guard.normalize_user_text, URLs stripped, length capped, and refused if it looks
like a credential. Everything recalled is returned to the model as UNTRUSTED
past notes (pm-agent/content/*/SOUL.md says never to follow instructions in it).
Every call fails soft: memory being down must never break a chat reply.

Endpoints (checked against hindsight-client 0.10.3, the version of the server
image): POST /v1/default/banks/{bank}/memories and .../memories/recall.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

import guard

DEFAULT_URL = "http://hindsight:8888"
DEFAULT_BANK = "pm-lead"
HTTP_TIMEOUT_SECONDS = 20  # recall reranks on CPU (~5s); stays under the 30s tool timeout
LESSON_MAX_CHARS = 400
QUERY_MAX_CHARS = 200
RECALL_MAX_RESULTS = 5
RECALL_RESULT_MAX_CHARS = 300
RECALL_MAX_TOKENS = 800
OUTCOMES = ("worked", "corrected", "failed", "unknown")

_URL_RE = guard._URL_RE  # noqa: SLF001 - same URL definition as the rest of pm-agent
_SECRET_RE = re.compile(
    r"(ghp_|github_pat_|xox[bp]-|AKIA[0-9A-Z]{12}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|postgres(ql)?://[^\s:]+:[^\s@]+@|Bearer\s+[A-Za-z0-9._-]{20,}"
    r"|(password|passwd|secret|token|cookie|api[_-]?key)\s*[:=]\s*\S{6,})",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

log = logging.getLogger("pm-agent.hindsight")


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    # Hermes leaves an unset ${VAR} reference as a literal string.
    return default if not value or value.startswith("${") else value


def clean_memory_text(text: Any, limit: int) -> str:
    """Normalise untrusted text for storage; raise ValueError if unusable."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required")
    out = _URL_RE.sub("", guard.normalize_user_text(text))
    out = _EMAIL_RE.sub("", out).replace("\n", " ")
    out = re.sub(r"\s{2,}", " ", out).strip()
    if _SECRET_RE.search(out):
        raise ValueError("memory refused: text looks like a credential")
    if not out:
        raise ValueError("text is required")
    return guard.cap(out, limit)


class Memory:
    """Retain/recall against one fixed bank. Never raises to the caller."""

    def __init__(self, url: str | None = None, api_key: str | None = None, bank: str | None = None):
        self.url = (url or _env("HINDSIGHT_URL", DEFAULT_URL)).rstrip("/")
        self.api_key = api_key if api_key is not None else _env("HINDSIGHT_API_KEY")
        self.bank = bank or _env("HINDSIGHT_BANK_ID", DEFAULT_BANK)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.url}/v1/default/banks/{self.bank}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "pm-agent-hindsight/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "{}")

    def retain(self, lesson: str, outcome: str, topic: str = "", source: str = "model") -> dict[str, Any]:
        """Store one short atomic lesson. Returns {"retained": bool, ...}."""
        if not self.enabled:
            return {"retained": False, "reason": "memory not configured"}
        if outcome not in OUTCOMES:
            outcome = "unknown"
        text = clean_memory_text(lesson, LESSON_MAX_CHARS)
        label = f" topic={guard.clean_topic(topic)}" if topic else ""
        content = f"[pm-lead lesson] outcome={outcome}{label}: {text}"
        item = {
            "content": content,
            "context": (
                "pm-agent lesson written by the PM lead agent; derived from untrusted "
                "end-user chat; holds no user identifiers"
            ),
            "tags": ["pm-lead", "pm-lesson", f"source:{source}", f"outcome:{outcome}"],
        }
        try:
            self._post("/memories", {"items": [item], "async": True})
        except (urllib.error.URLError, OSError, ValueError):
            log.warning("hindsight retain failed", exc_info=True)
            return {"retained": False, "reason": "memory temporarily unavailable"}
        return {"retained": True}

    def recall(self, query: str) -> dict[str, Any]:
        """Search the bank. Returns {"lessons": [...]}; empty on any failure."""
        if not self.enabled:
            return {"lessons": [], "unavailable": True}
        q = clean_memory_text(query, QUERY_MAX_CHARS)
        try:
            data = self._post(
                "/memories/recall",
                {"query": q, "budget": "low", "max_tokens": RECALL_MAX_TOKENS, "types": ["world", "experience"]},
            )
        except (urllib.error.URLError, OSError, ValueError):
            log.warning("hindsight recall failed", exc_info=True)
            return {"lessons": [], "unavailable": True}
        lessons = []
        for r in (data.get("results") or [])[:RECALL_MAX_RESULTS]:
            text = r.get("text") if isinstance(r, dict) else None
            if isinstance(text, str) and text.strip():
                lessons.append(guard.cap(guard.normalize_user_text(text), RECALL_RESULT_MAX_CHARS))
        return {"lessons": lessons}
