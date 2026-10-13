"""Pick a currently-working FREE model for a Hermes runtime (pm-agent, CEO Hermes).

Nous retires ``:free`` models without notice, and its /models listing keeps
showing retired ones, so this resolver PROBES candidates instead of trusting
the list. Stdlib only (the pm-agent venv has no yaml).

Flow: fetch /models -> keep free + tool-capable + long-context + unexpired
-> order by model-preferences.json then context length -> probe each with a
1-token chat completion -> first HTTP 200 wins. If every probe fails, reuse
the last working model from the state file. If nothing works, exit non-zero
(never silently use a paid model; paid only when PM_AGENT_ALLOW_PAID=true).

Shared by pm-agent/resolve_model.py and hermes/resolve_model.py (thin
wrappers that set the defaults below). Stdlib only and loaded BY PATH
(company_ops/__init__ pulls in psycopg, which the Hermes image lacks).

Env (generic names; wrappers map their own defaults):
  RESOLVER_KEY_ENV        name of the env var holding the API key
  RESOLVER_BASE_URL       OpenAI-compatible base URL
  RESOLVER_CONFIG_PATH    rendered Hermes config to write / compare
  RESOLVER_BAKED_CONFIG   source config whose model.default is replaced
  RESOLVER_STATE_DIR      writable dir for the last-working-model state file
  RESOLVER_PREFS          preferences JSON (preferred ids, exclusions)
  RESOLVER_ALLOW_PAID     "true" permits a paid fallback (PM_AGENT_ALLOW_PAID too)
  RESOLVER_TAGS           comma list sent as `tags` (Nous requires them)
  RESOLVER_EXTRA_BODY     JSON object merged into every probe request, so the probe
                          sends what the runtime sends (e.g. pm-agent's reasoning
                          effort) and a model that rejects it is skipped

Usage: model_resolver.py [--check]   (--check exits 75 if the model changed)
The API key is never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("model_resolver")

DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"
MIN_CONTEXT = 100_000
EXPIRY_MARGIN_SECONDS = 3 * 86400
PROBE_TIMEOUT = 30
EXIT_NO_MODEL = 2
EXIT_CHANGED = 75
HERE = Path(__file__).resolve().parent
DEFAULT_PREFS = HERE / "model-preferences.json"
DEFAULT_TAGS = ["product=hermes-agent"]
STATE_FILE = "resolved-model.json"
_MODEL_LINE = re.compile(r"^(\s+default:\s*)\S.*$", re.MULTILINE)
_MODEL_BLOCK = re.compile(r"^model:[ \t]*\n", re.MULTILINE)


class NoModelError(RuntimeError):
    pass


def is_free(model: dict[str, Any]) -> bool:
    """Free means BOTH prices are exactly 0. The ``:free`` suffix is not
    trusted (Nous lists retired ``:free`` ids) and not required (some free
    models, e.g. inclusionai/ling-3.1-flash, have no suffix)."""
    return _price(model) == (0.0, 0.0)


def _price(model: dict[str, Any]) -> tuple[float, float] | None:
    try:
        p = model["pricing"]
        return float(p["prompt"]), float(p["completion"])
    except (KeyError, TypeError, ValueError):
        return None


def _expired(model: dict[str, Any], now: float) -> bool:
    raw = model.get("expiration_date")
    if not raw:
        return False
    try:
        t = time.mktime(time.strptime(str(raw)[:10], "%Y-%m-%d"))
    except ValueError:
        return True  # unparseable expiry: be conservative
    return t < now + EXPIRY_MARGIN_SECONDS


def _usable(model: dict[str, Any], now: float, exclude: list[str]) -> bool:
    mid = str(model.get("id", ""))
    if not mid or any(s in mid for s in exclude):
        return False
    if "tools" not in (model.get("supported_parameters") or []):
        return False
    if int(model.get("context_length") or 0) < MIN_CONTEXT:
        return False
    return not _expired(model, now)


def load_preferences(path: Path | None = None) -> dict[str, Any]:
    path = path or DEFAULT_PREFS
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"preferred": [], "exclude_substrings": ["-fin", "-sante", "stealth/"]}


def candidates(models: list[dict[str, Any]], prefs: dict[str, Any], *, paid: bool = False,
               now: float | None = None) -> list[str]:
    """Ordered candidate ids. Free by default; cheapest-first paid when paid=True."""
    now = time.time() if now is None else now
    exclude = list(prefs.get("exclude_substrings", []))
    ok = []
    for m in models:
        price = _price(m)
        if price is None or not _usable(m, now, exclude):
            continue
        if paid == is_free(m):
            continue
        ok.append((m, price))
    if paid:
        ok.sort(key=lambda mp: (mp[1][0] + mp[1][1], -int(mp[0].get("context_length") or 0)))
        return [m["id"] for m, _ in ok]
    rank = {mid: i for i, mid in enumerate(prefs.get("preferred", []))}
    ok.sort(key=lambda mp: (rank.get(mp[0]["id"], len(rank)), -int(mp[0].get("context_length") or 0)))
    return [m["id"] for m, _ in ok]


def fetch_models(base_url: str = DEFAULT_BASE_URL) -> list[dict[str, Any]]:
    req = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:  # noqa: S310
        return list(json.loads(resp.read()).get("data", []))


def probe(model_id: str, api_key: str, base_url: str = DEFAULT_BASE_URL,
          tags: list[str] | None = None, extra_body: dict[str, Any] | None = None) -> bool:
    """True iff a 1-token chat completion returns HTTP 200. Never logs the key.

    ``extra_body`` is merged in last so the probe sends the same extra request
    fields as the runtime (e.g. reasoning effort).
    """
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "tags": tags or DEFAULT_TAGS,
        **(extra_body or {}),
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:  # noqa: S310
            return resp.status == 200
    except urllib.error.HTTPError as e:
        log.info("probe %s -> HTTP %s", model_id, e.code)
    except (OSError, ValueError) as e:
        log.info("probe %s -> %s", model_id, type(e).__name__)
    return False


def _read_state(state_dir: Path) -> str | None:
    try:
        return str(json.loads((state_dir / STATE_FILE).read_text())["model"])
    except (OSError, ValueError, KeyError):
        return None


def _write_state(state_dir: Path, model: str) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / STATE_FILE).write_text(json.dumps({"model": model, "at": int(time.time())}))
    except OSError as e:
        log.warning("could not write state file: %s", e)


def resolve(api_key: str, state_dir: Path, *, allow_paid: bool = False,
            prefs: dict[str, Any] | None = None,
            fetch=fetch_models, probe_fn=probe) -> tuple[str, str]:
    """Return (model_id, reason). Raises NoModelError when nothing is usable."""
    prefs = prefs if prefs is not None else load_preferences()
    try:
        models = fetch()
    except (OSError, ValueError) as e:
        log.warning("could not fetch /models (%s); trying last-known model", type(e).__name__)
        models = []
    for mid in candidates(models, prefs):
        if probe_fn(mid, api_key):
            _write_state(state_dir, mid)
            return mid, "first free candidate passing probe"
    last = _read_state(state_dir)
    if last and probe_fn(last, api_key):
        return last, "all fresh candidates failed; reusing last working model"
    if allow_paid:
        for mid in candidates(models, prefs, paid=True):
            if probe_fn(mid, api_key):
                _write_state(state_dir, mid)
                return mid, "PAID fallback (PM_AGENT_ALLOW_PAID=true), cheapest with tools"
    raise NoModelError(
        "no free tool-capable model passed a live probe and no previously working "
        "model is recorded; set PM_AGENT_ALLOW_PAID=true to allow a paid fallback"
    )


def _default_span(src: str) -> re.Match[str] | None:
    """The first `default:` line inside the top-level `model:` block."""
    b = _MODEL_BLOCK.search(src)
    if not b:
        return None
    nxt = re.compile(r"^[A-Za-z_]", re.MULTILINE).search(src, b.end())
    return _MODEL_LINE.search(src, b.end(), nxt.start() if nxt else len(src))


def render_config(src: str, model: str) -> str:
    """Replace model.default in the Hermes config text (stdlib: no yaml parser)."""
    m = _default_span(src)
    if m is None:
        raise ValueError("model.default line not found in Hermes config")
    return src[: m.start()] + f"{m.group(1)}{model}" + src[m.end():]


def current_model(config_path: Path) -> str | None:
    try:
        m = _default_span(config_path.read_text())
    except OSError:
        return None
    return m.group(0).split(":", 1)[1].strip() if m else None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    env = os.environ
    key_env = env.get("RESOLVER_KEY_ENV", "PM_AGENT_LLM_API_KEY")
    api_key = env.get(key_env, "")
    if not api_key:
        log.error("%s is required", key_env)
        return EXIT_NO_MODEL
    base_url = env.get("RESOLVER_BASE_URL", DEFAULT_BASE_URL)
    tags = [t for t in env.get("RESOLVER_TAGS", "").split(",") if t] or DEFAULT_TAGS
    try:
        extra_body = json.loads(env.get("RESOLVER_EXTRA_BODY") or "{}")
    except ValueError:
        log.error("RESOLVER_EXTRA_BODY is not valid JSON")
        return EXIT_NO_MODEL
    if not isinstance(extra_body, dict):
        log.error("RESOLVER_EXTRA_BODY must be a JSON object")
        return EXIT_NO_MODEL
    config_path = Path(env["RESOLVER_CONFIG_PATH"])
    state_dir = Path(env.get("RESOLVER_STATE_DIR", "/run/pm-agent"))
    allow_paid = "true" in (env.get("RESOLVER_ALLOW_PAID", "").lower(),
                            env.get("PM_AGENT_ALLOW_PAID", "").lower())
    prefs = load_preferences(Path(env["RESOLVER_PREFS"]) if env.get("RESOLVER_PREFS") else None)
    try:
        model, reason = resolve(
            api_key, state_dir, allow_paid=allow_paid, prefs=prefs,
            fetch=lambda: fetch_models(base_url),
            probe_fn=lambda mid, k: probe(mid, k, base_url, tags, extra_body),
        )
    except NoModelError as e:
        log.error("model resolution failed: %s", e)
        return EXIT_NO_MODEL
    log.info(json.dumps({"event": "model_resolved", "model": model, "reason": reason}))
    if check:
        cur = current_model(config_path)
        if cur != model:
            log.info("model changed %s -> %s; restart required", cur, model)
            return EXIT_CHANGED
        return 0
    baked = Path(env.get("RESOLVER_BAKED_CONFIG", str(config_path)))
    text = render_config(baked.read_text(), model)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
