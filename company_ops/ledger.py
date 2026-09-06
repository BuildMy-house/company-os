from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import psycopg.rows
import psycopg.sql

from .policy import DAILY_ALLOWANCE, DAILY_CAP, action_cost, credit_allocation

_SCHEMA_SQL = (Path(__file__).parent.parent / "sql" / "company_schema.sql").read_text()


def now() -> str:
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, dsn: str) -> None:
        self.db = psycopg.connect(dsn, row_factory=psycopg.rows.dict_row)
        self.db.execute("SET search_path TO company, public")

    def close(self) -> None:
        self.db.close()

    def init(self) -> None:
        table_exists = self.db.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'company' AND table_name = 'plans'"
        ).fetchone()
        if not table_exists:
            self.db.execute(_SCHEMA_SQL)
        if self.balance() == 0 and not self.db.execute("SELECT 1 FROM credit_transactions LIMIT 1").fetchone():
            self._credit("daily_allowance", DAILY_ALLOWANCE, "initial")
        self.db.commit()

    def _credit(self, kind: str, amount: int, reference: str | None) -> None:
        self.db.execute(
            psycopg.sql.SQL(
                "INSERT INTO {} ({}, {}, {}, {}, {}) VALUES (%s, %s, %s, %s, %s)"
            ).format(
                psycopg.sql.Identifier("credit_transactions"),
                psycopg.sql.Identifier("id"),
                psycopg.sql.Identifier("kind"),
                psycopg.sql.Identifier("amount"),
                psycopg.sql.Identifier("reference"),
                psycopg.sql.Identifier("created_at"),
            ),
            (str(uuid.uuid4()), kind, amount, reference, now()),
        )

    def balance(self) -> int:
        row = self.db.execute("SELECT COALESCE(SUM(amount), 0) AS balance FROM credit_transactions").fetchone()
        return int(row["balance"])

    def create_plan(self, goal: str, owner: str = "hermes", budget_credits: int = 0, plan_id: str | None = None) -> str:
        plan_id = plan_id or f"PLAN-{uuid.uuid4().hex[:12]}"
        try:
            self.db.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {} ({}, {}, {}, {}, {}, {}, {}) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                ).format(
                    psycopg.sql.Identifier("plans"),
                    psycopg.sql.Identifier("id"),
                    psycopg.sql.Identifier("goal"),
                    psycopg.sql.Identifier("owner"),
                    psycopg.sql.Identifier("status"),
                    psycopg.sql.Identifier("budget_credits"),
                    psycopg.sql.Identifier("created_at"),
                    psycopg.sql.Identifier("result"),
                ),
                (plan_id, goal, owner, "proposed", budget_credits, now(), None),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return plan_id

    def charge(self, agent: str, action_type: str, plan_id: str | None = None,
               idempotency_key: str | None = None, provider_cost_cents: int = 0,
               result: str = "success") -> dict:
        credits = action_cost(action_type)
        key = idempotency_key or str(uuid.uuid4())
        try:
            existing = self.db.execute("SELECT * FROM actions WHERE idempotency_key = %s", (key,)).fetchone()
            if existing:
                return dict(existing)
            if self.balance() < credits:
                raise ValueError("insufficient credits")
            action_id = f"ACTION-{uuid.uuid4().hex[:12]}"
            self._credit("action", -credits, action_id)
            self.db.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {} ({}, {}, {}, {}, {}, {}, {}, {}, {}, {}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(
                    psycopg.sql.Identifier("actions"),
                    psycopg.sql.Identifier("id"),
                    psycopg.sql.Identifier("plan_id"),
                    psycopg.sql.Identifier("agent"),
                    psycopg.sql.Identifier("action_type"),
                    psycopg.sql.Identifier("credits"),
                    psycopg.sql.Identifier("provider_cost_cents"),
                    psycopg.sql.Identifier("status"),
                    psycopg.sql.Identifier("idempotency_key"),
                    psycopg.sql.Identifier("created_at"),
                    psycopg.sql.Identifier("result"),
                ),
                (action_id, plan_id, agent, action_type, credits, provider_cost_cents,
                 result, key, now(), None),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return dict(self.db.execute("SELECT * FROM actions WHERE id = %s", (action_id,)).fetchone())

    def record_provider_usage(self, action_id: str, provider: str, model: str,
                              provider_cost_cents: int = 0, capacity_status: str = "available") -> str:
        usage_id = f"USAGE-{uuid.uuid4().hex[:12]}"
        try:
            self.db.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {} ({}, {}, {}, {}, {}, {}, {}) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                ).format(
                    psycopg.sql.Identifier("provider_usage"),
                    psycopg.sql.Identifier("id"),
                    psycopg.sql.Identifier("action_id"),
                    psycopg.sql.Identifier("provider"),
                    psycopg.sql.Identifier("model"),
                    psycopg.sql.Identifier("provider_cost_cents"),
                    psycopg.sql.Identifier("capacity_status"),
                    psycopg.sql.Identifier("created_at"),
                ),
                (usage_id, action_id, provider, model, provider_cost_cents, capacity_status, now()),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return usage_id

    def add_routing_lesson(self, model: str, task_type: str, reason: str) -> str:
        lesson_id = f"LESSON-{uuid.uuid4().hex[:12]}"
        try:
            self.db.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {} ({}, {}, {}, {}, {}) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT ({}, {}) DO NOTHING"
                ).format(
                    psycopg.sql.Identifier("routing_lessons"),
                    psycopg.sql.Identifier("id"),
                    psycopg.sql.Identifier("model"),
                    psycopg.sql.Identifier("task_type"),
                    psycopg.sql.Identifier("reason"),
                    psycopg.sql.Identifier("created_at"),
                    psycopg.sql.Identifier("model"),
                    psycopg.sql.Identifier("task_type"),
                ),
                (lesson_id, model, task_type, reason, now()),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        row = self.db.execute(
            "SELECT id FROM routing_lessons WHERE model = %s AND task_type = %s",
            (model, task_type),
        ).fetchone()
        return str(row["id"])

    def avoided_models(self, task_type: str) -> set[str]:
        rows = self.db.execute(
            "SELECT model FROM routing_lessons WHERE task_type = %s", (task_type,)
        )
        return {str(row["model"]) for row in rows}

    def record_revenue(self, net_revenue_cents: int, source: str) -> dict:
        credits = credit_allocation(net_revenue_cents)
        revenue_id = f"REV-{uuid.uuid4().hex[:12]}"
        try:
            self.db.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {} ({}, {}, {}, {}, {}) VALUES (%s, %s, %s, %s, %s)"
                ).format(
                    psycopg.sql.Identifier("revenue_events"),
                    psycopg.sql.Identifier("id"),
                    psycopg.sql.Identifier("net_revenue_cents"),
                    psycopg.sql.Identifier("source"),
                    psycopg.sql.Identifier("credits_created"),
                    psycopg.sql.Identifier("created_at"),
                ),
                (revenue_id, net_revenue_cents, source, credits, now()),
            )
            self._credit("revenue", credits // 100, revenue_id)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {"id": revenue_id, "net_revenue_cents": net_revenue_cents, "credits_created": credits // 100}

    def add_tool_request(self, requested_by: str, name: str, purpose: str, cost_credits: int, risk: str = "low") -> str:
        request_id = f"REQ-{uuid.uuid4().hex[:12]}"
        try:
            self.db.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {} ({}, {}, {}, {}, {}, {}, {}, {}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(
                    psycopg.sql.Identifier("tool_requests"),
                    psycopg.sql.Identifier("id"),
                    psycopg.sql.Identifier("requested_by"),
                    psycopg.sql.Identifier("name"),
                    psycopg.sql.Identifier("purpose"),
                    psycopg.sql.Identifier("cost_credits"),
                    psycopg.sql.Identifier("risk"),
                    psycopg.sql.Identifier("status"),
                    psycopg.sql.Identifier("created_at"),
                ),
                (request_id, requested_by, name, purpose, cost_credits, risk, "proposed", now()),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return request_id

    def add_deployment(self, environment: str, version: str, status: str = "proposed") -> str:
        deployment_id = f"DEPLOY-{uuid.uuid4().hex[:12]}"
        try:
            self.db.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {} ({}, {}, {}, {}, {}, {}) VALUES (%s, %s, %s, %s, %s, %s)"
                ).format(
                    psycopg.sql.Identifier("deployments"),
                    psycopg.sql.Identifier("id"),
                    psycopg.sql.Identifier("environment"),
                    psycopg.sql.Identifier("version"),
                    psycopg.sql.Identifier("status"),
                    psycopg.sql.Identifier("created_at"),
                    psycopg.sql.Identifier("result"),
                ),
                (deployment_id, environment, version, status, now(), None),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return deployment_id

    def update_deployment_status(self, deployment_id: str, status: str, result: str | None = None) -> None:
        try:
            self.db.execute(
                psycopg.sql.SQL("UPDATE {} SET {} = %s, {} = %s WHERE {} = %s").format(
                    psycopg.sql.Identifier("deployments"),
                    psycopg.sql.Identifier("status"),
                    psycopg.sql.Identifier("result"),
                    psycopg.sql.Identifier("id"),
                ),
                (status, result, deployment_id),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def status(self) -> dict:
        return {
            "balance": self.balance(),
            "plans": self.db.execute("SELECT COUNT(*) AS count FROM plans").fetchone()["count"],
            "actions": self.db.execute("SELECT COUNT(*) AS count FROM actions").fetchone()["count"],
            "revenue_cents": self.db.execute("SELECT COALESCE(SUM(net_revenue_cents), 0) AS total FROM revenue_events").fetchone()["total"],
            "daily_cap": DAILY_CAP,
        }
