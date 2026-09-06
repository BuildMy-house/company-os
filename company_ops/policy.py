from __future__ import annotations

from decimal import Decimal, ROUND_UP

DAILY_ALLOWANCE = 100
DAILY_CAP = 500
REVENUE_CREDIT_PERCENT = 70
ACTION_COSTS = {
    "inspection": 1,
    "seo": 5,
    "social_post": 5,
    "coding": 10,
    "verification": 5,
    "deployment": 10,
    "tool_request": 20,
}

# Prices are TokenRouter list prices where verified. Account-level free quota is
# deliberately separate: quota can change without changing model pricing.
MODEL_COSTS = {
    "tencent/hy3:free": {
        "provider": "nous", "billing": "free_quota",
        "input_usd_per_million": Decimal("0"),
        "output_usd_per_million": Decimal("0"),
        "price_verified": True,
    },
    "mimo-v2.5-free": {
        "provider": "opencode", "billing": "zen_free",
        "input_usd_per_million": Decimal("0"),
        "output_usd_per_million": Decimal("0"),
        "price_verified": True,
    },
    "xiaomi/mimo-v2.5": {
        "provider": "tokenrouter", "billing": "account_quota",
        "input_usd_per_million": None, "output_usd_per_million": None,
        "price_verified": False,
    },
    "z-ai/glm-5.3-flash": {
        "provider": "tokenrouter", "billing": "payg",
        "input_usd_per_million": Decimal("0.075"),
        "output_usd_per_million": Decimal("0.250"),
        "price_verified": True,
    },
}


def model_cost(model: str, input_tokens: int = 0, output_tokens: int = 0) -> dict:
    info = MODEL_COSTS.get(model)
    if info is None:
        return {"model": model, "billing": "unknown", "known": False, "estimated_cost_cents": None}
    if info["input_usd_per_million"] is None:
        cents = None
    else:
        dollars = (Decimal(input_tokens) * info["input_usd_per_million"]
                   + Decimal(output_tokens) * info["output_usd_per_million"]) / 1_000_000
        cents = int((dollars * 100).quantize(Decimal("1"), rounding=ROUND_UP))
    return {"model": model, **info, "known": True, "estimated_cost_cents": cents}


def model_policy_text() -> str:
    rows = []
    for name in MODEL_COSTS:
        cost = model_cost(name)
        if cost["billing"] == "account_quota":
            price = "account quota/free allocation; verify current quota"
        else:
            price = f"${cost['input_usd_per_million']}/1M input, ${cost['output_usd_per_million']}/1M output"
        rows.append(f"- {name}: {price}")
    return "Known model economics:\n" + "\n".join(rows)


def action_cost(action_type: str) -> int:
    try:
        return ACTION_COSTS[action_type]
    except KeyError as exc:
        raise ValueError(f"unknown action type: {action_type}") from exc


def credit_allocation(net_revenue_cents: int) -> int:
    if net_revenue_cents < 0:
        raise ValueError("net revenue cannot be negative")
    return net_revenue_cents * REVENUE_CREDIT_PERCENT // 100
