import os
import unittest
from unittest import mock

from company_ops.axiom_client import AxiomClient


class AxiomClientDatasetTests(unittest.TestCase):
    def test_default_dataset_is_app_telemetry(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(AxiomClient(token="t").dataset, "buildmy-house-telemetry")

    def test_ignores_agent_ingest_dataset_variable(self):
        with mock.patch.dict(os.environ, {"AXIOM_DATASET": "bmh-company"}, clear=True):
            self.assertEqual(AxiomClient(token="t").dataset, "buildmy-house-telemetry")

    def test_telemetry_dataset_variable_overrides_default(self):
        env = {"AXIOM_DATASET": "bmh-company", "AXIOM_TELEMETRY_DATASET": "custom-telemetry"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(AxiomClient(token="t").dataset, "custom-telemetry")

    def test_explicit_dataset_wins(self):
        with mock.patch.dict(os.environ, {"AXIOM_TELEMETRY_DATASET": "x"}, clear=True):
            self.assertEqual(AxiomClient(token="t", dataset="y").dataset, "y")

    def test_endpoint_default_and_override(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(AxiomClient().endpoint, "https://api.axiom.co")
        with mock.patch.dict(os.environ, {"AXIOM_ENDPOINT": "https://x.example/"}, clear=True):
            self.assertEqual(AxiomClient().endpoint, "https://x.example")


if __name__ == "__main__":
    unittest.main()
