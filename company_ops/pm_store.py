from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row


def now() -> str:
    return datetime.now(UTC).isoformat()


class PmStore:
    def __init__(self, dsn: str | None = None) -> None:
        if dsn is None:
            dsn = os.environ.get("PM_DATABASE_URL")
        if not dsn:
            raise ValueError(
                "No DSN provided and PM_DATABASE_URL not set"
            )
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def close(self) -> None:
        self.conn.close()

    def get_or_create_conversation(self, app_user_id: str) -> str:
        cur = self.conn.execute(
            "SELECT id FROM company.pm_conversations "
            "WHERE app_user_id = %s "
            "ORDER BY started_at DESC "
            "LIMIT 1",
            (app_user_id,),
        )
        row = cur.fetchone()
        if row is not None:
            return str(row["id"])
        cur = self.conn.execute(
            "INSERT INTO company.pm_conversations (app_user_id) "
            "VALUES (%s) "
            "RETURNING id",
            (app_user_id,),
        )
        return str(cur.fetchone()["id"])

    def record_message(
        self,
        conversation_id: str,
        app_user_id: str,
        role: str,
        content: str,
    ) -> str:
        message = json.dumps({"role": role, "content": content, "at": now()})
        cur = self.conn.execute(
            "UPDATE company.pm_conversations "
            "SET messages = messages || %s::jsonb, "
            "    last_message_at = now() "
            "WHERE id = %s AND app_user_id = %s "
            "RETURNING id",
            (message, conversation_id, app_user_id),
        )
        if cur.fetchone() is None:
            raise ValueError(
                f"No conversation {conversation_id!r} for app_user "
                f"{app_user_id!r}"
            )
        return conversation_id

    def find_similar_signal(self, normalized_topic: str) -> dict[str, object] | None:
        cur = self.conn.execute(
            "SELECT * FROM company.feedback_signals "
            "WHERE normalized_topic = %s "
            "ORDER BY last_seen_at DESC "
            "LIMIT 1",
            (normalized_topic.strip().lower(),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def upsert_signal(self, topic: str, quote: str) -> str:
        normalized = topic.strip().lower()
        quote_json = json.dumps(quote)
        existing = self.find_similar_signal(normalized)
        if existing is not None:
            cur = self.conn.execute(
                "UPDATE company.feedback_signals "
                "SET occurrence_count = occurrence_count + 1, "
                "    example_quotes = example_quotes || %s::jsonb, "
                "    last_seen_at = now() "
                "WHERE id = %s "
                "RETURNING id",
                (quote_json, existing["id"]),
            )
            return str(cur.fetchone()["id"])
        cur = self.conn.execute(
            "INSERT INTO company.feedback_signals "
            "    (topic, normalized_topic, example_quotes) "
            "VALUES (%s, %s, %s::jsonb) "
            "RETURNING id",
            (topic, normalized, json.dumps([quote])),
        )
        return str(cur.fetchone()["id"])

    def mark_signal_filed(self, signal_id: str, hive_work_id: str) -> str:
        cur = self.conn.execute(
            "UPDATE company.feedback_signals "
            "SET status = 'filed', linked_hive_work_id = %s "
            "WHERE id = %s "
            "RETURNING id",
            (hive_work_id, signal_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No feedback signal found with id {signal_id!r}")
        return str(row["id"])
