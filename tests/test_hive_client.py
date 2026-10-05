from __future__ import annotations

import json
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch

from company_ops.hive_client import HiveClientError, file_feedback_work


def _mock_response(body: bytes) -> BytesIO:
    resp = BytesIO(body)
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: False
    return resp


class TestFileFeedbackWork(unittest.TestCase):
    @patch("company_ops.hive_client.urllib.request.urlopen")
    def test_success_returns_result(self, mock_urlopen):
        result = {
            "id": "task_abc123",
            "name": "Fix login",
            "slug": "fix-login",
            "status": {"state": "submitted"},
        }
        mock_urlopen.return_value = _mock_response(
            json.dumps(
                {"jsonrpc": "2.0", "id": "req-1", "result": result}
            ).encode()
        )

        out = file_feedback_work(
            "http://hive:4100",
            "Fix login",
            "Users cannot log in.",
            {"slug": "fix-login", "source": "pm-agent"},
        )

        self.assertEqual(out, result)
        self.assertEqual(mock_urlopen.call_count, 1)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "http://hive:4100/")
        body = json.loads(req.data)
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["method"], "message/send")
        self.assertTrue(body["id"])
        self.assertEqual(
            body["params"]["message"]["parts"],
            [{"type": "text", "text": "Users cannot log in."}],
        )
        self.assertEqual(
            body["params"]["metadata"],
            {"title": "Fix login", "slug": "fix-login", "source": "pm-agent"},
        )

    @patch("company_ops.hive_client.urllib.request.urlopen")
    def test_slug_taken_raises_with_code(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://hive:4100/",
            409,
            "Conflict",
            {},
            _mock_response(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "req-1",
                        "error": {"code": -32009, "message": "work slug already exists"},
                    }
                ).encode()
            ),
        )

        with self.assertRaises(HiveClientError) as ctx:
            file_feedback_work("http://hive:4100", "Fix login", "desc", {"slug": "fix-login"})

        msg = str(ctx.exception)
        self.assertIn("-32009", msg)
        self.assertIn("work slug already exists", msg)

    @patch("company_ops.hive_client.urllib.request.urlopen")
    def test_jsonrpc_error_in_200_response_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "req-1",
                    "error": {"code": -32601, "message": "unsupported method: x"},
                }
            ).encode()
        )

        with self.assertRaises(HiveClientError) as ctx:
            file_feedback_work("http://hive:4100", "T", "d", {})

        self.assertIn("-32601", str(ctx.exception))

    @patch("company_ops.hive_client.urllib.request.urlopen")
    def test_invalid_json_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"not json at all")

        with self.assertRaises(HiveClientError):
            file_feedback_work("http://hive:4100", "T", "d", {})

    @patch("company_ops.hive_client.urllib.request.urlopen")
    def test_missing_result_field_raises(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(
            json.dumps({"jsonrpc": "2.0", "id": "req-1"}).encode()
        )

        with self.assertRaises(HiveClientError) as ctx:
            file_feedback_work("http://hive:4100", "T", "d", {})

        self.assertIn("result", str(ctx.exception))

    @patch("company_ops.hive_client.urllib.request.urlopen")
    def test_non_2xx_without_jsonrpc_error_raises(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://hive:4100/",
            500,
            "Server Error",
            {},
            _mock_response(json.dumps({"detail": "boom"}).encode()),
        )

        with self.assertRaises(HiveClientError) as ctx:
            file_feedback_work("http://hive:4100", "T", "d", {})

        self.assertIn("500", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
