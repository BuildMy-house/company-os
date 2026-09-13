from __future__ import annotations

import os
import unittest

import psycopg

from company_ops.observer import ObserverWriter, VALID_TABLES, now

OBSERVER_DSN = os.environ.get("TEST_OBSERVER_DATABASE_URL")
SUPERUSER_DSN = "postgresql://postgres:localtestpw@localhost:5544/homely_company"


def _cleanup():
    """Delete all rows from observer tables (superuser only)."""
    conn = psycopg.connect(SUPERUSER_DSN, autocommit=True)
    for table in VALID_TABLES:
        conn.execute(f"DELETE FROM observer.{table}")
    conn.close()


@unittest.skipUnless(OBSERVER_DSN, "TEST_OBSERVER_DATABASE_URL not set")
class TestObserverWriter(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.writer = ObserverWriter(OBSERVER_DSN)

    def tearDown(self):
        self.writer.close()
        _cleanup()

    def test_append_rejects_unknown_table(self):
        with self.assertRaises(ValueError, msg="must reject unknown table"):
            self.writer.append("nonexistent_table", foo="bar")

    def test_append_auto_generates_id_and_created_at(self):
        rid = self.writer.append("decisions", problem="test problem")
        self.assertTrue(rid.startswith("DECI-"))
        cur = self.writer.conn.execute(
            "SELECT id, created_at, problem FROM observer.decisions WHERE id = %s",
            (rid,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["problem"], "test problem")
        self.assertIsNotNone(row["created_at"])

    def test_append_respects_explicit_id(self):
        rid = self.writer.append("decisions", id="CUSTOM-123", problem="x")
        self.assertEqual(rid, "CUSTOM-123")

    def test_record_and_complete_human_request(self):
        original_id = self.writer.record_human_request(
            type="ask_information",
            question="What is the repo layout?",
            reason="need context",
            importance="high",
            blocking=True,
            related_ids="DECI-001",
            initiated_by="hermes",
        )
        self.assertTrue(original_id.startswith("HUMA-"))

        completion_id = self.writer.complete_human_request(
            original_id=original_id,
            outcome="The repo has a monorepo layout",
            human_minutes=5.0,
            avoidable=False,
            initiated_by="grace",
        )
        self.assertTrue(completion_id.startswith("HUMA-"))
        self.assertNotEqual(completion_id, original_id)

        cur = self.writer.conn.execute(
            "SELECT references_id, outcome, completed_at, type, question "
            "FROM observer.human_requests WHERE id = %s",
            (completion_id,),
        )
        comp = cur.fetchone()
        self.assertIsNotNone(comp)
        self.assertEqual(comp["references_id"], original_id)
        self.assertEqual(comp["outcome"], "The repo has a monorepo layout")
        self.assertIsNotNone(comp["completed_at"])

        cur2 = self.writer.conn.execute(
            "SELECT outcome, completed_at FROM observer.human_requests WHERE id = %s",
            (original_id,),
        )
        orig = cur2.fetchone()
        self.assertIsNotNone(orig)
        self.assertIsNone(orig["outcome"])
        self.assertIsNone(orig["completed_at"])

    def test_complete_nonexistent_request_raises(self):
        with self.assertRaises(ValueError, msg="must raise for missing id"):
            self.writer.complete_human_request(
                original_id="NOPE-999", outcome="none"
            )

    def test_find_prior_answer_returns_none_before_completion(self):
        self.writer.record_human_request(
            type="ask_information",
            question="What is the schema?",
        )
        result = self.writer.find_prior_answer("What is the schema?")
        self.assertIsNone(result)

    def test_find_prior_answer_returns_outcome_after_completion(self):
        original_id = self.writer.record_human_request(
            type="ask_information",
            question="What is the schema?",
        )
        self.writer.complete_human_request(
            original_id=original_id,
            outcome="It has 10 tables in observer schema",
        )
        result = self.writer.find_prior_answer("What is the schema?")
        self.assertIsNotNone(result)
        self.assertEqual(result["question"], "What is the schema?")
        self.assertEqual(result["outcome"], "It has 10 tables in observer schema")
        self.assertEqual(result["original_id"], original_id)
        self.assertIsNotNone(result["completed_at"])

    def test_find_prior_answer_normalizes_whitespace_and_case(self):
        original_id = self.writer.record_human_request(
            type="ask_information",
            question="  What IS the schema?  ",
        )
        self.writer.complete_human_request(
            original_id=original_id,
            outcome="Answer",
        )
        result = self.writer.find_prior_answer("  what is the schema?  ")
        self.assertIsNotNone(result)
        self.assertEqual(result["outcome"], "Answer")

    def test_find_prior_answer_ignores_non_ask_information_type(self):
        original_id = self.writer.record_human_request(
            type="bug_report",
            question="What is the schema?",
        )
        self.writer.complete_human_request(
            original_id=original_id,
            outcome="Not applicable",
        )
        result = self.writer.find_prior_answer("What is the schema?")
        self.assertIsNone(result)

    def test_record_decision_round_trips_fields(self):
        rid = self.writer.record_decision(
            problem="Which DB?",
            decision="Postgres",
            evidence_refs="doc-1, doc-2",
            reasoning_summary="ops knows it",
            alternatives_considered="SQLite, Mongo",
            confidence="high",
            expected_outcome="faster restores",
            initiated_by="hermes",
        )
        self.assertTrue(rid.startswith("DECI-"))
        cur = self.writer.conn.execute(
            "SELECT * FROM observer.decisions WHERE id = %s",
            (rid,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["problem"], "Which DB?")
        self.assertEqual(row["decision"], "Postgres")
        self.assertEqual(row["evidence_refs"], "doc-1, doc-2")
        self.assertEqual(row["reasoning_summary"], "ops knows it")
        self.assertEqual(row["alternatives_considered"], "SQLite, Mongo")
        self.assertEqual(row["confidence"], "high")
        self.assertEqual(row["expected_outcome"], "faster restores")
        self.assertEqual(row["initiated_by"], "hermes")

    def test_record_prediction_then_evaluate_creates_second_row(self):
        decision_id = self.writer.record_decision(
            problem="p", decision="d",
        )
        pred_id = self.writer.record_prediction(
            decision_id=decision_id,
            metric="restore_time",
            target_value="under 5m",
            confidence="high",
            evaluation_date="2026-10-01",
        )
        self.assertTrue(pred_id.startswith("PRED-"))

        new_id = self.writer.evaluate_prediction(
            prediction_id=pred_id,
            actual_value="4m",
            outcome="correct",
        )
        self.assertNotEqual(new_id, pred_id)

        # Original row untouched, still has no actual_value.
        cur = self.writer.conn.execute(
            "SELECT actual_value, outcome FROM observer.predictions WHERE id = %s",
            (pred_id,),
        )
        orig = cur.fetchone()
        self.assertIsNotNone(orig)
        self.assertIsNone(orig["actual_value"])
        self.assertIsNone(orig["outcome"])

        # New row carries same fields plus actual_value/outcome.
        cur = self.writer.conn.execute(
            "SELECT decision_id, metric, target_value, confidence, "
            "evaluation_date, actual_value, outcome "
            "FROM observer.predictions WHERE id = %s",
            (new_id,),
        )
        ev = cur.fetchone()
        self.assertIsNotNone(ev)
        self.assertEqual(ev["decision_id"], decision_id)
        self.assertEqual(ev["metric"], "restore_time")
        self.assertEqual(ev["target_value"], "under 5m")
        self.assertEqual(ev["confidence"], "high")
        self.assertEqual(ev["evaluation_date"], "2026-10-01")
        self.assertEqual(ev["actual_value"], "4m")
        self.assertEqual(ev["outcome"], "correct")

        # Relationship row links new row to original.
        cur = self.writer.conn.execute(
            "SELECT from_id, to_id, relation_type FROM observer.relationships "
            "WHERE from_id = %s AND to_id = %s",
            (new_id, pred_id),
        )
        rel = cur.fetchone()
        self.assertIsNotNone(rel)
        self.assertEqual(rel["relation_type"], "evaluates")

    def test_evaluate_nonexistent_prediction_raises(self):
        with self.assertRaises(ValueError, msg="must raise for missing id"):
            self.writer.evaluate_prediction(
                prediction_id="NOPE-999", actual_value="x", outcome="y"
            )

    def test_record_experiment_then_decide_creates_second_row(self):
        exp_id = self.writer.record_experiment(
            name="backup window",
            hypothesis="nightly 03:00 is quietest",
        )
        self.assertTrue(exp_id.startswith("EXPE-"))

        new_id = self.writer.decide_experiment(
            experiment_id=exp_id, decision="KEEP",
        )
        self.assertNotEqual(new_id, exp_id)

        # Original row untouched, still 'started'.
        cur = self.writer.conn.execute(
            "SELECT status, decided_at, decision FROM observer.experiments WHERE id = %s",
            (exp_id,),
        )
        orig = cur.fetchone()
        self.assertIsNotNone(orig)
        self.assertEqual(orig["status"], "started")
        self.assertIsNone(orig["decided_at"])
        self.assertIsNone(orig["decision"])

        # New row: same name/hypothesis, decided.
        cur = self.writer.conn.execute(
            "SELECT name, hypothesis, status, started_at, decided_at, decision "
            "FROM observer.experiments WHERE id = %s",
            (new_id,),
        )
        dec = cur.fetchone()
        self.assertIsNotNone(dec)
        self.assertEqual(dec["name"], "backup window")
        self.assertEqual(dec["hypothesis"], "nightly 03:00 is quietest")
        self.assertEqual(dec["status"], "decided")
        self.assertIsNotNone(dec["started_at"])
        self.assertIsNotNone(dec["decided_at"])
        self.assertEqual(dec["decision"], "KEEP")

        cur = self.writer.conn.execute(
            "SELECT from_id, to_id, relation_type FROM observer.relationships "
            "WHERE from_id = %s AND to_id = %s",
            (new_id, exp_id),
        )
        rel = cur.fetchone()
        self.assertIsNotNone(rel)
        self.assertEqual(rel["relation_type"], "tests")

    def test_decide_nonexistent_experiment_raises(self):
        with self.assertRaises(ValueError, msg="must raise for missing id"):
            self.writer.decide_experiment(experiment_id="NOPE-999", decision="KILL")

    def test_add_relationship_appends_row(self):
        a = self.writer.record_decision(problem="a", decision="a2")
        b = self.writer.record_decision(problem="b", decision="b2")
        rid = self.writer.add_relationship(a, b, "corrects")
        self.assertTrue(rid.startswith("RELA-"))
        cur = self.writer.conn.execute(
            "SELECT from_id, to_id, relation_type FROM observer.relationships WHERE id = %s",
            (rid,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["from_id"], a)
        self.assertEqual(row["to_id"], b)
        self.assertEqual(row["relation_type"], "corrects")

    def test_record_failure_round_trips_fields(self):
        rid = self.writer.record_failure(
            description="DB connection dropped",
            detected_by="monitoring",
            severity="critical",
            related_ids="DECI-001",
        )
        self.assertTrue(rid.startswith("FAIL-"))
        cur = self.writer.conn.execute(
            "SELECT * FROM observer.failures WHERE id = %s",
            (rid,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["description"], "DB connection dropped")
        self.assertEqual(row["detected_by"], "monitoring")
        self.assertEqual(row["severity"], "critical")
        self.assertEqual(row["related_ids"], "DECI-001")

    def test_record_recovery_round_trips_fields(self):
        failure_id = self.writer.record_failure(
            description="DB connection dropped",
        )
        rid = self.writer.record_recovery(
            failure_id=failure_id,
            description="Reconnected after retry",
            recovered_by="hermes",
        )
        self.assertTrue(rid.startswith("RECO-"))
        cur = self.writer.conn.execute(
            "SELECT * FROM observer.recoveries WHERE id = %s",
            (rid,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["failure_id"], failure_id)
        self.assertEqual(row["description"], "Reconnected after retry")
        self.assertEqual(row["recovered_by"], "hermes")

    def test_record_autonomy_event_round_trips_fields(self):
        rid = self.writer.record_autonomy_event(
            dimension="decision",
            event_type="auto_escalate",
            initiated_by="hermes",
            related_ids="DECI-001",
        )
        self.assertTrue(rid.startswith("AUTO-"))
        cur = self.writer.conn.execute(
            "SELECT * FROM observer.autonomy_events WHERE id = %s",
            (rid,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["dimension"], "decision")
        self.assertEqual(row["event_type"], "auto_escalate")
        self.assertEqual(row["initiated_by"], "hermes")
        self.assertEqual(row["related_ids"], "DECI-001")

    def test_record_prediction_initiated_by_round_trips(self):
        decision_id = self.writer.record_decision(
            problem="p", decision="d",
        )
        pred_id = self.writer.record_prediction(
            decision_id=decision_id,
            metric="restore_time",
            target_value="under 5m",
            confidence="high",
            evaluation_date="2026-10-01",
            initiated_by="grace",
        )
        cur = self.writer.conn.execute(
            "SELECT initiated_by FROM observer.predictions WHERE id = %s",
            (pred_id,),
        )
        row = cur.fetchone()
        self.assertEqual(row["initiated_by"], "grace")

    def test_record_experiment_initiated_by_round_trips(self):
        exp_id = self.writer.record_experiment(
            name="backup window",
            hypothesis="nightly 03:00 is quietest",
            initiated_by="grace",
        )
        cur = self.writer.conn.execute(
            "SELECT initiated_by FROM observer.experiments WHERE id = %s",
            (exp_id,),
        )
        row = cur.fetchone()
        self.assertEqual(row["initiated_by"], "grace")

    def test_complete_human_request_appends_human_minutes_row(self):
        original_id = self.writer.record_human_request(
            type="ask_information",
            question="What is the schema?",
        )
        self.writer.complete_human_request(
            original_id=original_id,
            outcome="It has 10 tables",
            human_minutes=12.5,
        )
        cur = self.writer.conn.execute(
            "SELECT request_id, minutes, activity FROM observer.human_minutes "
            "WHERE request_id = %s",
            (original_id,),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["request_id"], original_id)
        self.assertEqual(float(row["minutes"]), 12.5)
        self.assertEqual(row["activity"], "ask_information")


@unittest.skipUnless(OBSERVER_DSN, "TEST_OBSERVER_DATABASE_URL not set")
class TestObserverPermissions(unittest.TestCase):
    def test_update_raises_permission_denied(self):
        conn = psycopg.connect(OBSERVER_DSN, autocommit=True)
        with self.assertRaises(psycopg.Error):
            conn.execute("UPDATE observer.decisions SET problem='x'")
        conn.close()

    def test_delete_raises_permission_denied(self):
        conn = psycopg.connect(OBSERVER_DSN, autocommit=True)
        with self.assertRaises(psycopg.Error):
            conn.execute("DELETE FROM observer.decisions")
        conn.close()


if __name__ == "__main__":
    unittest.main()
