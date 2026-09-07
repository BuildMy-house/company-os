from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Generator

import psycopg
from psycopg.rows import dict_row

from company_ops.human_interface import ask_judgment, request_approval
from company_ops.observer import ObserverWriter


def _ensure_env() -> None:
    """Load missing operational variables from .env if present and not in test suite."""
    if "PYTEST_CURRENT_TEST" in os.environ or "TEST_COMPANY_DATABASE_URL" in os.environ:
        return
    env_file = Path(".env")
    if not env_file.exists():
        env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


def resolve_dsn(dsn: str) -> str:
    """Resolve docker-internal hostnames if unreachable on the current host."""
    if not dsn:
        return dsn
    if "@postgres:" in dsn or "@postgres/" in dsn:
        try:
            socket.gethostbyname("postgres")
        except socket.gaierror:
            # When run from host outside the compose network, bridge IP is typically 172.18.0.2
            return dsn.replace("@postgres:", "@172.18.0.2:").replace("@postgres/", "@172.18.0.2/")
    return dsn


@contextmanager
def _get_connection(
    conn_or_dsn: psycopg.Connection | str | Any,
) -> Generator[psycopg.Connection, None, None]:
    """Provide a working psycopg connection with dict_row factory and company search path."""
    _ensure_env()
    if hasattr(conn_or_dsn, "db") and hasattr(conn_or_dsn.db, "execute"):
        # Ledger instance passed
        conn = conn_or_dsn.db
        conn.execute("SET search_path TO company, public")
        yield conn
    elif isinstance(conn_or_dsn, str) or conn_or_dsn is None:
        dsn = conn_or_dsn or os.environ.get("TEST_COMPANY_DATABASE_URL") or os.environ.get("COMPANY_DATABASE_URL", "")
        if not dsn:
            raise ValueError("No database DSN provided and COMPANY_DATABASE_URL not set")
        dsn = resolve_dsn(dsn)
        conn = psycopg.connect(dsn, row_factory=dict_row)
        try:
            conn.execute("SET search_path TO company, public")
            yield conn
        finally:
            conn.close()
    else:
        # Already an open psycopg Connection
        conn_or_dsn.execute("SET search_path TO company, public")
        yield conn_or_dsn


def _format_cents(cents: int) -> str:
    return f"${cents / 100:.2f}"


