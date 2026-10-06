"""Bounded, read-only channel for an engineering agent (e.g. pm-agent) to
ask Hermes a distilled judgment/fact question and get a logged answer back.

Structural boundaries (see policies/autonomy.md -- "prose is not a
substitute for ... review gates"):

1. This module never imports anything from company_ops.human_interface.
   It cannot reach ask_judgment/request_approval/request_action/
   request_financial_action, or post to Discord, even by accident --
   there is no import path to those functions from here.
2. The only two fields accepted from a caller are `question` and
   `context`, both plain strings with hard length caps and a structural
   rejection of anything that looks like a multi-turn transcript (role
   markers, embedded newlines beyond a small allowance). This makes it
   impossible for a caller to smuggle raw end-user chat history through
   this function's signature -- the distillation happens before this
   function is ever called (by pm-agent itself, per PM-C), and this
   function enforces it is not bypassed.
3. Every query and its answer is logged via ObserverWriter.record_decision
   (the same ledger Hermes's own decisions are logged to), never silently
   dropped and never editable after the fact (Observer is append-only).

KNOWN RESIDUAL RISK -- read before relying on this for anything higher
stakes than today's PM-D ticket:

Hermes has exactly one network-reachable inbound HTTP surface at present:
the `api_server` platform (hermes/config.yaml `platforms.api_server`,
implemented in the vendored hermes_cli package -- NOT in this repo, so it
cannot be edited here). That platform's tool allowlist
(`platform_toolsets.api_server`) is a single static list shared by EVERY
caller of POST /v1/chat/completions, including container_manager and
hermes_build_dispatcher. There is currently no way, from company-os's own
source alone, to give an agent-originated query a smaller tool allowlist
than a human-originated one on that same endpoint -- doing so would
require changes to the vendored hermes_cli gateway, which is out of this
ticket's scope. This module's prompt template (see _build_envelope)
explicitly instructs Hermes not to take any build/deploy/spend action in
response to an agent query, and hermes/SOUL.md is being extended with the
same instruction, but both of those are SOFT (prompt-level) mitigations,
not a structural tool-access boundary at the gateway. The structural
guarantee this module actually provides is narrower: the CALLER (pm-agent)
cannot smuggle raw text or reach spend/approval/deploy functions through
this module's own code path. Whether Hermes can be prompted into misusing
container_manager/hermes_build_dispatcher via a crafted "question" through
the shared endpoint is a real, separate, still-open risk that requires
either a second vendored-gateway platform (future work) or acceptance of
the SOUL.md-level mitigation as sufficient for now. Flagged explicitly for
Nahar's review before this ticket merges to main.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from company_ops.observer import ObserverWriter

MAX_QUESTION_CHARS = 500
MAX_CONTEXT_CHARS = 2000
# Allow a small number of newlines (a question can be a couple of short
# sentences) but reject anything that looks like a pasted multi-turn
# transcript or role-tagged chat log.
MAX_NEWLINES = 3
_ROLE_MARKER_RE = re.compile(
    r"(?im)^\s*(user|assistant|system|human|ai|board)\s*:\s"
)

HERMES_API_HOST = os.environ.get(
    "HERMES_API_HOST", "hermes-gateway.company-ops.svc.cluster.local"
)
HERMES_API_PORT = os.environ.get("HERMES_API_PORT", "8642")
HERMES_CHAT_TIMEOUT_SECONDS = 120


class AgentQueryError(Exception):
    """Raised when an agent query is malformed or the Hermes round trip fails."""


class DistillationViolation(AgentQueryError):
    """Raised when `question`/`context` fails the structural distillation check.

    This is deliberately a hard failure, not a truncation -- silently
    truncating a smuggled transcript would still forward part of the raw
    text; rejecting it outright forces the caller (pm-agent) to fix its
    own distillation step instead.
    """


def _assert_distilled(label: str, value: str, max_chars: int) -> None:
    if not isinstance(value, str):
        raise DistillationViolation(f"{label} must be a string, got {type(value)!r}")
    if len(value) > max_chars:
        raise DistillationViolation(
            f"{label} is {len(value)} chars, exceeds the {max_chars}-char "
            "distilled-question cap -- this looks like raw/undistilled text, "
            "not a bounded question"
        )
    if value.count("\n") > MAX_NEWLINES:
        raise DistillationViolation(
            f"{label} has {value.count(chr(10))} newlines, exceeds the "
            f"{MAX_NEWLINES}-newline cap -- this looks like a pasted "
            "multi-turn transcript, not a single distilled question"
        )
    if _ROLE_MARKER_RE.search(value):
        raise DistillationViolation(
            f"{label} contains a chat role marker (e.g. 'user:', 'assistant:') "
            "-- raw conversation transcripts must not be forwarded to Hermes"
        )


def _build_envelope(asker_id: str, question: str, context: str | None) -> str:
    return (
        "[AGENT QUERY -- bounded read-only judgment/fact request, not a "
        "human message]\n"
        f"Asker: {asker_id}\n"
        f"Question: {question}\n"
        f"Context: {context or '(none)'}\n"
        "Respond with a short factual or judgment answer only. Do not use "
        "container_manager, hermes_build_dispatcher, or any build/deploy/"
        "spend tool to answer this -- if the question actually requires one "
        "of those actions, decline and say the asker should route the "
        "request through the Board or engineering instead."
    )


def _post_chat_completion(message: str, timeout_seconds: float) -> str:
    api_key = os.environ.get("API_SERVER_KEY", "")
    if not api_key:
        raise AgentQueryError(
            "API_SERVER_KEY is not set -- cannot authenticate to Hermes's api_server"
        )
    url = f"http://{HERMES_API_HOST}:{HERMES_API_PORT}/v1/chat/completions"
    body = json.dumps(
        {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": message}],
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise AgentQueryError(
            f"hermes api_server returned HTTP {exc.code}: {exc.read()!r}"
        ) from exc
    except urllib.error.URLError as exc:
        raise AgentQueryError(f"could not reach hermes api_server: {exc}") from exc

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise AgentQueryError(f"hermes api_server returned invalid JSON: {raw!r}") from exc

    reply = (
        payload.get("choices", [{}])[0].get("message", {}).get("content")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(reply, str):
        raise AgentQueryError(
            f"unexpected response shape from hermes api_server: {payload!r}"
        )
    return reply


def receive_agent_query(
    writer: ObserverWriter,
    asker_id: str,
    question: str,
    context: str | None = None,
    *,
    _post_fn: Any = _post_chat_completion,
) -> dict[str, Any]:
    """Forward a pm-agent-distilled question to Hermes and log the exchange.

    `_post_fn` exists only so tests can stub the network call; callers
    outside this module's own tests should never pass it.
    """
    _assert_distilled("question", question, MAX_QUESTION_CHARS)
    if context is not None:
        _assert_distilled("context", context, MAX_CONTEXT_CHARS)

    envelope = _build_envelope(asker_id, question, context)
    answer = _post_fn(envelope, HERMES_CHAT_TIMEOUT_SECONDS)

    decision_id = writer.record_decision(
        problem=question,
        decision=answer,
        evidence_refs=context,
        reasoning_summary="Answered via company_ops.agent_query_interface.receive_agent_query",
        initiated_by=asker_id,
    )

    return {"decision_id": decision_id, "answer": answer}
