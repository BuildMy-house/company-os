"""Detect CoreDNS forwarding to a stale/unreachable upstream DNS resolver.

Pure module — no Ledger/ObserverWriter dependency. Run directly via
``python -m company_ops.dns_healthcheck``; ``--dry-run`` prints the detection
result without opening a Steward task.

Automates detection of the 2026-09-28 incident class: CoreDNS's pod caches
the host's /etc/resolv.conf, so when the host's resolver moves (that day:
192.168.8.1 -> 192.168.1.1, plus a malformed fe80:: link-local with no zone
id) CoreDNS keeps forwarding external queries to the dead upstream. That
breaks ALL external DNS for every pod cluster-wide, but it first surfaces as
whichever service happens to need external DNS — e.g. hermes-gateway's
Discord adapter crash-looping on ``aiohttp.client_exceptions.ClientConnector
DNSError: Cannot connect to host discord.com:443 ... Temporary failure in
name resolution``. registry.npmjs.org, api.osv.dev, eu.infisical.com and
buildmyhouse.stewardacs.xyz are equally dead at the same time.

Detection signal (validated by hand during that incident): repeating
(>= ``min_repeats``, default 3 for the SAME upstream — a single blip is
noise) ``[ERROR] plugin/errors: ... i/o timeout`` or ``dial udp [..]:53:
connect: invalid argument`` lines in the coredns pod logs. Deliberately NOT
compared against the host's real resolver: a pod cannot introspect the
host's live resolvectl state, so that comparison is documentation context,
not a detection precondition.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys

import requests

log = logging.getLogger(__name__)

DEFAULT_STEWARD_MCP_URL = "https://buildmyhouse.stewardacs.xyz/mcp/sse"
# Creator identity for the task this module opens. Env-overridable because
# the ACS may want a pre-registered name for the bot instead of the default.
DEFAULT_AGENT_ID = "dns-healthcheck"
RAW_TAIL_LINES = 40
HTTP_TIMEOUT_SECS = 30
UNKNOWN_UPSTREAM = "<unparsed-upstream>"

# The two error shapes validated by hand on 2026-09-28.
_ERROR_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[ERROR\] plugin/errors:.*i/o timeout"),
    re.compile(r"dial udp \[.*\]:53: connect: invalid argument"),
)

# Upstream extraction: the address after "-> " (``...->192.168.8.1:53``),
# else the address inside ``dial udp <addr>:53``. Handles plain IPv4/host,
# bracketed IPv6, and bare (bracketless) IPv6.
_UPSTREAM_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"->\s*(\[[^\]]+\]|[0-9A-Fa-f]*:[0-9A-Fa-f:.%]+|[^\s>:]+):\d+"),
    re.compile(r"dial udp\s+(\[[^\]]+\]|[0-9A-Fa-f]*:[0-9A-Fa-f:.%]+|[^\s>:]+):\d+"),
)


def _require(value: str | None, env_var: str) -> str:
    if value is None:
        value = os.environ.get(env_var)
    if not value:
        raise ValueError(f"{env_var} is not set — required to open a Steward task")
    return value


def _extract_upstream(line: str) -> str:
    for rx in _UPSTREAM_RES:
        m = rx.search(line)
        if m:
            return m.group(1)
    # Error matched but no arrow/dial address to parse: still count it under
    # a fallback key so detection never silently drops evidence.
    return UNKNOWN_UPSTREAM


def check_coredns_logs(
    namespace: str = "kube-system",
    since: str = "15m",
    tail: int = 2000,
    min_repeats: int = 3,
) -> dict:
    """Grep coredns pod logs for repeating stale-upstream errors.

    Shells out to ``kubectl logs -n <ns> -l k8s-app=kube-dns --since=...``.

    Never raises on a nonzero kubectl exit (cluster/kubectl access briefly
    unavailable = "no signal this run"): that returns ``detected: False`` with
    an ``error`` note. A *missing* kubectl binary (FileNotFoundError from
    subprocess) does raise — that is a deployment misconfiguration (this
    module runs in a CronJob; see k8s/dns-healthcheck.yaml) and should fail
    the Job loudly instead of silently never detecting anything.

    Returns {"detected": bool, "matches": [{"upstream", "count",
    "sample_line"}], "raw_tail": str, "namespace": str, "since": str}.
    """
    cmd = [
        "kubectl", "logs",
        "-n", namespace,
        "-l", "k8s-app=kube-dns",
        f"--since={since}",
        f"--tail={tail}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    lines = (proc.stdout or "").splitlines()
    result: dict = {
        "detected": False,
        "matches": [],
        "namespace": namespace,
        "since": since,
        "raw_tail": "\n".join(lines[-RAW_TAIL_LINES:]),
    }
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        result["error"] = f"kubectl logs exited {proc.returncode}: {stderr[:500]}"
        log.info("dns_healthcheck: no signal this run (%s)", result["error"])
        return result

    grouped: dict[str, dict] = {}
    for line in lines:
        if not any(rx.search(line) for rx in _ERROR_RES):
            continue
        upstream = _extract_upstream(line)
        entry = grouped.setdefault(
            upstream, {"upstream": upstream, "count": 0, "sample_line": line}
        )
        entry["count"] += 1
    matches = sorted(grouped.values(), key=lambda m: m["count"], reverse=True)
    result["matches"] = matches
    # Trigger only when the SAME upstream repeats min_repeats+ times.
    result["detected"] = any(m["count"] >= min_repeats for m in matches)
    return result


def _task_text(evidence: dict) -> tuple[str, str]:
    """Build (title, description) for the Steward task from detection evidence."""
    matches = evidence.get("matches") or []
    since = evidence.get("since") or "15m"
    top = matches[0] if matches else {"upstream": UNKNOWN_UPSTREAM, "count": 0}
    title = (
        f"CoreDNS forwarding to stale upstream: {top['upstream']} "
        f"({top['count']} timeouts in {since})"
    )
    body = [
        f"Automated detection by `company_ops/dns_healthcheck.py` "
        f"(CronJob `k8s/dns-healthcheck.yaml`, every 15m) reading "
        f"`kubectl logs -n {evidence.get('namespace', 'kube-system')} "
        f"-l k8s-app=kube-dns --since={since}`.",
        "",
        f"## Matched upstreams (last {since})",
    ]
    for m in matches:
        body.append(f"- `{m['upstream']}` — {m['count']} error lines, sample:")
        body.append(f"      {m['sample_line'].strip()}")
    body += [
        "",
        "## What this means",
        "CoreDNS's pod caches the host's /etc/resolv.conf; when the host's "
        "resolver moves, CoreDNS keeps forwarding external queries to the "
        "stale/dead upstream, breaking ALL external DNS for every pod "
        "cluster-wide (registry.npmjs.org, api.osv.dev, eu.infisical.com, "
        "buildmyhouse.stewardacs.xyz, discord.com, ...). It usually first "
        "looks like a single service's bug — e.g. hermes-gateway's Discord "
        "adapter crash-looping on ClientConnectorDNSError / 'Temporary "
        "failure in name resolution' — but the failing service itself is "
        "healthy.",
        "",
        "## Remediation (known-safe, intentionally NOT run automatically)",
        "    kubectl rollout restart deployment/coredns -n kube-system",
        "Fully reversible: k3s recreates the pod from the Deployment spec, "
        "which re-reads the host's resolver. The healthcheck's ServiceAccount "
        "deliberately has no permission to do this (get/list on pods + "
        "pods/log only, no `apps` rules) — auto-remediation needs its own "
        "sign-off on approach, so the fix is left for a human or an "
        "already-privileged agent (e.g. Hermes via its k8s_deployment MCP, "
        "or a host-side kubectl session) to run.",
        "",
        "Optional follow-up: if hermes-gateway was already mid-crash-loop "
        "when DNS broke, its in-process aiohttp/discord.py client may still "
        "hold cached DNS failures — restart it too: "
        "`kubectl rollout restart deployment/hermes-gateway -n company-ops`.",
        "",
        "Full context / detector source: `company_ops/dns_healthcheck.py` "
        "(repo `company-os`); manifest `k8s/dns-healthcheck.yaml`. "
        "Manual diagnosis steps: Steward skill `dns-stale-upstream-diagnosis`.",
        "",
        "## Evidence (tail of the checked log window)",
        "```",
        evidence.get("raw_tail", ""),
        "```",
    ]
    return title, "\n".join(body)


def _post_rpc(url: str, headers: dict, payload: dict, session_id: str | None = None):
    """POST one JSON-RPC message to the Streamable-HTTP MCP endpoint.

    Returns (response_message_or_None, session_id). Handles both a plain
    JSON response and a ``text/event-stream`` response (reads data: lines
    until the message with the matching id arrives), and propagates the
    ``Mcp-Session-Id`` header the server sets on initialize. Notifications
    (no id) get a 202/empty body, hence the None return.
    """
    hdrs = dict(headers)
    if session_id:
        hdrs["Mcp-Session-Id"] = session_id
    resp = requests.post(url, json=payload, headers=hdrs, timeout=HTTP_TIMEOUT_SECS)
    resp.raise_for_status()
    new_session_id = resp.headers.get("Mcp-Session-Id") or session_id
    message: dict | None = None
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        for line in (resp.text or "").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                parsed = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            if parsed.get("id") == payload.get("id"):
                message = parsed
                break
    elif (resp.text or "").strip():
        try:
            message = json.loads(resp.text)
        except json.JSONDecodeError:
            message = None
    return message, new_session_id


def _unwrap_result(message: dict) -> object:
    """Pull the JSON object out of a tools/call result (content[].text)."""
    result = message.get("result", message)
    if isinstance(result, dict):
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except (TypeError, json.JSONDecodeError):
                    return item.get("text")
    return result


def create_steward_task(
    evidence: dict,
    steward_mcp_url: str | None = None,
    steward_token: str | None = None,
) -> dict:
    """Open a Steward coordination **task** carrying the detection evidence.

    Only called when ``evidence["detected"]`` is true.

    Three-message MCP sequence over plain HTTP (Streamable HTTP transport —
    no SSE stream held open for a single call), identical shapes to
    ``company_ops/mcp_client.py``'s stdio client: ``initialize`` ->
    ``notifications/initialized`` -> ``tools/call``.

    Calls the ``create_work`` tool, NOT ``create_task_from_error_trace``:
    that tool requires a pre-existing ErrorTrace row created by Steward's own
    internal log ingestion, which this in-cluster check has no way to insert
    into — so a task is created directly instead. The task is named a
    "task" everywhere, never an "error trace".

    URL resolution: explicit arg > env STEWARD_MCP_URL > env STEWARD_URL
    (the name Infisical actually syncs into company-ops-secrets for this, see
    k8s/infisical-sync.yaml's /infra listing) > the production default.
    STEWARD_TOKEN is required (ValueError naming it if missing).
    """
    token = _require(steward_token, "STEWARD_TOKEN")
    url = (
        steward_mcp_url
        or os.environ.get("STEWARD_MCP_URL")
        or os.environ.get("STEWARD_URL")
        or DEFAULT_STEWARD_MCP_URL
    )
    agent_id = os.environ.get("STEWARD_AGENT_ID") or DEFAULT_AGENT_ID
    title, description = _task_text(evidence)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    _, session_id = _post_rpc(url, headers, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "company-ops-dns-healthcheck", "version": "0.1.0"},
        },
    })
    _post_rpc(url, headers, {
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }, session_id)
    message, _ = _post_rpc(url, headers, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "create_work", "arguments": {
            "agent_id": agent_id,
            "title": title,
            "description": description,
        }},
    }, session_id)
    if message is None:
        raise RuntimeError(f"Steward MCP returned no parseable tools/call response from {url}")
    if "error" in message:
        raise RuntimeError(f"Steward MCP tools/call failed: {message['error']}")
    task = _unwrap_result(message)
    log.info("dns_healthcheck: opened Steward task for %s", title)
    return {"url": url, "agent_id": agent_id, "title": title, "task": task}


def run_healthcheck() -> dict:
    """Detect stale-upstream errors; open a Steward task only when detected.

    The not-detected path is the common case and must never raise, so the
    CronJob (k8s/dns-healthcheck.yaml) exits 0 cleanly and does not spam
    failure notifications on a healthy cluster.
    """
    evidence = check_coredns_logs()
    if not evidence.get("detected"):
        out: dict = {"detected": False, "matches": []}
        if evidence.get("error"):
            out["error"] = evidence["error"]
        log.info("dns_healthcheck: clean run, no repeating stale-upstream errors")
        return out
    task_created = create_steward_task(evidence)
    return {
        "detected": True,
        "matches": evidence["matches"],
        "task_created": task_created,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--dry-run" in sys.argv:
        # Print detection only; never open a Steward task (safe to test
        # against a live cluster repeatedly).
        print(json.dumps(check_coredns_logs(), default=str))
    else:
        print(json.dumps(run_healthcheck(), default=str))