def compile_spend_review(
    db: psycopg.Connection | str | Any = None,
    days: int = 30,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Compile a spend review report for the given period (default: trailing 30 days).

    Queries company.finance_expenses, company.provider_usage, and company.routing_lessons.
    Calculates total spend by category, month-over-month deltas (if prior-period data exists),
    and aggregates notable routing lessons.
    """
    if as_of is None:
        end_dt = datetime.now(UTC)
    elif isinstance(as_of, str):
        end_dt = datetime.fromisoformat(as_of)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
    elif isinstance(as_of, datetime):
        end_dt = as_of
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
    else:
        raise TypeError(f"Unsupported type for as_of: {type(as_of)}")

    start_dt = end_dt - timedelta(days=days)
    prior_start_dt = start_dt - timedelta(days=days)

    cur_start_iso = start_dt.isoformat()
    cur_end_iso = end_dt.isoformat()
    prior_start_iso = prior_start_dt.isoformat()
    prior_end_iso = cur_start_iso

    with _get_connection(db) as conn:
        # 1. Current period expenses
        cur_exp_rows = conn.execute(
            """
            SELECT id, category, amount_cents, recurrence, source, created_at
            FROM company.finance_expenses
            WHERE created_at::timestamptz >= %s::timestamptz
              AND created_at::timestamptz <= %s::timestamptz
            ORDER BY created_at ASC
            """,
            (cur_start_iso, cur_end_iso),
        ).fetchall()

        # 2. Prior period expenses
        prior_exp_rows = conn.execute(
            """
            SELECT id, category, amount_cents, recurrence, source, created_at
            FROM company.finance_expenses
            WHERE created_at::timestamptz >= %s::timestamptz
              AND created_at::timestamptz < %s::timestamptz
            ORDER BY created_at ASC
            """,
            (prior_start_iso, prior_end_iso),
        ).fetchall()

        # 3. Current period provider usage
        cur_usage_rows = conn.execute(
            """
            SELECT id, action_id, provider, model, provider_cost_cents, capacity_status, created_at
            FROM company.provider_usage
            WHERE created_at::timestamptz >= %s::timestamptz
              AND created_at::timestamptz <= %s::timestamptz
            ORDER BY created_at ASC
            """,
            (cur_start_iso, cur_end_iso),
        ).fetchall()

        # 4. Prior period provider usage
        prior_usage_rows = conn.execute(
            """
            SELECT id, action_id, provider, model, provider_cost_cents, capacity_status, created_at
            FROM company.provider_usage
            WHERE created_at::timestamptz >= %s::timestamptz
              AND created_at::timestamptz < %s::timestamptz
            ORDER BY created_at ASC
            """,
            (prior_start_iso, prior_end_iso),
        ).fetchall()

        # 5. Routing lessons recorded in current period
        cur_lesson_rows = conn.execute(
            """
            SELECT id, model, task_type, reason, created_at
            FROM company.routing_lessons
            WHERE created_at::timestamptz >= %s::timestamptz
              AND created_at::timestamptz <= %s::timestamptz
            ORDER BY created_at DESC
            """,
            (cur_start_iso, cur_end_iso),
        ).fetchall()

    # Aggregate current period spend
    spend_by_category: dict[str, int] = {}
    for r in cur_exp_rows:
        cat = str(r["category"])
        spend_by_category[cat] = spend_by_category.get(cat, 0) + int(r["amount_cents"])

    cur_provider_cost = 0
    provider_breakdown: dict[str, dict[str, Any]] = {}
    for r in cur_usage_rows:
        provider = str(r["provider"])
        cost = int(r["provider_cost_cents"])
        cur_provider_cost += cost
        if provider not in provider_breakdown:
            provider_breakdown[provider] = {"cost_cents": 0, "call_count": 0, "models": {}}
        provider_breakdown[provider]["cost_cents"] += cost
        provider_breakdown[provider]["call_count"] += 1
        model = str(r["model"])
        provider_breakdown[provider]["models"][model] = (
            provider_breakdown[provider]["models"].get(model, 0) + cost
        )

    if cur_provider_cost > 0 or len(cur_usage_rows) > 0:
        spend_by_category["provider_api"] = (
            spend_by_category.get("provider_api", 0) + cur_provider_cost
        )

    total_spend_cents = sum(spend_by_category.values())

    # Aggregate prior period spend
    prior_spend_by_category: dict[str, int] = {}
    for r in prior_exp_rows:
        cat = str(r["category"])
        prior_spend_by_category[cat] = (
            prior_spend_by_category.get(cat, 0) + int(r["amount_cents"])
        )

    prior_provider_cost = 0
    for r in prior_usage_rows:
        prior_provider_cost += int(r["provider_cost_cents"])

    if prior_provider_cost > 0 or len(prior_usage_rows) > 0:
        prior_spend_by_category["provider_api"] = (
            prior_spend_by_category.get("provider_api", 0) + prior_provider_cost
        )

    prior_total_spend_cents = sum(prior_spend_by_category.values())
    has_prior_data = bool(prior_exp_rows or prior_usage_rows)

    # Calculate Month-over-Month delta if prior period data exists
    mom_delta: dict[str, Any] | None = None
    if has_prior_data:
        delta_cents = total_spend_cents - prior_total_spend_cents
        delta_pct = (
            round((delta_cents / prior_total_spend_cents) * 100.0, 1)
            if prior_total_spend_cents > 0
            else None
        )
        by_category_delta: dict[str, dict[str, Any]] = {}
        all_cats = sorted(set(spend_by_category.keys()) | set(prior_spend_by_category.keys()))
        for c in all_cats:
            cur_c = spend_by_category.get(c, 0)
            pri_c = prior_spend_by_category.get(c, 0)
            diff_c = cur_c - pri_c
            pct_c = round((diff_c / pri_c) * 100.0, 1) if pri_c > 0 else None
            by_category_delta[c] = {
                "current_cents": cur_c,
                "prior_cents": pri_c,
                "delta_cents": diff_c,
                "delta_pct": pct_c,
            }
        mom_delta = {
            "delta_cents": delta_cents,
            "delta_pct": delta_pct,
            "by_category": by_category_delta,
        }

    # Notable routing lessons
    routing_lessons: list[dict[str, Any]] = [
        {
            "id": str(r["id"]),
            "model": str(r["model"]),
            "task_type": str(r["task_type"]),
            "reason": str(r["reason"]),
            "created_at": str(r["created_at"]),
        }
        for r in cur_lesson_rows
    ]

    # Proposals (automated suggestions from spend review)
    detected_proposals: list[str] = []
    if mom_delta:
        for cat, cat_info in mom_delta["by_category"].items():
            if cat_info["delta_cents"] >= 1000 and (cat_info["delta_pct"] is None or cat_info["delta_pct"] >= 20.0):
                pct_str = f"+{cat_info['delta_pct']:.1f}%" if cat_info["delta_pct"] is not None else "new"
                detected_proposals.append(
                    f"Review spend spike in '{cat}': +{_format_cents(cat_info['delta_cents'])} ({pct_str})"
                )
    for r in cur_exp_rows:
        if r["recurrence"] == "monthly":
            detected_proposals.append(
                f"Review renewal for recurring monthly expense '{r['category']}': {_format_cents(int(r['amount_cents']))}/mo"
            )

    return {
        "period_days": days,
        "current_period": {
            "start": cur_start_iso,
            "end": cur_end_iso,
        },
        "prior_period": {
            "start": prior_start_iso,
            "end": prior_end_iso,
        },
        "has_prior_data": has_prior_data,
        "total_spend_cents": total_spend_cents,
        "spend_by_category": spend_by_category,
        "prior_total_spend_cents": prior_total_spend_cents if has_prior_data else None,
        "prior_spend_by_category": prior_spend_by_category if has_prior_data else None,
        "month_over_month_delta": mom_delta,
        "provider_usage": {
            "total_cost_cents": cur_provider_cost,
            "call_count": len(cur_usage_rows),
            "by_provider": provider_breakdown,
        },
        "routing_lessons": routing_lessons,
        "expenses_count": len(cur_exp_rows),
        "detected_proposals": detected_proposals,
    }


def format_spend_review_message(
    report: dict[str, Any],
    proposal: str | None = None,
) -> dict[str, Any]:
    """Format spend review report into Human Interface message payload.

    If proposal is specified, returns request_approval payload.
    Otherwise (exploratory review), returns ask_judgment payload.
    """
    days = report["period_days"]
    cur_start = report["current_period"]["start"][:10]
    cur_end = report["current_period"]["end"][:10]
    total_spend = _format_cents(report["total_spend_cents"])

    mom_str = ""
    mom = report.get("month_over_month_delta")
    if mom:
        d_cents = mom["delta_cents"]
        d_pct = mom["delta_pct"]
        sign = "+" if d_cents >= 0 else ""
        pct_display = f" ({sign}{d_pct:.1f}% MoM)" if d_pct is not None else ""
        mom_str = f" ({sign}{_format_cents(d_cents)}{pct_display})"
    elif not report.get("has_prior_data"):
        mom_str = " (no prior baseline)"

    category_lines: list[str] = []
    spend_by_cat = report.get("spend_by_category", {})
    if spend_by_cat:
        for cat, cents in sorted(spend_by_cat.items(), key=lambda x: -x[1]):
            cat_mom = ""
            if mom and cat in mom["by_category"]:
                cat_info = mom["by_category"][cat]
                cat_pct = cat_info["delta_pct"]
                if cat_pct is not None:
                    cat_mom = f" ({'+' if cat_pct >= 0 else ''}{cat_pct:.1f}% MoM)"
                elif cat_info["prior_cents"] == 0:
                    cat_mom = " (new)"
            category_lines.append(f"• {cat}: {_format_cents(cents)}{cat_mom}")
    else:
        category_lines.append("• (no expenses recorded)")

    provider_lines: list[str] = []
    p_usage = report.get("provider_usage", {})
    by_provider = p_usage.get("by_provider", {})
    if by_provider:
        for p_name, p_data in sorted(by_provider.items()):
            provider_lines.append(
                f"  - {p_name}: {_format_cents(p_data['cost_cents'])} ({p_data['call_count']} calls)"
            )

    lesson_lines: list[str] = []
    lessons = report.get("routing_lessons", [])
    if lessons:
        lesson_lines.append("\nNotable Routing Lessons:")
        for l in lessons[:5]:
            lesson_lines.append(f"• {l['model']} on '{l['task_type']}': {l['reason']}")

    obs_lines: list[str] = []
    proposals = report.get("detected_proposals", [])
    if proposals:
        obs_lines.append("\nObservations / Notes:")
        for p in proposals[:5]:
            obs_lines.append(f"• {p}")

    if proposal:
        action_type = "request_approval"
        lines = [
            f"Monthly Spend Review (Trailing {days}d: {cur_start} to {cur_end})",
            f"Total Spend: {total_spend}{mom_str}\n",
            f"Proposal: {proposal}\n",
            "Spend by Category:",
            *category_lines,
        ]
        if provider_lines:
            lines.append("Provider Usage Details:")
            lines.extend(provider_lines)
        if lesson_lines:
            lines.extend(lesson_lines)
        if obs_lines:
            lines.extend(obs_lines)

        proposal_text = "\n".join(lines)
        risk_str = (
            f"Spend delta: {mom['delta_pct']:+.1f}% MoM"
            if mom and mom["delta_pct"] is not None
            else "Standard operational review"
        )
        return {
            "action_type": action_type,
            "proposal": proposal_text,
            "cost_estimate": total_spend,
            "risk": risk_str,
            "importance": "medium",
        }
    else:
        action_type = "ask_judgment"
        lines = [
            f"Monthly Spend Review (Trailing {days}d: {cur_start} to {cur_end})",
            f"Total Spend: {total_spend}{mom_str}\n",
            "Spend by Category:",
            *category_lines,
        ]
        if provider_lines:
            lines.append("Provider Usage Details:")
            lines.extend(provider_lines)
        if lesson_lines:
            lines.extend(lesson_lines)
        if obs_lines:
            lines.extend(obs_lines)
        lines.append("\nHow should we proceed with this month's budget and resource allocations?")

        question_text = "\n".join(lines)
        options = "approve-as-is, adjust-category-budgets, reduce-provider-spend, drill-down"
        return {
            "action_type": action_type,
            "question": question_text,
            "options": options,
            "reason": "Recurring monthly spend review ritual",
            "importance": "medium",
        }


def run_spend_review(
    company_db: psycopg.Connection | str | Any = None,
    observer_writer: ObserverWriter | Any = None,
    days: int = 30,
    as_of: datetime | str | None = None,
    proposal: str | None = None,
    dry_run: bool = False,
    initiated_by: str = "spend_review",
) -> dict[str, Any]:
    """Execute spend review compilation and fire the result through Human Interface."""
    _ensure_env()
    report = compile_spend_review(db=company_db, days=days, as_of=as_of)
    msg_info = format_spend_review_message(report=report, proposal=proposal)

    if dry_run:
        return {
            "report": report,
            "human_interface": {
                "dry_run": True,
                **msg_info,
            },
        }

    # Connect to ObserverWriter for live Human Interface call
    writer: ObserverWriter
    should_close = False
    if hasattr(observer_writer, "record_human_request") or isinstance(observer_writer, ObserverWriter):
        writer = observer_writer
    else:
        obs_dsn = observer_writer or os.environ.get("TEST_OBSERVER_DATABASE_URL") or os.environ.get("OBSERVER_DATABASE_URL", "")
        if not obs_dsn:
            raise ValueError("No Observer DSN provided and OBSERVER_DATABASE_URL not set")
        obs_dsn = resolve_dsn(obs_dsn)
        writer = ObserverWriter(dsn=obs_dsn)
        should_close = True

    try:
        action_type = msg_info["action_type"]
        if action_type == "request_approval":
            hi_res = request_approval(
                writer=writer,
                proposal=msg_info["proposal"],
                cost_estimate=msg_info.get("cost_estimate"),
                risk=msg_info.get("risk"),
                importance=msg_info.get("importance", "medium"),
                initiated_by=initiated_by,
                blocking=False,
            )
        else:
            hi_res = ask_judgment(
                writer=writer,
                question=msg_info["question"],
                options=msg_info.get("options"),
                reason=msg_info.get("reason"),
                importance=msg_info.get("importance", "medium"),
                initiated_by=initiated_by,
            )

        return {
            "report": report,
            "human_interface": {
                "action_type": action_type,
                "request_id": hi_res.get("request_id"),
                "source": hi_res.get("source"),
                "outcome": hi_res.get("outcome"),
                "message": msg_info.get("proposal") or msg_info.get("question"),
            },
        }
    finally:
        if should_close:
            writer.close()
