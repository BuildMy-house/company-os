from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row


def now() -> str:
    return datetime.now(UTC).isoformat()


class ResourcePool:
    def __init__(self, dsn: str | None = None) -> None:
        if not dsn:
            raise ValueError("No DSN provided")
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self.conn.execute("SET search_path TO company, public")

    def close(self) -> None:
        self.conn.close()

    def add(self, tool: str, tier: str, period: str, limit_value: int, reset_at: str) -> str:
        pool_id = f"POOL-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO company.resource_pools (id, tool, tier, period, limit_value, used, reset_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, 0, %s, %s)",
            (pool_id, tool, tier, period, limit_value, reset_at, now()),
        )
        return pool_id

    def debit(self, pool_id: str, amount: int = 1) -> dict:
        cur = self.conn.execute(
            "UPDATE company.resource_pools SET used = used + %s WHERE id = %s RETURNING *",
            (amount, pool_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No pool found with id {pool_id!r}")
        return dict(row)

    def list_pools(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, tool, tier, period, limit_value, used, reset_at FROM company.resource_pools ORDER BY created_at"
        )
        return [dict(r) for r in cur.fetchall()]

    def reset(self, pool_id: str) -> dict:
        cur = self.conn.execute(
            "UPDATE company.resource_pools SET used = 0, reset_at = %s WHERE id = %s RETURNING *",
            (now(), pool_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No pool found with id {pool_id!r}")
        return dict(row)
