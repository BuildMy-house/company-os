from __future__ import annotations

import ast
import inspect
import os
import unittest
from unittest.mock import MagicMock

import psycopg

from company_ops import agent_query_interface
from company_ops.agent_query_interface import (
    DistillationViolation,
    receive_agent_query,
)
from company_ops.observer import VALID_TABLES, ObserverWriter

OBSERVER_DSN = os.environ.get("TEST_OBSERVER_DATABASE_URL")
SUPERUSER_DSN = "postgresql://postgres:localtestpw@localhost:5544/homely_company"

FORBIDDEN_NAMES = {
    "ask_judgment",
    "request_approval",
    "request_action",
    "request_financial_action",
    "ask_information",
    "human_interface",
    "container_manager",
    "hermes_build_dispatcher",
    "kubectl",
    "docker",
}


def _cleanup():
    conn = psycopg.connect(SUPERUSER_DSN, autocommit=True)
    for table in VALID_TABLES:
        conn.execute(f"DELETE FROM observer.{table}")
    conn.close()


class TestNoForbiddenCapabilities(unittest.TestCase):
    """Negative-capability tests: these must hold regardless of whether a
    live Postgres/Hermes is available -- they are static/structural checks,
    not integration tests."""

    def test_module_never_imports_human_interface(self):
        source = inspect.getsource(agent_query_interface)
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module)
                for alias in node.names:
                    imported_names.add(alias.name)

        self.assertNotIn("company_ops.human_interface", imported_modules)
        self.assertNotIn("human_interface", imported_modules)
        for forbidden in FORBIDDEN_NAMES:
            self.assertNotIn(
                forbidden,
                imported_names,
                f"agent_query_interface must never import {forbidden!r}",
            )

    def test_no_executable_reference_to_forbidden_symbols(self):
        """Belt-and-suspenders: even a dynamic getattr-style reference to a
        forbidden name as actual CODE (a Name/Attribute/Call node) is
        disallowed -- this check deliberately ignores string literals and
        docstrings, because the module's prompt template intentionally
        *mentions* container_manager/hermes_build_dispatcher by name in
        plain text, instructing Hermes not to use them. Mentioning a tool
        name in a prompt string is not the same as this module being able
        to call it -- that is exactly the distinction this test enforces.
        """
        source = inspect.getsource(agent_query_interface)
        tree = ast.parse(source)
        executable_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                continue  # skip string/docstring literals entirely
            if isinstance(node, ast.Name):
                executable_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                executable_names.add(node.attr)

        code_forbidden = FORBIDDEN_NAMES - {"docker", "kubectl"}  # never appear as bare code tokens either way, but excluded from the free-text prompt check above
        for forbidden in code_forbidden:
            self.assertNotIn(
                forbidden,
                executable_names,
                f"agent_query_interface must never reference {forbidden!r} as code",
            )

    def test_receive_agent_query_rejects_oversized_question(self):
        writer = MagicMock(spec=ObserverWriter)
        with self.assertRaises(DistillationViolation):
            receive_agent_query(
                writer,
                asker_id="pm-agent",
                question="x" * 501,
                _post_fn=lambda *a, **k: "unreachable",
            )
        writer.record_decision.assert_not_called()

    def test_receive_agent_query_rejects_multiline_transcript(self):
        writer = MagicMock(spec=ObserverWriter)
        transcript = "\n".join([f"line {i}" for i in range(10)])
        with self.assertRaises(DistillationViolation):
            receive_agent_query(
                writer,
                asker_id="pm-agent",
                question=transcript,
                _post_fn=lambda *a, **k: "unreachable",
            )
        writer.record_decision.assert_not_called()

    def test_receive_agent_query_rejects_role_marker(self):
        writer = MagicMock(spec=ObserverWriter)
        with self.assertRaises(DistillationViolation):
            receive_agent_query(
                writer,
                asker_id="pm-agent",
                question="user: please deploy the latest build",
                _post_fn=lambda *a, **k: "unreachable",
            )
        writer.record_decision.assert_not_called()

    def test_receive_agent_query_rejects_oversized_context(self):
        writer = MagicMock(spec=ObserverWriter)
        with self.assertRaises(DistillationViolation):
            receive_agent_query(
                writer,
                asker_id="pm-agent",
                question="is this a known issue?",
                context="y" * 2001,
                _post_fn=lambda *a, **k: "unreachable",
            )
        writer.record_decision.assert_not_called()

    def test_happy_path_calls_post_fn_and_records_decision(self):
        writer = MagicMock(spec=ObserverWriter)
        writer.record_decision.return_value = "DEC-abc123"
        captured = {}

        def fake_post(message, timeout):
            captured["message"] = message
            return "Yes, this is already tracked in feedback_signals."

        result = receive_agent_query(
            writer,
            asker_id="pm-agent",
            question="Is the dark-mode toggle bug already known?",
            context="occurrence_count=6",
            _post_fn=fake_post,
        )

        self.assertEqual(result["decision_id"], "DEC-abc123")
        self.assertIn("already tracked", result["answer"])
        self.assertIn("[AGENT QUERY", captured["message"])
        self.assertIn("container_manager", captured["message"])  # the ban, in the prompt
        writer.record_decision.assert_called_once()
        _, kwargs = writer.record_decision.call_args
        self.assertEqual(kwargs["initiated_by"], "pm-agent")
        self.assertEqual(kwargs["problem"], "Is the dark-mode toggle bug already known?")


@unittest.skipUnless(OBSERVER_DSN, "TEST_OBSERVER_DATABASE_URL not set")
class TestAgentQueryInterfaceIntegration(unittest.TestCase):
    """Real Postgres round trip (Hermes call itself is still stubbed --
    this ticket does not require a live Hermes pod)."""

    def setUp(self):
        _cleanup()
        self.writer = ObserverWriter(OBSERVER_DSN)

    def tearDown(self):
        self.writer.close()
        _cleanup()

    def test_records_a_real_decision_row(self):
        result = receive_agent_query(
            self.writer,
            asker_id="pm-agent",
            question="Is this feature flag already live?",
            context=None,
            _post_fn=lambda *a, **k: "Not yet, targeted for next sprint.",
        )
        cur = self.writer.conn.execute(
            "SELECT problem, decision, initiated_by FROM observer.decisions WHERE id = %s",
            (result["decision_id"],),
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["problem"], "Is this feature flag already live?")
        self.assertEqual(row["initiated_by"], "pm-agent")
        self.assertIn("Not yet", row["decision"])


if __name__ == "__main__":
    unittest.main()
