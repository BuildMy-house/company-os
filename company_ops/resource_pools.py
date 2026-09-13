from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row


def now() -> str:
    return datetime.now(UTC).isoformat()


class ResourcePools:
    def __init__(self, dsn: str | None = None) -> None:
        if not dsn:
            raise ValueError("No DSN provided")
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self.conn.execute("SET search_path TO company, public")

    def close(self) -> None:
        self.conn.close()

    def record_pool(
        self,
        pool_id: str,
        provider: str,
        unit: str,
        quota_amount,
        period_type: str,
        period_start: str,
        period_end_or_reset_at: str,
        consumed_amount=0,
        source: str = "",
        level: str = "claude_to_worker",
    ) -> None:
        self.conn.execute(
            "INSERT INTO company.resource_pools "
            "(pool_id, provider, level, unit, quota_amount, period_type, period_start, "
            "period_end_or_reset_at, consumed_amount, last_checked_at, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (pool_id) DO UPDATE SET "
            "provider = EXCLUDED.provider, level = EXCLUDED.level, unit = EXCLUDED.unit, "
            "quota_amount = EXCLUDED.quota_amount, period_type = EXCLUDED.period_type, "
            "period_start = EXCLUDED.period_start, period_end_or_reset_at = EXCLUDED.period_end_or_reset_at, "
            "consumed_amount = EXCLUDED.consumed_amount, last_checked_at = EXCLUDED.last_checked_at, "
            "source = EXCLUDED.source",
            (pool_id, provider, level, unit, quota_amount, period_type,
             period_start, period_end_or_reset_at, consumed_amount, now(), source),
        )

    def update_consumed(self, pool_id: str, consumed_amount, source: str) -> None:
        cur = self.conn.execute(
            "UPDATE company.resource_pools SET consumed_amount = %s, last_checked_at = %s, source = %s "
            "WHERE pool_id = %s",
            (consumed_amount, now(), source, pool_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no resource pool with pool_id={pool_id!r}")

    def get_pool(self, pool_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM company.resource_pools WHERE pool_id = %s", (pool_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_pools(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM company.resource_pools ORDER BY provider, period_type"
        )
        return [dict(r) for r in cur.fetchall()]

    def remaining_budget_vs_time(self, pool_id: str) -> dict:
        pool = self.get_pool(pool_id)
        if pool is None:
            raise ValueError(f"no resource pool with pool_id={pool_id!r}")

        remaining_amount = pool["quota_amount"] - pool["consumed_amount"]

        if pool["quota_amount"] != 0:
            fraction_budget_remaining = float(remaining_amount / pool["quota_amount"])
        else:
            fraction_budget_remaining = None

        period_start = datetime.fromisoformat(pool["period_start"])
        period_end = datetime.fromisoformat(pool["period_end_or_reset_at"])
        total_period_seconds = (period_end - period_start).total_seconds()
        now_ = datetime.now(UTC)
        remaining_period_seconds = (period_end - now_).total_seconds()

        if total_period_seconds != 0:
            fraction_time_remaining = remaining_period_seconds / total_period_seconds
        else:
            fraction_time_remaining = None

        # Budget running out meaningfully faster than time is passing means throttle.
        # 0.15 threshold: if budget fraction is more than 15 points below time fraction, it's thin.
        is_thin = (
            fraction_budget_remaining is not None
            and fraction_time_remaining is not None
            and fraction_budget_remaining < fraction_time_remaining - 0.15
        )

        return {
            "pool_id": pool_id,
            "remaining_amount": remaining_amount,
            "fraction_budget_remaining": fraction_budget_remaining,
            "fraction_time_remaining": fraction_time_remaining,
            "is_thin": is_thin,
        }
