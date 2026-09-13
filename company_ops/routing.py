from __future__ import annotations


FREE_TASKS = {"content", "seo", "summary", "low_risk_code", "test_scaffold"}
DEFAULT_FREE_MODEL = "mimo-v2.5-free"
PAID_FALLBACK_MODEL = "z-ai/glm-5.3-flash"


def choose_provider(task_type: str, free_available: bool = True, avoided_models: set[str] | None = None) -> dict:
    avoided_models = avoided_models or set()
    if task_type in {"security", "deployment", "financial", "policy", "complex_code"}:
        return {"provider": "claude", "model": PAID_FALLBACK_MODEL, "model_tier": "senior",
                "reason": "high-risk or complex task; paid quality is justified"}
    if task_type in FREE_TASKS and free_available and DEFAULT_FREE_MODEL not in avoided_models:
        return {"provider": "opencode", "model": DEFAULT_FREE_MODEL, "model_tier": "free",
                "reason": "low-risk task using available account quota"}
    return {"provider": "opencode", "model": PAID_FALLBACK_MODEL, "model_tier": "fallback",
            "reason": "standard execution route; free route unavailable or avoided"}
