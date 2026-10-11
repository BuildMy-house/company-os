"""Tests for company_ops/axiom_emit.py (fail-open Axiom ingest) and the
backup CLI's telemetry wiring. No network: urlopen is patched."""

import json
import unittest
import urllib.error
from unittest import mock

from company_ops import axiom_emit, backup


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class BuildEventTests(unittest.TestCase):
    def test_envelope_has_shared_tags(self):
        with mock.patch.dict("os.environ", {"DEPLOYMENT_ENVIRONMENT": "local-k8s"}, clear=True):
            ev = axiom_emit.build_event("backup", "job", "postgres-backup", {"kind": "postgres"})
        self.assertEqual(ev["service"], "postgres-backup")
        self.assertEqual(ev["role"], "job")
        self.assertEqual(ev["environment"], "local-k8s")
        self.assertEqual(ev["event"], "backup")
        self.assertEqual(ev["kind"], "postgres")
        self.assertIn("_time", ev)

    def test_service_name_env_overrides_default(self):
        with mock.patch.dict("os.environ", {"AXIOM_SERVICE_NAME": "x-k8s"}, clear=True):
            ev = axiom_emit.build_event("e", "job", "x", {})
        self.assertEqual(ev["service"], "x-k8s")


class EmitTests(unittest.TestCase):
    def test_noop_without_token(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
            "urllib.request.urlopen"
        ) as urlopen:
            self.assertFalse(axiom_emit.emit("e", role="job", service="s"))
        urlopen.assert_not_called()

    def test_posts_to_edge_ingest_with_bearer(self):
        env = {"AXIOM_TOKEN": "tok", "AXIOM_DATASET": "ds"}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "urllib.request.urlopen", return_value=_Resp()
        ) as urlopen:
            self.assertTrue(axiom_emit.emit("e", role="job", service="s", n=1))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://eu-central-1.aws.edge.axiom.co/v1/ingest/ds")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        body = json.loads(req.data)
        self.assertEqual(body[0]["n"], 1)
        self.assertEqual(body[0]["role"], "job")

    def test_default_dataset(self):
        with mock.patch.dict("os.environ", {"AXIOM_TOKEN": "t"}, clear=True), mock.patch(
            "urllib.request.urlopen", return_value=_Resp()
        ) as urlopen:
            axiom_emit.emit("e", role="job", service="s")
        self.assertTrue(urlopen.call_args.args[0].full_url.endswith("/ingest/bmh-company"))

    def test_network_failure_is_swallowed(self):
        with mock.patch.dict("os.environ", {"AXIOM_TOKEN": "t"}, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("down")
        ):
            self.assertFalse(axiom_emit.emit("e", role="job", service="s"))


class BackupMainTelemetryTests(unittest.TestCase):
    def test_success_emits_and_returns_zero(self):
        result = {"uploaded_key": "k", "deleted_keys": ["a"], "bytes_uploaded": 42}
        with mock.patch.object(backup, "run_backup", return_value=result), mock.patch(
            "company_ops.axiom_emit.emit"
        ) as emit:
            self.assertEqual(backup.main([]), 0)
        kwargs = emit.call_args.kwargs
        self.assertEqual(emit.call_args.args[0], "backup")
        self.assertEqual(kwargs["outcome"], "success")
        self.assertEqual(kwargs["kind"], "postgres")
        self.assertEqual(kwargs["bytes_uploaded"], 42)
        self.assertEqual(kwargs["deleted_count"], 1)
        self.assertNotIn("uploaded_key", kwargs)

    def test_failure_emits_then_reraises(self):
        with mock.patch.object(backup, "run_hermes_backup", side_effect=ValueError("secret-ish")), mock.patch(
            "company_ops.axiom_emit.emit"
        ) as emit:
            with self.assertRaises(ValueError):
                backup.main(["--hermes-data", "/x"])
        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["outcome"], "failure")
        self.assertEqual(kwargs["kind"], "hermes-data")
        self.assertEqual(kwargs["error_type"], "ValueError")
        self.assertNotIn("secret-ish", json.dumps(kwargs, default=str))


if __name__ == "__main__":
    unittest.main()
