"""Scheduled "dream" pass over one Hindsight bank: injection / planted-lesson scan.

Hindsight has ONE shared API key for every bank (bank separation is by name), so
each agent's memory is only as trustworthy as what was retained into it. This
pass lists the bank's valid memories, flags the ones that look like prompt
injection or planted lessons, and (in apply mode) quarantines them by setting
their state to ``invalidated`` (PATCH .../memories/{id}). Invalidated memories
are excluded from recall, consolidation and graph maintenance but kept for
audit and are reversible (state=valid), which is why this is quarantine, not a
hard delete (the API has no per-memory delete).

LOGGING CONTRACT: only counts, rule names and memory ids are ever printed.
Memory text is never written to stdout, stderr or logs, and the API key is
never printed.

Modes: ``audit`` (default for this CLI; report only) and ``apply`` (quarantine).
The Hermes dream-audit passes the mode from ``$HERMES_HOME/dream-audit.mode``;
the pm-lead and marketing-lead CronJobs pass ``--apply``.

    python -m company_ops.hindsight_dream --bank pm-lead --apply --behaviour-check
Stdlib only (runs in the Hermes pre-run script and in the company-os image).
Endpoints checked against hindsight-client 0.10.3.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Iterable

PAGE = 100
HTTP_TIMEOUT = 60
QUARANTINE_REASON = "quarantined by hindsight-dream injection scan"

_I = re.IGNORECASE
# (rule name, pattern). Rules are intentionally broad: a false positive is only
# a reversible invalidation, a false negative is a planted instruction.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        re.compile(
            r"(ignore|disregard|forget|override|bypass)\s+(all\s+|any\s+|the\s+|your\s+)*"
            r"(previous|prior|above|earlier|system|safety|existing)?\s*"
            r"(instructions?|rules?|prompts?|guidelines?|polic(?:y|ies)|guardrails?)"
            r"|new\s+instructions?\s*:|system\s+prompt|developer\s+message"
            r"|you\s+are\s+now\b|act\s+as\s+(an?\s+)?(admin|root|system|developer)|jailbreak",
            _I,
        ),
    ),
    (
        "instruction_to_agent",
        re.compile(
            r"\b(assistant|agent|ai|bot|model|hermes|pm[- ]?lead)\s*[,:]\s*(you\s+)?"
            r"(must|should|shall|will|need to|have to|always|never|do not|don't)\b"
            r"|\b(when|if)\s+(you|the agent|an agent)\s+(see|read|recall|get|receive)s?\b[^.]{0,80}\b(then\s+)?"
            r"(do|run|call|send|reply|respond|answer|forward|post)\b",
            _I,
        ),
    ),
    (
        "tool_request",
        re.compile(
            r"\b(call|invoke|run|execute|use|trigger)\s+(the\s+)?[\w.-]*\s*(tool|command|script|function|mcp|shell|terminal)\b"
            r"|\b(curl|wget)\s+\S|\bsh\s+-c\b|\bbash\s+-c\b|\bsudo\s|\brm\s+-rf\b|\bos\.system\b|\beval\(",
            _I,
        ),
    ),
    (
        "credential_request",
        re.compile(
            r"\b(send|share|reveal|print|show|output|paste|leak|exfiltrate|forward|email|post|include)\b"
            r"[^.\n]{0,60}\b(password|passwd|token|api[_ -]?key|secret|credential|cookie|private key|env(?:ironment)? var)",
            _I,
        ),
    ),
    ("url", re.compile(r"https?://|\bwww\.[a-z0-9-]+\.[a-z]{2,}|\bftp://", _I)),
    (
        "credential_like",
        re.compile(
            r"(ghp_[A-Za-z0-9]{20,}|github_pat_|xox[bp]-|AKIA[0-9A-Z]{12}|\bsk-[A-Za-z0-9_-]{16,}"
            r"|-----BEGIN [A-Z ]*PRIVATE KEY|postgres(?:ql)?://[^\s:]+:[^\s@]+@|Bearer\s+[A-Za-z0-9._-]{20,}"
            r"|(?:password|passwd|secret|token|cookie|api[_-]?key)\s*[:=]\s*\S{6,}"
            r"|\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/_-]{48,}={0,2}(?=\s|$))",
            _I,
        ),
    ),
)

# Only with --behaviour-check (pm-lead): lessons derived from anonymous end-user
# chat must describe outcomes, not change future behaviour.
_BEHAVIOUR = re.compile(
    r"\b(from now on|going forward|in (the )?future|whenever|every time|always|never|"
    r"you (must|should|need to)|do not|don't ever|make sure to|remember to)\b",
    _I,
)
# Tags marking a template-generated memory (pm-agent's auto-retained outcomes):
# trusted for the behaviour check only; the content rules above still apply.
TRUSTED_TAGS = frozenset({"source:auto"})


def classify(text: str, tags: Iterable[str] = (), behaviour_check: bool = False) -> list[str]:
    """Return the sorted rule names that flag this memory (empty = clean)."""
    hits = {name for name, rx in RULES if rx.search(text or "")}
    if behaviour_check and not (set(tags or ()) & TRUSTED_TAGS) and _BEHAVIOUR.search(text or ""):
        hits.add("anonymous_behaviour")
    return sorted(hits)


class Client:
    def __init__(self, url: str, api_key: str, bank: str):
        self.url, self.key, self.bank = url.rstrip("/"), api_key, bank

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.url}/v1/default/banks/{self.bank}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
                "User-Agent": "hindsight-dream/1.0",
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "{}")

    def list_valid(self) -> Iterable[dict[str, Any]]:
        offset = 0
        while True:
            page = self._call("GET", f"/memories/list?limit={PAGE}&offset={offset}&state=valid")
            items = page.get("items") or []
            yield from items
            offset += len(items)
            if len(items) < PAGE:
                return

    def quarantine(self, memory_id: str) -> None:
        self._call("PATCH", f"/memories/{memory_id}", {"state": "invalidated", "reason": QUARANTINE_REASON})


def run(client: Client, apply: bool, behaviour_check: bool = False) -> dict[str, Any]:
    """Scan the bank; quarantine flagged items when apply. Returns counts + ids only."""
    # Materialise the listing first: invalidating while paging would shift offsets.
    items = list(client.list_valid())
    flagged: list[tuple[str, list[str]]] = []
    by_rule: dict[str, int] = {}
    for m in items:
        text, mid = m.get("text") or "", m.get("id")
        if not mid:
            continue
        rules = classify(text, m.get("tags") or (), behaviour_check)
        if rules:
            flagged.append((str(mid), rules))
            for r in rules:
                by_rule[r] = by_rule.get(r, 0) + 1
    quarantined, failed = [], 0
    if apply:
        for mid, _ in flagged:
            try:
                client.quarantine(mid)
                quarantined.append(mid)
            except (urllib.error.URLError, OSError, ValueError):
                failed += 1
    return {
        "bank": client.bank,
        "mode": "apply" if apply else "audit",
        "scanned": len(items),
        "flagged": len(flagged),
        "flagged_ids": [mid for mid, _ in flagged],
        "by_rule": dict(sorted(by_rule.items())),
        "quarantined": len(quarantined),
        "failed": failed,
    }


def _env_url() -> str:
    return os.environ.get("HINDSIGHT_URL") or os.environ.get("HINDSIGHT_API_URL") or "http://hindsight:8888"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bank", required=True)
    p.add_argument("--apply", action="store_true", help="quarantine flagged memories (default: audit only)")
    p.add_argument("--behaviour-check", action="store_true", help="also flag behaviour-changing lessons (pm-lead)")
    args = p.parse_args(argv)
    key = os.environ.get("HINDSIGHT_API_KEY", "").strip()
    if not key or key.startswith("${"):
        print(json.dumps({"bank": args.bank, "skipped": "HINDSIGHT_API_KEY not set"}))
        return 0  # nothing to scan; do not fail the CronJob/pre-run
    try:
        result = run(Client(_env_url(), key, args.bank), args.apply, args.behaviour_check)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(json.dumps({"bank": args.bank, "error": type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
