"""Hermes axiom_usage plugin: startup/heartbeat liveness events (no network)."""

import importlib.util
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "hermes-plugins" / "axiom_usage" / "__init__.py"


def _load():
    spec = importlib.util.spec_from_file_location("axiom_usage_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Ctx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, fn):
        self.hooks.append(name)


class LivenessTests(unittest.TestCase):
    def test_register_emits_startup_with_shared_tags(self):
        mod = _load()
        mod._started = True  # don't spawn flush/heartbeat threads in a unit test
        ctx = _Ctx()
        mod.register(ctx)
        self.assertIn("post_llm_call", ctx.hooks)
        event = mod._queue.get_nowait()
        self.assertEqual(event["event"], "startup")
        self.assertEqual(event["role"], "hermes")
        self.assertIn("service", event)
        self.assertIn("environment", event)

    def test_heartbeat_carries_uptime_only(self):
        mod = _load()
        mod._HEARTBEAT_INTERVAL = 0
        calls = []
        mod._push = lambda ev: (calls.append(ev), (_ for _ in ()).throw(StopIteration))[0]
        with self.assertRaises(StopIteration):
            mod._heartbeat_loop()
        self.assertEqual(calls[0]["event"], "heartbeat")
        self.assertEqual(set(calls[0]), {"event", "uptime_s"})


if __name__ == "__main__":
    unittest.main()
