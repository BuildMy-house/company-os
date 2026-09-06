from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from company_ops.ledger import Ledger
from company_ops.policy import ACTION_COSTS, DAILY_ALLOWANCE

_SUPER_DSN = os.environ.get(
    "TEST_SUPER_DATABASE_URL",
    "postgresql://postgres:localtestpw@localhost:5544/homely_company",
)
_COMPANY_DSN = os.environ.get(
    "TEST_COMPANY_DATABASE_URL",
    "postgresql://hermes_company:localtest_company@localhost:5544/homely_company",
)
_TABLES = [
    "snapshots", "routing_lessons", "provider_usage", "actions",
    "credit_transactions", "revenue_events", "tool_requests",
    "deployments", "plans",
]


@pytest.fixture
def ledger():
    with psycopg.connect(_SUPER_DSN) as conn:
        conn.execute("SET search_path TO company, public")
        for t in _TABLES:
            conn.execute(f"TRUNCATE {t} CASCADE")
        conn.commit()
    l = Ledger(_COMPANY_DSN)
    l.init()
    yield l
    l.close()
    with psycopg.connect(_SUPER_DSN) as conn:
        conn.execute("SET search_path TO company, public")
        for t in _TABLES:
            conn.execute(f"TRUNCATE {t} CASCADE")
        conn.commit()


class TestLedgerInit:
    def test_init_creates_daily_allowance(self, ledger):
        assert ledger.balance() == DAILY_ALLOWANCE

    def test_init_idempotent(self, ledger):
        ledger.init()
        assert ledger.balance() == DAILY_ALLOWANCE


class TestBalance:
    def test_balance_starts_at_daily_allowance(self, ledger):
        assert ledger.balance() == DAILY_ALLOWANCE

    def test_balance_after_credit(self, ledger):
        ledger._credit("bonus", 50, "test")
        ledger.db.commit()
        assert ledger.balance() == DAILY_ALLOWANCE + 50


class TestPlans:
    def test_create_plan(self, ledger):
        pid = ledger.create_plan("build website")
        assert pid.startswith("PLAN-")
        status = ledger.status()
        assert status["plans"] == 1

    def test_create_plan_explicit_id(self, ledger):
        pid = ledger.create_plan("test", plan_id="PLAN-custom")
        assert pid == "PLAN-custom"


class TestCharge:
    def test_charge_deducts_credits(self, ledger):
        plan = ledger.create_plan("seo work")
        result = ledger.charge("hermes", "seo", plan)
        assert result["agent"] == "hermes"
        assert result["action_type"] == "seo"
        assert result["credits"] == ACTION_COSTS["seo"]
        assert ledger.balance() == DAILY_ALLOWANCE - ACTION_COSTS["seo"]

    def test_charge_idempotent(self, ledger):
        plan = ledger.create_plan("idempotent test")
        key = str(uuid.uuid4())
        first = ledger.charge("hermes", "seo", plan, idempotency_key=key)
        second = ledger.charge("hermes", "seo", plan, idempotency_key=key)
        assert first["id"] == second["id"]
        assert ledger.balance() == DAILY_ALLOWANCE - ACTION_COSTS["seo"]

    def test_charge_insufficient_credits(self, ledger):
        plan = ledger.create_plan("expensive test")
        for _ in range(DAILY_ALLOWANCE // ACTION_COSTS["coding"]):
            ledger.charge("hermes", "coding", plan)
        assert ledger.balance() == 0
        with pytest.raises(ValueError, match="insufficient credits"):
            ledger.charge("hermes", "coding", plan)


class TestRevenue:
    def test_record_revenue(self, ledger):
        result = ledger.record_revenue(1000, "subscription")
        assert result["net_revenue_cents"] == 1000
        assert result["credits_created"] == 7
        assert ledger.balance() == DAILY_ALLOWANCE + 7


class TestRoutingLessons:
    def test_add_and_avoid(self, ledger):
        lid = ledger.add_routing_lesson("gpt-4", "seo", "too expensive")
        assert lid.startswith("LESSON-")
        assert ledger.avoided_models("seo") == {"gpt-4"}

    def test_avoided_models_empty(self, ledger):
        assert ledger.avoided_models("nonexistent") == set()


class TestProviderUsage:
    def test_record_usage(self, ledger):
        plan = ledger.create_plan("test")
        action = ledger.charge("hermes", "seo", plan)
        uid = ledger.record_provider_usage(action["id"], "opencode", "mimo-v2.5-free")
        assert uid.startswith("USAGE-")


class TestToolRequest:
    def test_add_request(self, ledger):
        rid = ledger.add_tool_request("hermes", "github", "code hosting", 20)
        assert rid.startswith("REQ-")


class TestDeployment:
    def test_add_deployment(self, ledger):
        did = ledger.add_deployment("staging", "0.1.0")
        assert did.startswith("DEPLOY-")


class TestSnapshot:
    def test_snapshot_captures_state(self, ledger):
        snap_id = ledger.snapshot()
        assert snap_id.startswith("SNAP-")
        snap = ledger.latest_snapshot()
        assert snap is not None
        assert snap["balance"] == DAILY_ALLOWANCE
        assert snap["plans_count"] == 0
        assert snap["actions_count"] == 0
        assert snap["revenue_cents"] == 0

    def test_snapshot_after_charge(self, ledger):
        plan = ledger.create_plan("test snap")
        ledger.charge("hermes", "seo", plan)
        snap = ledger.latest_snapshot()
        assert snap is not None
        assert snap["balance"] == DAILY_ALLOWANCE - ACTION_COSTS["seo"]
        assert snap["actions_count"] == 1

    def test_snapshot_after_revenue(self, ledger):
        ledger.record_revenue(500, "test")
        snap = ledger.latest_snapshot()
        assert snap is not None
        assert snap["revenue_cents"] == 500

    def test_latest_snapshot_none_when_empty(self, ledger):
        assert ledger.latest_snapshot() is None


class TestStatus:
    def test_status_fields(self, ledger):
        s = ledger.status()
        assert "balance" in s
        assert "plans" in s
        assert "actions" in s
        assert "revenue_cents" in s
        assert "daily_cap" in s
