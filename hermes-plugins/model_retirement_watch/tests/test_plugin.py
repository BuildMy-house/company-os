import copy
import json

import pytest

CONFIG = {
    "model": {
        "provider": "custom:nous",
        "default": "upstage/solar-pro4:free",
        "fallbacks": [
            "custom:tokenrouter/z-ai/glm-5.3-free",
            "custom:zai_coding_plan/glm-5.3-flash",
        ],
    },
    "custom_providers": [
        {
            "name": "nous",
            "base_url": "https://inference-api.nousresearch.com/v1",
            "key_env": "NOUS_API_KEY",
        },
        {
            "name": "tokenrouter",
            "base_url": "https://api.tokenrouter.com/v1",
            "key_env": "TOKENROUTER_API_KEY",
        },
        {
            "name": "zai_coding_plan",
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "key_env": "ZAI_API_KEY",
        },
    ],
}

# Fabricated /models listing WITHOUT the retired id. Expected pick:
# poolside/laguna-2:free — free tier, agentic/coding words, larger ctx
# than the other free candidate (whose description is domain-narrow).
MODELS = [
    {
        "id": "inclusionai/ling-mini:free",
        "context_length": 131072,
        "description": "Health and finance assistant for domain answers.",
    },
    {
        "id": "poolside/laguna-2:free",
        "context_length": 262144,
        "description": "Coding model for code completion and repository-level reasoning.",
    },
    {
        "id": "meituan/longcat-2.0",
        "context_length": 1048576,
        "description": "General chat assistant.",
    },
]

RETIRED = "upstage/solar-pro4:free"


def ev(**overrides):
    evt = {
        "task_id": "t1",
        "turn_id": "u1",
        "api_request_id": "a1",
        "session_id": "s1",
        "platform": "cli",
        "model": RETIRED,
        "provider": "custom:nous",
        "base_url": "https://inference-api.nousresearch.com/v1",
        "api_mode": "chat",
        "api_call_count": 4,
        "api_duration": 2.0,
        "started_at": 0,
        "ended_at": 1759800000.0,
        "status_code": 404,
        "retry_count": 3,
        "max_retries": 3,
        "retryable": False,
        "reason": "model_not_found",
        "error": {
            "type": "not_found_error",
            "message": "model upstage/solar-pro4:free has been retired",
        },
        "request": {},
    }
    evt.update(overrides)
    return evt


@pytest.fixture
def wired(mod, tmp_path, monkeypatch):
    """Stub all network/config I/O; capture steward tool calls."""
    calls = {"steward": [], "models": [], "announce": 0}
    monkeypatch.setattr(mod, "_load_config", lambda path=None: copy.deepcopy(CONFIG))
    monkeypatch.setattr(
        mod,
        "_list_models",
        lambda base_url, key: (calls["models"].append((base_url, key)), copy.deepcopy(MODELS))[1],
    )

    def capture(url, token, tool_name, arguments, timeout=30.0):
        calls["steward"].append(
            {"url": url, "token": token, "tool": tool_name, "arguments": arguments}
        )
        return {"ok": True}

    monkeypatch.setattr(mod, "_mcp_sse_call", capture)
    monkeypatch.setattr(mod, "_announce", lambda evt: calls.__setitem__("announce", calls["announce"] + 1))
    monkeypatch.setattr(mod, "_marker_path", lambda: tmp_path / "retirement-marker.json")
    monkeypatch.setenv("STEWARD_URL", "https://steward.example/mcp/sse")
    monkeypatch.setenv("STEWARD_TOKEN", "test-token")
    return mod, calls


def test_primary_model_not_found_files_task(wired):
    mod, calls = wired
    mod.on_api_request_error(**ev())

    assert len(calls["steward"]) == 1
    call = calls["steward"][0]
    assert call["tool"] == "create_work"
    args = call["arguments"]
    assert RETIRED in args["title"]
    desc = args["description"]
    assert RETIRED in desc
    assert "custom:nous" in desc
    assert "poolside/laguna-2:free" in desc
    assert "404" in desc
    assert "builder_build_and_push" in desc
    assert "container_test" in desc
    assert "container_upgrade" in desc
    assert "hermes/config.yaml" in desc
    assert calls["models"] == [("https://inference-api.nousresearch.com/v1", "")]
    assert calls["announce"] == 1
    marker = json.loads(mod._marker_path().read_text())
    assert marker["model"] == RETIRED
    assert marker["replacement"] == "poolside/laguna-2:free"


def test_rate_limit_on_primary_never_fires(wired):
    mod, calls = wired
    mod.on_api_request_error(**ev(reason="rate_limit", retryable=True))
    assert calls["steward"] == []
    assert not mod._marker_path().exists()


def test_retryable_model_not_found_never_fires(wired):
    mod, calls = wired
    mod.on_api_request_error(**ev(retryable=True))
    assert calls["steward"] == []


def test_fallback_lane_never_fires(wired):
    mod, calls = wired
    mod.on_api_request_error(
        **ev(provider="custom:zai_coding_plan", model="glm-5.3-flash")
    )
    assert calls["steward"] == []


def test_second_event_in_cooldown_does_not_fire_twice(wired):
    mod, calls = wired
    mod.on_api_request_error(**ev())
    mod.on_api_request_error(**ev())
    assert len(calls["steward"]) == 1


def test_cooldown_expiry_fires_again(wired, tmp_path):
    mod, calls = wired
    mod.on_api_request_error(**ev())
    marker_path = mod._marker_path()
    marker = json.loads(marker_path.read_text())
    marker["announced_at"] -= 7200  # well past the 1h cooldown
    marker_path.write_text(json.dumps(marker))
    mod.on_api_request_error(**ev())
    assert len(calls["steward"]) == 2


def test_pick_replacement_heuristics(mod):
    retired = "old/model"

    # free preferred over larger paid
    models = [
        {"id": "paid/big", "context_length": 1000000, "description": "general assistant"},
        {"id": "free/small:free", "context_length": 8192, "description": "general assistant"},
    ]
    assert mod._pick_replacement(models, retired)[0] == "free/small:free"

    # agentic beats domain-narrow at equal tier
    models = [
        {"id": "narrow/m:free", "context_length": 999999, "description": "health and finance chat"},
        {"id": "agent/a:free", "context_length": 999999, "description": "agentic coding with repository reasoning"},
    ]
    assert mod._pick_replacement(models, retired)[0] == "agent/a:free"

    # context_length breaks ties among neutral free models
    models = [
        {"id": "a:free", "context_length": 32768, "description": "assistant"},
        {"id": "b:free", "context_length": 131072, "description": "assistant"},
    ]
    assert mod._pick_replacement(models, retired)[0] == "b:free"

    # retired id itself is never picked
    models = [{"id": retired, "context_length": 9999999, "description": "agentic coding"}]
    with pytest.raises(ValueError):
        mod._pick_replacement(models, retired)


def test_register_records_hook(mod):
    recorded = {}

    class Ctx:
        def register_hook(self, name, fn):
            recorded[name] = fn

    mod.register(Ctx())
    assert recorded.get("api_request_error") is mod.on_api_request_error


def test_hook_swallows_exceptions(wired, monkeypatch):
    mod, calls = wired

    def boom(path=None):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(mod, "_load_config", boom)
    mod.on_api_request_error(**ev())  # must not raise
    assert calls["steward"] == []
    assert mod._FAILURES.get("on_api_request_error") == 1
