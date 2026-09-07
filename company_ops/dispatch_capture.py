"""Capture per-dispatch provider usage (token counts, cost) from CLI tool output.

Parses raw stdout from opencode, codex, and agy/Antigravity CLIs, then records
the results into company.provider_usage (via Ledger.record_provider_usage) and
optionally updates a matching company.resource_pools entry.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Parsers — each takes raw stdout text + model name, returns normalized dict
# ---------------------------------------------------------------------------

_EMPTY = {"provider": "", "model": None, "input_tokens": 0, "output_tokens": 0,
          "total_tokens": 0, "cost_cents": None}


def parse_opencode_output(raw_text: str, model: str) -> dict:
    """Parse opencode NDJSON: sum tokens.input/output and cost across step_finish events."""
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    cost_dollars = 0.0
    cost_present = False
    found = False

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "step_finish":
            continue
        found = True
        part = obj.get("part", {})
        tokens = part.get("tokens", {})
        input_tokens += tokens.get("input", 0) or 0
        output_tokens += tokens.get("output", 0) or 0
        total_tokens += tokens.get("total", 0) or 0
        if "cost" in part:
            cost_present = True
            cost_dollars += part["cost"] or 0

    if not found:
        return {**_EMPTY, "provider": "opencode", "model": model}

    cost_cents = round(cost_dollars * 100) if cost_present else None
    return {
        "provider": "opencode",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens or input_tokens + output_tokens,
        "cost_cents": cost_cents,
    }


def parse_codex_output(raw_text: str, model: str) -> dict:
    """Parse codex NDJSON: sum input_tokens/output_tokens across turn.completed events."""
    input_tokens = 0
    output_tokens = 0
    found = False

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "turn.completed":
            continue
        found = True
        usage = obj.get("usage", {})
        input_tokens += usage.get("input_tokens", 0) or 0
        output_tokens += usage.get("output_tokens", 0) or 0

    if not found:
        return {**_EMPTY, "provider": "codex", "model": model}

    return {
        "provider": "codex",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_cents": None,
    }


def parse_agy_output(raw_text: str, model: str) -> dict:
    """Parse agy single JSON: read usage fields."""
    try:
        obj = json.loads(raw_text.strip())
    except (json.JSONDecodeError, ValueError):
        return {**_EMPTY, "provider": "antigravity", "model": model}

    usage = obj.get("usage", {})
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    total_tokens = usage.get("total_tokens", 0) or (input_tokens + output_tokens)

    return {
        "provider": "antigravity",
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_cents": None,
    }


# ---------------------------------------------------------------------------
# Record parsed usage into the ledger (+ optional resource-pool side effect)
# ---------------------------------------------------------------------------

def capture_and_record(
    ledger,
    parsed: dict,
    action_id: str | None = None,
    resource_pool_id: str | None = None,
    resource_pools_conn=None,
) -> str:
    """Record parsed dispatch usage into provider_usage; optionally update a resource pool.

    Returns the provider_usage row id.
    """
    from .policy import model_cost

    cost_cents = parsed["cost_cents"]

    # Try policy cost estimation when the CLI didn't report a dollar figure
    if cost_cents is None and parsed["model"] is not None:
        estimate = model_cost(parsed["model"], parsed["input_tokens"], parsed["output_tokens"])
        if estimate.get("known") and estimate.get("estimated_cost_cents") is not None:
            cost_cents = estimate["estimated_cost_cents"]

    usage_id = ledger.record_provider_usage(
        action_id,
        parsed["provider"],
        parsed["model"] or "unknown",
        provider_cost_cents=cost_cents or 0,
        capacity_status="available",
    )

    # Best-effort resource pool update — must never prevent the record above
    if resource_pool_id and resource_pools_conn is not None:
        try:
            pool = resource_pools_conn.get_pool(resource_pool_id)
            if pool is not None:
                unit = pool.get("unit", "requests")
                if unit == "dollars":
                    increment = (cost_cents or 0) / 100
                elif unit == "tokens":
                    increment = parsed["total_tokens"]
                else:  # "requests" or anything else
                    increment = 1
                new_consumed = float(pool["consumed_amount"]) + increment
                resource_pools_conn.update_consumed(
                    resource_pool_id, new_consumed,
                    source=f"dispatch capture: {parsed['provider']}",
                )
        except Exception as exc:
            print(f"dispatch_capture: resource pool update failed: {exc}", file=sys.stderr)

    return usage_id


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="company_ops.dispatch_capture",
        description="Capture provider usage from CLI dispatch output files.",
    )
    parser.add_argument("tool", choices=["opencode", "codex", "agy"],
                        help="Which CLI produced the output")
    parser.add_argument("output_file", help="Path to raw stdout file")
    parser.add_argument("--model", default="unknown", help="Model used for the dispatch")
    parser.add_argument(
        "--db",
        default=os.environ.get("COMPANY_DATABASE_URL")
        or os.environ.get("TEST_COMPANY_DATABASE_URL", ""),
    )
    parser.add_argument("--action-id", default=None, help="Optional action FK")
    parser.add_argument("--resource-pool-id", default=None, help="Optional resource pool to update")

    args = parser.parse_args(argv)

    if not args.db:
        print(json.dumps({"error": "No database DSN (set --db or COMPANY_DATABASE_URL)"}))
        return 2

    raw_text = Path(args.output_file).read_text()

    parsers = {
        "opencode": parse_opencode_output,
        "codex": parse_codex_output,
        "agy": parse_agy_output,
    }
    parsed = parsers[args.tool](raw_text, args.model)

    from .ledger import Ledger
    ledger = Ledger(args.db)
    ledger.init()

    rp_conn = None
    if args.resource_pool_id:
        from .resource_pools import ResourcePools
        try:
            rp_conn = ResourcePools(args.db)
        except Exception:
            pass  # proceed without resource pool if connection fails

    try:
        usage_id = capture_and_record(
            ledger, parsed, action_id=args.action_id,
            resource_pool_id=args.resource_pool_id, resource_pools_conn=rp_conn,
        )
        print(json.dumps({"usage_id": usage_id, **parsed}))
        return 0
    finally:
        ledger.close()
        if rp_conn:
            rp_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
