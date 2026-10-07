"""model_retirement_watch — Hermes plugin.

Detects permanent retirement of the configured PRIMARY model via the
``api_request_error`` hook, picks a vetted replacement from the provider's
``/models`` listing, and files a Steward task so a human/agent can update
``hermes/config.yaml`` and redeploy through the normal pipeline.

Design boundary: this plugin NEVER edits config or touches deployments —
all container changes go through builder_build_and_push → container_test →
container_upgrade. It only raises the alarm with a concrete suggestion.
"""

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import yaml

log = logging.getLogger(__name__)

_PLUGIN_NAME = "model_retirement_watch"

# Only these reasons indicate the model is gone for good (not found /
# not entitled to us / blocked by provider policy). Transient reasons
# (rate_limit, overloaded, timeout, server_error) and auth problems are
# never retirements.
_PERMANENT_REASONS = frozenset(
    {"model_not_found", "model_entitlement", "provider_policy_blocked"}
)

# Retirement is permanent, so this is a re-alert, not a new event: one task
# per hour bounds spam if the broken primary keeps being retried, while still
# re-reminding if nobody acts. The marker is pod-local
# (<hermes home>/plugin-data/), so a pod restart may cause at most one
# duplicate task — acceptable for an hourly-cadence alarm.
_COOLDOWN_SECONDS = 3600

# Runtime config path inside the hermes-agent image (Dockerfile copies
# hermes/config.yaml here).
_CONFIG_PATH = "/opt/data/config.yaml"

# Cloudflare fronts the prod Steward URL and 403s python-urllib's default
# User-Agent; every request must carry a real one.
_USER_AGENT = "model_retirement_watch/0.1.0"

_AXIOM_INGEST_URL = "https://eu-central-1.aws.edge.axiom.co/v1/ingest/bmh-company"

_MCP_PROTOCOL_VERSION = "2024-11-05"

_AGENTIC_WORDS = (
    "agentic", "agent", "long-horizon", "horizon", "coding", "code",
    "repository", "software", "reasoning", "tool",
)
_NARROW_WORDS = (
    "health", "medical", "finance", "financial", "legal", "translation",
    "chat", "roleplay",
)

# op → count of swallowed failures (mirrors axiom_usage's bookkeeping)
_FAILURES = {}


def _record_failure(op, exc=None):
    _FAILURES[op] = _FAILURES.get(op, 0) + 1
    log.warning("model_retirement_watch: %s failed (%s; total=%s)", op, exc, _FAILURES[op])


# ---------------------------------------------------------------- config ---

