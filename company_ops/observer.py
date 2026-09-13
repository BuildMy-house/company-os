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

    def record_decision(
        self,
        problem: str,
        decision: str,
        evidence_refs: str | None = None,
        reasoning_summary: str | None = None,
        alternatives_considered: str | None = None,
        confidence: str | None = None,
        expected_outcome: str | None = None,
        initiated_by: str | None = None,
    ) -> str:
        return self.append(
            "decisions",
            problem=problem,
            decision=decision,
            evidence_refs=evidence_refs,
            reasoning_summary=reasoning_summary,
            alternatives_considered=alternatives_considered,
            confidence=confidence,
            expected_outcome=expected_outcome,
            initiated_by=initiated_by,
        )

    def record_prediction(
        self,
        decision_id: str,
        metric: str,
        target_value: str,
        confidence: str | None = None,
        evaluation_date: str | None = None,
        initiated_by: str | None = None,
    ) -> str:
        return self.append(
            "predictions",
            decision_id=decision_id,
            metric=metric,
            target_value=target_value,
            confidence=confidence,
            evaluation_date=evaluation_date,
            initiated_by=initiated_by,
        )

    def evaluate_prediction(
        self,
        prediction_id: str,
        actual_value: str,
        outcome: str,
    ) -> str:
        cur = self.conn.execute(
            "SELECT decision_id, metric, target_value, confidence, evaluation_date, initiated_by "
            "FROM observer.predictions WHERE id = %s",
            (prediction_id,),
        )
        original = cur.fetchone()
        if original is None:
            raise ValueError(
                f"No prediction found with id {prediction_id!r}"
            )
        new_id = self.append(
            "predictions",
            decision_id=original["decision_id"],
            metric=original["metric"],
            target_value=original["target_value"],
            confidence=original["confidence"],
            evaluation_date=original["evaluation_date"],
            initiated_by=original["initiated_by"],
            actual_value=actual_value,
            outcome=outcome,
        )
        self.add_relationship(new_id, prediction_id, "evaluates")
        return new_id

    def record_experiment(
        self,
        name: str,
        hypothesis: str,
        status: str = "started",
        started_at: str | None = None,
        initiated_by: str | None = None,
    ) -> str:
        return self.append(
            "experiments",
            name=name,
            hypothesis=hypothesis,
            status=status,
            started_at=started_at if started_at is not None else now(),
            initiated_by=initiated_by,
        )

    def decide_experiment(self, experiment_id: str, decision: str) -> str:
        cur = self.conn.execute(
            "SELECT name, hypothesis, status, started_at, initiated_by "
            "FROM observer.experiments WHERE id = %s",
            (experiment_id,),
        )
        original = cur.fetchone()
        if original is None:
            raise ValueError(
                f"No experiment found with id {experiment_id!r}"
            )
        new_id = self.append(
            "experiments",
            name=original["name"],
            hypothesis=original["hypothesis"],
            status="decided",
            started_at=original["started_at"],
            decided_at=now(),
            decision=decision,
            initiated_by=original["initiated_by"],
        )
        self.add_relationship(new_id, experiment_id, "tests")
        return new_id

    def add_relationship(
        self,
        from_id: str,
        to_id: str,
        relation_type: str,
    ) -> str:
        return self.append(
            "relationships",
            from_id=from_id,
            to_id=to_id,
            relation_type=relation_type,
        )

    def record_failure(
        self,
        description: str,
        detected_by: str | None = None,
        severity: str | None = None,
        related_ids: str | None = None,
    ) -> str:
        return self.append(
            "failures",
            description=description,
            detected_by=detected_by,
            severity=severity,
            related_ids=related_ids,
        )

    def record_recovery(
        self,
        failure_id: str,
        description: str,
        recovered_by: str | None = None,
    ) -> str:
        return self.append(
            "recoveries",
            failure_id=failure_id,
            description=description,
            recovered_by=recovered_by,
        )

    def record_autonomy_event(
        self,
        dimension: str,
        event_type: str,
        initiated_by: str | None = None,
        related_ids: str | None = None,
    ) -> str:
        return self.append(
            "autonomy_events",
            dimension=dimension,
            event_type=event_type,
            initiated_by=initiated_by,
            related_ids=related_ids,
        )

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
        completion_id = self.append(
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
        if human_minutes is not None:
            self.append(
                "human_minutes",
                request_id=original_id,
                minutes=human_minutes,
                activity=original["type"],
            )
        return completion_id

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
