"""CEO Hermes entrypoint wrapper for the shared company_ops/model_resolver.py.

Resolves a live-probed FREE Nous model and renders it into model.default of
/opt/data/config.yaml (the fallback_providers glm-5.3-flash lane and the
tokenrouter auxiliary slots are left untouched). Loaded by file path because
the company_ops package __init__ imports psycopg.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_SHARED = Path(os.environ.get("COMPANY_OPS_DIR", "/opt/company-ops")) / "company_ops" / "model_resolver.py"
if not _SHARED.exists():
    _SHARED = Path(__file__).resolve().parent.parent / "company_ops" / "model_resolver.py"
_spec = importlib.util.spec_from_file_location("model_resolver", _SHARED)
assert _spec and _spec.loader
shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shared)


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    env.setdefault("RESOLVER_KEY_ENV", "NOUS_API_KEY")
    env.setdefault("RESOLVER_CONFIG_PATH", "/opt/data/config.yaml")
    env.setdefault("RESOLVER_BAKED_CONFIG", "/opt/company-ops/hermes/config.yaml")
    env.setdefault("RESOLVER_STATE_DIR", "/opt/data/.model-resolver")
    env.setdefault("RESOLVER_TAGS", "product=hermes-agent,user=hermes-agent")
    return shared.main(argv)


if __name__ == "__main__":
    sys.exit(main())