def _load_config(path=None):
    with open(path or _CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _primary(config):
    model = config.get("model") or {}
    return model.get("provider") or "", model.get("default") or ""


def _custom_provider(config, provider):
    name = provider[len("custom:"):] if provider.startswith("custom:") else provider
    for entry in config.get("custom_providers") or []:
        if entry.get("name") == name:
            return entry
    return None


# ---------------------------------------------------------------- marker ---

def _marker_path():
    """Sanctioned plugin state dir; falls back for ad-hoc/test use."""
    try:
        from plugins.plugin_storage import plugin_data_dir  # vendored in image

        return Path(plugin_data_dir(_PLUGIN_NAME)) / "retirement-marker.json"
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        path = Path(home) / "plugin-data" / _PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path / "retirement-marker.json"


def _read_marker():
    try:
        with open(_marker_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_marker(provider, model, ts, replacement):
    payload = {
        "provider": provider,
        "model": model,
        "announced_at": ts,
        "replacement": replacement,
    }
    tmp = Path(str(_marker_path()) + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, _marker_path())


def _cooldown_active(marker, provider, model, now):
    return (
        marker.get("provider") == provider
        and marker.get("model") == model
        and (now - float(marker.get("announced_at", 0))) < _COOLDOWN_SECONDS
    )


# ------------------------------------------------------------ replacement ---

def _list_models(base_url, key):
    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict):
        return payload.get("data") or []
    return payload or []


def _pick_replacement(models, retired_id):
    """Pick ONE replacement: prefer :free ids, then agentic/coding-leaning
    descriptions over domain-narrow ones, then largest context_length."""
    candidates = [
        m for m in models
        if isinstance(m, dict) and m.get("id") and m["id"] != retired_id
    ]
    if not candidates:
        raise ValueError("no candidate replacement models available")

    free = [m for m in candidates if str(m["id"]).endswith(":free")]
    pool = free or candidates

    def score(m):
        text = "{} {}".format(m.get("id", ""), m.get("description") or "").lower()
        agentic = sum(text.count(w) for w in _AGENTIC_WORDS)
        narrow = sum(text.count(w) for w in _NARROW_WORDS)
        return (agentic - narrow, m.get("context_length") or 0)

    best = max(pool, key=score)
    ctx = best.get("context_length")
    parts = ["free-tier" if str(best["id"]).endswith(":free") else "paid"]
    if ctx:
        parts.append("{}-token context".format(ctx))
    text = "{} {}".format(best.get("id", ""), best.get("description") or "").lower()
    if sum(text.count(w) for w in _AGENTIC_WORDS) > sum(text.count(w) for w in _NARROW_WORDS):
        parts.append("agentic/coding-oriented")
    return best["id"], ", ".join(parts)


# --------------------------------------------------------- MCP over SSE ---

def _sse_events(resp, deadline):
    """Yield (event, data) tuples from an SSE stream. urlopen's socket
    timeout bounds each blocking read; deadline bounds the whole call."""
    event, data_lines = None, []
    while time.monotonic() < deadline:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield (event or "message", "\n".join(data_lines))
            event, data_lines = None, []
        elif line.startswith(":"):
            continue  # SSE comment / keep-alive ping
        elif line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if data_lines:
        yield (event or "message", "\n".join(data_lines))


def _read_sse_event(resp, name, deadline):
    for event, data in _sse_events(resp, deadline):
        if event == name:
            return data
    raise TimeoutError("SSE stream never delivered '{}' event".format(name))


def _read_sse_result(resp, want_id, deadline):
    for _event, data in _sse_events(resp, deadline):
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("id") == want_id:
            if payload.get("error"):
                raise RuntimeError("MCP error: {}".format(payload["error"]))
            return payload.get("result")
    raise TimeoutError("SSE stream never delivered response id {}".format(want_id))


def _post_json(url, token, payload, deadline):
    """POST one JSON-RPC message. Returns the parsed body if the server
    answers inline with JSON, else None (response will arrive on the SSE
    stream instead — both wire behaviors exist)."""
    remaining = max(1.0, deadline - time.monotonic())
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
            "User-Agent": _USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=remaining) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            return None
        body = resp.read().decode("utf-8")
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _rpc_post(sse_resp, endpoint_url, token, payload, want_id, deadline):
    body = _post_json(endpoint_url, token, payload, deadline)
    if isinstance(body, dict) and body.get("id") == want_id:
        if body.get("error"):
            raise RuntimeError("MCP error: {}".format(body["error"]))
        return body.get("result")
    return _read_sse_result(sse_resp, want_id, deadline)


