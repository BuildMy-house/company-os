from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

VALID_TABLES = frozenset({
    "decisions", "predictions", "experiments", "human_requests",
    "human_discoveries", "failures", "recoveries", "autonomy_events",
    "human_minutes", "relationships",
})


def now() -> str:
    return datetime.now(UTC).isoformat()


class ObserverWriter:
    def __init__(self, dsn: str | None = None) -> None:
        if dsn is None:
            dsn = os.environ.get("OBSERVER_DATABASE_URL")
        if not dsn:
            raise ValueError(
                "No DSN provided and OBSERVER_DATABASE_URL not set"
            )
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def close(self) -> None:
        self.conn.close()

    def append(self, table: str, **fields: object) -> str:
        if table not in VALID_TABLES:
            raise ValueError(
                f"Invalid table {table!r}; must be one of: "
                f"{', '.join(sorted(VALID_TABLES))}"
            )
        if "id" not in fields:
            fields["id"] = f"{table.upper()[:4]}-{uuid.uuid4().hex[:12]}"
        if "created_at" not in fields:
            fields["created_at"] = now()

        cols = list(fields.keys())
        stmt = sql.SQL("INSERT INTO observer.{table} ({cols}) VALUES ({vals})").format(
            table=sql.Identifier(table),
            cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
            vals=sql.SQL(", ").join(sql.Placeholder() * len(cols)),
        )
        self.conn.execute(stmt, [fields[c] for c in cols])
        return str(fields["id"])

    def record_human_request(
        self,
        type: str,
        question: str,
        reason: str | None = None,
        importance: str | None = None,
        blocking: bool = False,
        related_ids: str | None = None,
        initiated_by: str | None = None,
    ) -> str:
        return self.append(
            "human_requests",
            type=type,
            question=question,
            reason=reason,
            importance=importance,
            blocking=blocking,
            related_ids=related_ids,
            initiated_by=initiated_by,
        )

    def complete_human_request(
        self,
        original_id: str,
        outcome: str,
        human_minutes: float | None = None,
        avoidable: bool | None = None,
        initiated_by: str | None = None,
    ) -> str:
        cur = self.conn.execute(
            "SELECT type, question, reason, importance, blocking, related_ids "
            "FROM observer.human_requests WHERE id = %s",
            (original_id,),
        )
        original = cur.fetchone()
        if original is None:
            raise ValueError(
                f"No human_request found with id {original_id!r}"
            )
        return self.append(
            "human_requests",
            type=original["type"],
            question=original["question"],
            reason=original["reason"],
            importance=original["importance"],
            blocking=original["blocking"],
            related_ids=original["related_ids"],
            references_id=original_id,
            completed_at=now(),
            outcome=outcome,
            human_minutes=human_minutes,
            avoidable=avoidable,
            initiated_by=initiated_by,
        )

    def find_prior_answer(self, question: str) -> dict | None:
        normalized = question.strip().lower()
        cur = self.conn.execute(
            "SELECT hr.id, hr.question, hr.type, hr.created_at, "
            "comp.id AS comp_id, comp.outcome, comp.completed_at "
            "FROM observer.human_requests hr "
            "JOIN observer.human_requests comp "
            "  ON comp.references_id = hr.id AND comp.outcome IS NOT NULL "
            "WHERE hr.type = 'ask_information' "
            "  AND lower(trim(hr.question)) = %s "
            "ORDER BY comp.completed_at DESC "
            "LIMIT 1",
            (normalized,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "question": row["question"],
            "outcome": row["outcome"],
            "original_id": row["id"],
            "completed_at": row["completed_at"],
        }
