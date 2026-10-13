"""Tests for company_ops/model_resolver.py (via pm-agent/resolve_model.py wrapper) (mocked /models and probe; no network)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

PM_AGENT_DIR = Path(__file__).resolve().parent.parent / "pm-agent"
sys.path.insert(0, str(PM_AGENT_DIR))

import resolve_model  # noqa: E402

rm = resolve_model.shared

FUTURE = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 90 * 86400))
SOON = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 1 * 86400))


def model(mid, prompt="0", completion="0", ctx=200_000, tools=True, exp=None):
    m = {"id": mid, "pricing": {"prompt": prompt, "completion": completion},
         "context_length": ctx,
         "supported_parameters": ["tools", "tool_choice"] if tools else ["temperature"]}
    if exp:
        m["expiration_date"] = exp
    return m


PREFS = {"preferred": ["b", "a"], "exclude_substrings": ["-fin", "-sante", "stealth/"]}


class CandidateTests(unittest.TestCase):
    def test_free_filtering_and_exclusions(self):
        ms = [model("a"), model("paid", "0.1", "0.2"), model("notools", tools=False),
              model("short", ctx=8000), model("x-fin"), model("x-sante"),
              model("stealth/missingno"), model("expired", exp="2020-01-01"),
              model("soon", exp=SOON), model("later", exp=FUTURE)]
        self.assertEqual(rm.candidates(ms, PREFS), ["a", "later"])

    def test_ordering_preferred_then_context(self):
        ms = [model("c", ctx=300_000), model("d", ctx=900_000), model("a"), model("b")]
        self.assertEqual(rm.candidates(ms, PREFS), ["b", "a", "d", "c"])

    def test_paid_cheapest_first(self):
        ms = [model("free"), model("exp", "0.5", "1"), model("cheap", "0.1", "0.1")]
        self.assertEqual(rm.candidates(ms, PREFS, paid=True), ["cheap", "exp"])


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.models = [model("a"), model("b"), model("paid", "1", "1")]

    def run_resolve(self, ok, **kw):
        calls = []

        def probe(mid, key):
            calls.append(mid)
            return mid in ok

        out = rm.resolve("k", self.tmp, prefs=PREFS, fetch=lambda: self.models, probe_fn=probe, **kw)
        return out, calls

    def test_first_passing_wins_and_state_written(self):
        (mid, _), calls = self.run_resolve({"a", "b"})
        self.assertEqual((mid, calls), ("b", ["b"]))
        self.assertIn("b", (self.tmp / rm.STATE_FILE).read_text())

    def test_probe_fail_then_next(self):
        (mid, _), calls = self.run_resolve({"a"})
        self.assertEqual((mid, calls), ("a", ["b", "a"]))

    def test_all_fail_uses_last_known(self):
        rm._write_state(self.tmp, "old")
        (mid, reason), _ = self.run_resolve({"old"})
        self.assertEqual(mid, "old")
        self.assertIn("last working", reason)

    def test_no_candidates_raises_and_never_pays(self):
        with self.assertRaises(rm.NoModelError):
            self.run_resolve({"paid"})

    def test_paid_fallback_only_with_flag(self):
        (mid, reason), _ = self.run_resolve({"paid"}, allow_paid=True)
        self.assertEqual(mid, "paid")
        self.assertIn("PAID", reason)

    def test_fetch_failure_falls_back_to_state(self):
        rm._write_state(self.tmp, "old")

        def boom():
            raise OSError("down")

        mid, _ = rm.resolve("k", self.tmp, prefs=PREFS, fetch=boom, probe_fn=lambda m, k: m == "old")
        self.assertEqual(mid, "old")


class FreeAndHermesTests(unittest.TestCase):
    def test_is_free_ignores_suffix(self):
        self.assertTrue(rm.is_free(model("inclusionai/ling-3.1-flash")))
        self.assertFalse(rm.is_free(model("x:free", "0.1", "0")))

    def test_hermes_config_renders_only_model_default(self):
        src = (PM_AGENT_DIR.parent / "hermes" / "config.yaml").read_text()
        out = rm.render_config(src, "q/z")
        self.assertIn("  default: q/z\n  provider: custom:nous", out)
        self.assertIn("model: glm-5.3-flash", out)
        self.assertEqual(out.count("q/z"), 1)


class RenderTests(unittest.TestCase):
    def test_render_replaces_model_default_only(self):
        src = (PM_AGENT_DIR / "hermes-config.yaml").read_text()
        out = rm.render_config(src, "x/y:free")
        self.assertIn("  default: x/y:free", out)
        self.assertEqual(out.count("default: x/y:free"), 1)
        self.assertIn("provider: custom:pm_llm", out)
        self.assertNotIn("longcat", out)

    def test_baked_default_is_current_free_model(self):
        tmp = Path(tempfile.mkdtemp()) / "c.yaml"
        tmp.write_text(rm.render_config((PM_AGENT_DIR / "hermes-config.yaml").read_text(), "q/z"))
        self.assertEqual(rm.current_model(tmp), "q/z")
        self.assertEqual(rm.current_model(PM_AGENT_DIR / "hermes-config.yaml"), "inclusionai/ling-3.1-flash")

    def test_render_missing_line_errors(self):
        with self.assertRaises(ValueError):
            rm.render_config("model: {}\n", "x")


class ReasoningProbeTests(unittest.TestCase):
    def _probe_body(self, **kw):
        import io
        import json
        import urllib.request
        from unittest import mock

        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["body"] = json.loads(req.data)
            resp = io.BytesIO(b"{}")
            resp.status = 200
            resp.__enter__ = lambda self: self
            resp.__exit__ = lambda *a: None
            return resp

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            rm.probe("m", "k", **kw)
        return seen["body"]

    def test_probe_sends_extra_body(self):
        body = self._probe_body(extra_body=resolve_model.REASONING_EXTRA_BODY)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["max_tokens"], 1)
        self.assertIn("tags", body)

    def test_probe_without_extra_body_is_unchanged(self):
        self.assertNotIn("reasoning_effort", self._probe_body())

    def test_rejected_reasoning_effort_skips_model(self):
        # probe False (HTTP 400 on the effort) => next candidate wins.
        tmp = Path(tempfile.mkdtemp())
        models = [model("a"), model("b")]
        mid, _ = rm.resolve("k", tmp, prefs=PREFS, fetch=lambda: models, probe_fn=lambda m, k: m == "a")
        self.assertEqual(mid, "a")

    def test_wrapper_passes_reasoning_to_probe_via_env(self):
        import os
        from unittest import mock

        captured = {}
        with mock.patch.dict(os.environ, {"PM_AGENT_LLM_API_KEY": "k", "HERMES_HOME": str(Path(tempfile.mkdtemp()))}, clear=True), \
                mock.patch.object(rm, "fetch_models", lambda base: [model("a")]), \
                mock.patch.object(rm, "probe", lambda mid, k, base, tags, extra=None: captured.setdefault("extra", extra) or True), \
                mock.patch.dict(os.environ, {"RESOLVER_BAKED_CONFIG": str(PM_AGENT_DIR / "hermes-config.yaml"),
                                             "RESOLVER_STATE_DIR": tempfile.mkdtemp()}):
            self.assertEqual(resolve_model.main([]), 0)
        self.assertEqual(captured["extra"], resolve_model.REASONING_EXTRA_BODY)

    def test_hermes_config_reasoning_effort_matches_probe(self):
        import yaml

        cfg = yaml.safe_load((PM_AGENT_DIR / "hermes-config.yaml").read_text())
        self.assertEqual({"reasoning_effort": cfg["agent"]["reasoning_effort"]}, resolve_model.REASONING_EXTRA_BODY)
        self.assertIn("tags", cfg["custom_providers"][0]["extra_body"])  # Nous 400s without tags

    def test_no_conflicting_reasoning_forms(self):
        # Regression: Nous 400s ("reasoningeffort" and "reasoning.effort" both
        # provided) when the runtime's top-level reasoning_effort meets a nested
        # extra_body.reasoning.
        import yaml

        cfg = yaml.safe_load((PM_AGENT_DIR / "hermes-config.yaml").read_text())
        self.assertNotIn("reasoning", cfg["custom_providers"][0]["extra_body"])
        body = self._probe_body(extra_body=resolve_model.REASONING_EXTRA_BODY)
        self.assertFalse("reasoning" in body and "reasoning_effort" in body)
        self.assertNotIn("reasoning", body)

    def test_timeout_budget_is_nested(self):
        import yaml
        import agent  # noqa: PLC0415

        cfg = yaml.safe_load((PM_AGENT_DIR / "hermes-config.yaml").read_text())
        # app proxy (chat.ts) is 60s; front door must answer before it, and a
        # single tool call must fit inside the front door's budget.
        self.assertLess(agent.LLM_TIMEOUT_SECONDS, 60)
        self.assertLess(cfg["mcp_servers"]["pm_tools"]["timeout"], agent.LLM_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
