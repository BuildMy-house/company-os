"""pm-agent entrypoint wrapper for the shared company_ops/model_resolver.py.

Sets pm-agent defaults (key env, paths) then delegates. The shared module is
loaded by file path because importing the company_ops package pulls psycopg.
Import-safe: tests import `resolve_model` for the re-exported shared API.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent / "company_ops" / "model_resolver.py"
_spec = importlib.util.spec_from_file_location("model_resolver", _SHARED)
assert _spec and _spec.loader
shared = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("model_resolver", shared)
_spec.loader.exec_module(shared)


# Reasoning effort the Hermes runtime sends (hermes-config.yaml
# custom_providers[pm_llm].extra_body.reasoning). The probe sends the same field
# so a model that rejects it is skipped. tests/test_pm_agent_resolve_model.py
# fails if the two drift apart. `low` is the lowest level every reasoning-mandatory
# Nous model accepts (their supported_efforts are high/medium/low).
REASONING_EXTRA_BODY = {"reasoning": {"effort": "low"}}


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    hermes_home = env.get("HERMES_HOME", "/home/pmagent/.hermes")
    env.setdefault("RESOLVER_KEY_ENV", "PM_AGENT_LLM_API_KEY")
    env.setdefault("RESOLVER_CONFIG_PATH", f"{hermes_home}/config.yaml")
    env.setdefault("RESOLVER_BAKED_CONFIG", env.get("PM_AGENT_BAKED_CONFIG", str(_HERE / "hermes-config.yaml")))
    env.setdefault("RESOLVER_STATE_DIR", env.get("PM_AGENT_STATE_DIR", "/run/pm-agent"))
    env.setdefault("RESOLVER_TAGS", "product=hermes-agent,user=pm-agent")
    env.setdefault("RESOLVER_EXTRA_BODY", json.dumps(REASONING_EXTRA_BODY))
    return shared.main(argv)


if __name__ == "__main__":
    sys.exit(main())
