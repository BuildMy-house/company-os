import importlib.util
import json
import logging
import sys
import urllib.error
from pathlib import Path

import pytest

PLUGIN_PATH = Path(__file__).resolve().parent / "__init__.py"


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("axiom_usage", PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["axiom_usage"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return load_plugin_module()


def test_sanitize_keeps_allowlisted_fields(mod):
    event = {
        "_time": 1.5, "service": "svc", "environment": "local", "role": "hermes",
        "event": "message", "task_id": "t1", "session_id": "s1", "turn_id": "u1",
        "text": "hello", "input_tokens": 10, "output_tokens": 20,
        "pod_name": "pod-1", "model": "m", "provider": "p", "duration_ms": 42,
        "tool_name": "bash", "tool_call_id": "tc1", "user_text": "hi",
        "assistant_text": "yo", "caller_channel": "discord",
        "reasoning_tokens": 3, "state": "ok", "agent_id": "a", "call_id": "c",
        "tool": "t", "error": "e", "pid": 1, "result": "r", "claimed_by": "x",
    }
    out = mod._sanitize_event(event)
    for key, value in event.items():
        assert out[key] == value
    assert "extra" not in out


def test_sanitize_folds_unknown_fields_into_extra(mod):
    out = mod._sanitize_event({
        "event": "tool_call", "args": {"cmd": "ls"}, "cache_read_tokens": 5,
        "cache_write_tokens": 6, "brand_new_field": "oops",
    })
    assert out["event"] == "tool_call"
    assert set(out) == {"event", "extra"}
    extra = json.loads(out["extra"])
    assert extra["args"] == '{"cmd": "ls"}'
    assert extra["cache_read_tokens"] == 5
    assert extra["cache_write_tokens"] == 6
    assert extra["brand_new_field"] == "oops"


def test_sanitize_caps_long_values(mod):
    out = mod._sanitize_event({"text": "x" * 5000, "unknown": "y" * 5000})
    assert len(out["text"]) == 1000
    assert len(out["extra"]) == 500


def test_sanitize_stringifies_allowlisted_non_scalars(mod):
    out = mod._sanitize_event({"result": {"nested": {"deep": True}}})
    assert out["result"] == '{"nested": {"deep": true}}'
    assert "extra" not in out


def test_no_key_outside_allowlist_plus_extra(mod):
    event = {
        "event": "turn", "session_id": "s", "new_a": 1, "new_b": [1, 2],
        "new_c": {"d": 1}, "new_e": None,
    }
    out = mod._sanitize_event(event)
    allowed = set(mod.TELEMETRY_FIELD_ALLOWLIST) | {"extra"}
    assert set(out) <= allowed


def test_push_sanitizes_before_enqueue(mod):
    mod._push({"event": "message", "text": "hi", "brand_new_field": 1})
    queued = mod._queue.get_nowait()
    assert queued["event"] == "message"
    assert queued["text"] == "hi"
    assert json.loads(queued["extra"])["brand_new_field"] == 1


def test_flush_logs_http_error_body_without_token(mod, caplog, monkeypatch):
    mod._token = lambda: "SECRET-TOKEN-XYZ"
    mod._queue.put_nowait({"event": "message"})
    body = b'{"status":400,"error_details":"adding \'nope\' to dataset fields would exceed the column limit"}'

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://x", 400, "Bad Request", None, None)

        def read(self):
            return body

    def fake_urlopen(req, timeout=None):
        raise FakeHTTPError()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        mod._flush_once()
    record = next(r for r in caplog.records if "rejected" in r.message)
    message = record.getMessage()
    assert "HTTP 400" in message
    assert body[:300].decode() in message
    assert "SECRET-TOKEN-XYZ" not in caplog.text


def test_flush_http_error_body_truncated_to_300(mod, caplog, monkeypatch):
    mod._token = lambda: "SECRET-TOKEN-XYZ"
    mod._queue.put_nowait({"event": "message"})
    body = b"z" * 1000

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://x", 400, "Bad Request", None, None)

        def read(self):
            return body

    def fake_urlopen(req, timeout=None):
        raise FakeHTTPError()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        mod._flush_once()
    record = next(r for r in caplog.records if "rejected" in r.message)
    message = record.getMessage()
    assert message.count("z") == 300