def _mcp_sse_call(url, token, tool_name, arguments, timeout=30.0):
    """One-shot MCP tools/call over the SSE transport (stdlib only)."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(
        url, headers={
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + token,
            "User-Agent": _USER_AGENT,
        }
    )
    # check-then-act across the network below is deliberately not locked:
    # worst case two racing hook invocations file one duplicate task each.
    with urllib.request.urlopen(req, timeout=timeout) as sse:
        endpoint = _read_sse_event(sse, "endpoint", deadline)
        endpoint_url = urljoin(url, endpoint)

        _rpc_post(sse, endpoint_url, token, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": _PLUGIN_NAME, "version": "0.1.0"},
            },
        }, 1, deadline)
        try:
            _post_json(endpoint_url, token, {
                "jsonrpc": "2.0", "method": "notifications/initialized"
            }, deadline)
        except Exception:
            pass

        return _rpc_post(sse, endpoint_url, token, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }, 2, deadline)


# --------------------------------------------------------------- announce ---

def _axiom_token():
    try:
        from agent.secret_scope import get_secret  # vendored in image

        return get_secret("AXIOM_TOKEN")
    except Exception:
        return os.environ.get("AXIOM_TOKEN", "")


def _announce(evt):
    """Evidence-first: structured log + one Axiom event, so a record of the
    detection survives even if Steward task creation fails."""
    payload = {
        "event": "primary_model_retirement_detected",
        "provider": evt.get("provider"),
        "model": evt.get("model"),
        "reason": evt.get("reason"),
        "status_code": evt.get("status_code"),
        "retryable": evt.get("retryable"),
        "error": evt.get("error"),
        "api_request_id": evt.get("api_request_id"),
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    log.warning("model_retirement_watch: %s", json.dumps(payload, default=str))
    try:
        token = _axiom_token()
        if not token:
            return
        axiom_event = {
            "_time": payload["detected_at"],
            "service": "hermes-agent",
            "environment": os.environ.get("HERMES_ENVIRONMENT", "company-ops"),
            "role": os.environ.get("HERMES_ROLE", "hermes-gateway"),
            "pod_name": os.environ.get("HOSTNAME", "unknown"),
            "event": payload,
        }
        req = urllib.request.Request(
            _AXIOM_INGEST_URL,
            data=json.dumps([axiom_event]).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
                "User-Agent": _USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:
        _record_failure("axiom_announce", exc)


# ------------------------------------------------------------ steward task ---

def _file_steward_task(evt, replacement, why):
    url = os.environ.get(
        "STEWARD_URL", "https://buildmyhouse.stewardacs.xyz/mcp/sse"
    )
    token = os.environ.get("STEWARD_TOKEN", "")
    if not token:
        raise RuntimeError("STEWARD_TOKEN is not set; cannot file Steward task")

    model = evt.get("model") or "unknown"
    provider = evt.get("provider") or "unknown"
    ended_at = evt.get("ended_at")
    if ended_at:
        detected = datetime.fromtimestamp(float(ended_at), tz=timezone.utc).isoformat()
    else:
        detected = datetime.now(timezone.utc).isoformat()
    message = (evt.get("error") or {}).get("message", "") if isinstance(evt.get("error"), dict) else (evt.get("error") or "")

    description = (
        "The Hermes primary model appears permanently retired.\n"
        "\n"
        "- Provider: {provider}\n"
        "- Model: {model}\n"
        "- Reason: {reason}\n"
        "- Status code: {status}\n"
        "- Error: {message}\n"
        "- Detected at: {detected}\n"
        "- API request id: {req_id}\n"
        "\n"
        "Suggested replacement: `{replacement}`\n"
        "Why: {why}\n"
        "\n"
        "Action required: update `hermes/config.yaml` (model.provider / "
        "model.default, plus the matching custom_providers entry) and "
        "redeploy through the full pipeline: builder_build_and_push → "
        "container_test → container_upgrade. Do not edit the running "
        "container directly."
    ).format(
        provider=provider,
        model=model,
        reason=evt.get("reason"),
        status=evt.get("status_code"),
        message=message,
        detected=detected,
        req_id=evt.get("api_request_id"),
        replacement=replacement,
        why=why,
    )

    result = _mcp_sse_call(
        url, token, "create_work",
        {
            "agent_id": "hermes",
            "title": "Primary model retired: {} ({})".format(model, provider),
            "description": description,
        },
    )
    log.info("model_retirement_watch: Steward task filed: %s", result)
    return result


# ------------------------------------------------------------------ hook ---

def on_api_request_error(**kwargs):
    # Runs off the hot path (only after retries are exhausted); the worst
    # case (~45s of model listing + Steward call) is acceptable there.
    try:
        if kwargs.get("reason") not in _PERMANENT_REASONS:
            return
        if kwargs.get("retryable") is not False:
            return

        provider = kwargs.get("provider") or ""
        model = kwargs.get("model") or ""
        config = _load_config()
        primary_provider, primary_model = _primary(config)
        if provider != primary_provider or model != primary_model:
            return  # fallback/aux lane failure — not a primary retirement

        now = time.time()
        if _cooldown_active(_read_marker(), provider, model, now):
            return

        _announce(kwargs)

        entry = _custom_provider(config, provider)
        if not entry or not entry.get("base_url"):
            raise RuntimeError(
                "no custom_providers entry with base_url for {!r}".format(provider)
            )
        key = os.environ.get(entry.get("key_env") or "", "")
        models = _list_models(entry["base_url"], key)
        replacement, why = _pick_replacement(models, model)

        _file_steward_task(kwargs, replacement, why)
        _write_marker(provider, model, now, replacement)
    except Exception as exc:
        _record_failure("on_api_request_error", exc)


def register(ctx):
    try:
        ctx.register_hook("api_request_error", on_api_request_error)
    except Exception as exc:
        _record_failure("register", exc)
