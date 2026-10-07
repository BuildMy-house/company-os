"""Tests for the pm-agent chat service (pm-agent/agent.py): mocked Postgres
(FakeStore duck-typing PmStore's five methods) + stubbed Hive (patched
pm_agent.file_feedback_work). Follows the repo's stdlib-unittest convention.

Also includes a runnable no-forbidden-imports check over pm-agent/*.py.
"""

import ast
import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

PM_AGENT_DIR = Path(__file__).resolve().parent.parent / "pm-agent"


def _load_agent():
    # pm-agent/ has a dash, so it is not importable as a package.
    spec = importlib.util.spec_from_file_location(
        "pm_agent", PM_AGENT_DIR / "agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["pm_agent"] = module
    spec.loader.exec_module(module)
    return module


pm_agent = _load_agent()


class FakeStore:
    """Duck-types PmStore's five methods against in-memory dicts."""

    def __init__(self, signals=None):
        self.conversations = {}
        self.messages = []
        self.signals = dict(signals or {})  # normalized_topic -> signal dict
        self.filed = []  # (signal_id, hive_work_id)

    def get_or_create_conversation(self, app_user_id):
        return self.conversations.setdefault(
            app_user_id, f"conv-{len(self.conversations) + 1}"
        )

    def record_message(self, conversation_id, app_user_id, role, content):
        self.messages.append((conversation_id, app_user_id, role, content))

    def find_similar_signal(self, normalized_topic):
        return self.signals.get(normalized_topic.strip().lower())

    def upsert_signal(self, topic, quote):
        key = topic.strip().lower()
        signal = self.signals.get(key)
        if signal is None:
            signal = {
                "id": f"sig-{len(self.signals) + 1}",
                "topic": topic,
                "normalized_topic": key,
                "occurrence_count": 1,
                "status": "new",
            }
            self.signals[key] = signal
        else:
            signal["occurrence_count"] += 1
        return signal["id"]

    def mark_signal_filed(self, signal_id, hive_work_id):
        for signal in self.signals.values():
            if signal["id"] == signal_id:
                signal["status"] = "filed"
        self.filed.append((signal_id, hive_work_id))
        return signal_id


class ServiceHarness:
    def __init__(self, store):
        self.store = store
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), pm_agent.make_handler(store, "http://hive.invalid")
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post_chat(self, payload):
        req = urllib.request.Request(
            self.base + "/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ChatServiceTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.harness = ServiceHarness(self.store)

    def tearDown(self):
        self.harness.close()

    def _chat(self, message, user="user-1"):
        with patch(
            "pm_agent.file_feedback_work",
            return_value={"work_id": "hive-123"},
        ) as mock_file:
            status, body = self.harness.post_chat(
                {"app_user_id": user, "message": message}
            )
        return status, body, mock_file

    def test_healthz(self):
        status, body = self.harness.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_unknown_path_404(self):
        status, _ = self.harness.get("/nope")
        self.assertEqual(status, 404)

    def test_new_message_files_hive_and_marks_filed(self):
        status, body, mock_file = self._chat("Please add a dark mode")
        self.assertEqual(status, 200)
        self.assertIn("filed", body["reply"])
        mock_file.assert_called_once()
        self.assertEqual(len(self.store.filed), 1)
        self.assertEqual(self.store.filed[0][1], "hive-123")
        self.assertEqual(
            self.store.signals["please add a dark mode"]["status"], "filed"
        )
        self.assertEqual([m[2] for m in self.store.messages], ["user", "assistant"])

    def test_known_signal_below_threshold_not_refiled(self):
        self.store.signals["dark mode"] = {
            "id": "sig-1",
            "topic": "dark mode",
            "normalized_topic": "dark mode",
            "occurrence_count": 2,
            "status": "filed",
        }
        status, body, mock_file = self._chat("DARK MODE")
        self.assertEqual(status, 200)
        self.assertIn("Already known", body["reply"])
        self.assertIn("filed", body["reply"])  # status is reported
        mock_file.assert_not_called()
        self.assertEqual(self.store.filed, [])
        # The occurrence was still recorded, so escalation stays reachable.
        self.assertEqual(self.store.signals["dark mode"]["occurrence_count"], 3)
        self.assertEqual([m[2] for m in self.store.messages], ["user", "assistant"])

    def test_threshold_crossing_escalates(self):
        topic = "export to pdf"
        self.store.signals[topic] = {
            "id": "sig-9",
            "topic": topic,
            "normalized_topic": topic,
            "occurrence_count": pm_agent.ESCALATION_THRESHOLD,
            "status": "new",
        }
        status, body, mock_file = self._chat("Export to PDF")
        self.assertEqual(status, 200)
        self.assertIn("escalated", body["reply"])
        mock_file.assert_called_once()
        self.assertEqual(len(self.store.filed), 1)
        self.assertEqual(self.store.filed[0], ("sig-9", "hive-123"))

    def test_hive_failure_still_records_signal(self):
        with patch(
            "pm_agent.file_feedback_work",
            side_effect=pm_agent.HiveClientError("down"),
        ):
            status, body = self.harness.post_chat(
                {"app_user_id": "user-2", "message": "Crash on save"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], pm_agent._reply_recorded())
        self.assertEqual(len(self.store.signals), 1)  # occurrence recorded
        self.assertEqual(self.store.filed, [])

    def test_missing_fields_rejected(self):
        status, _ = self.harness.post_chat({"app_user_id": "user-1"})
        self.assertEqual(status, 400)
        status, _ = self.harness.post_chat({"message": "hi"})
        self.assertEqual(status, 400)

    def test_invalid_json_rejected(self):
        req = urllib.request.Request(
            self.harness.base + "/chat", data=b"not json"
        )
        try:
            urllib.request.urlopen(req)
            status = 200
        except urllib.error.HTTPError as err:
            status = err.code
        self.assertEqual(status, 400)


class HermesStubTest(unittest.TestCase):
    def test_ask_hermes_bounded_question_raises(self):
        with self.assertRaises(NotImplementedError):
            pm_agent.ask_hermes_bounded_question("status of dark mode?")


class NoForbiddenImportsTest(unittest.TestCase):
    FORBIDDEN_PREFIXES = (
        "company_ops.human_interface",
        "hermes_build_dispatcher",
        "company_ops.container_manager",
        "company_ops.mcp_client",
    )

    def test_pm_agent_modules_have_no_forbidden_imports(self):
        py_files = sorted(PM_AGENT_DIR.glob("*.py"))
        self.assertTrue(py_files)
        for path in py_files:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self._assert_allowed(alias.name, path)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        self._assert_allowed(node.module, path)

    def _assert_allowed(self, module, path):
        for prefix in self.FORBIDDEN_PREFIXES:
            self.assertFalse(
                module == prefix or module.startswith(prefix + "."),
                f"{path} imports forbidden module {module}",
            )


if __name__ == "__main__":
    unittest.main()
