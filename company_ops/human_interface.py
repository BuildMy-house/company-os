from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from company_ops.observer import ObserverWriter

DISCORD_API = "https://discord.com/api/v10"
CHUNK = 2000


def _get_token() -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set")
    return token


def _chunk(text: str) -> list[str]:
    if len(text) <= CHUNK:
        return [text]
    return [text[i : i + CHUNK] for i in range(0, len(text), CHUNK)]


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    token = _get_token()
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return json.loads(resp.read())


def _post_message(channel_id: str, text: str) -> None:
    for chunk in _chunk(text):
        _post(f"{DISCORD_API}/channels/{channel_id}/messages", {"content": chunk})


def _create_dm_channel(user_id: str) -> str:
    result = _post(f"{DISCORD_API}/users/@me/channels", {"recipient_id": user_id})
    return result["id"]


def _send_dm(user_id: str, text: str) -> None:
    channel_id = _create_dm_channel(user_id)
    _post_message(channel_id, text)


def _dm_target() -> str:
    target = os.environ.get("DISCORD_DM_USER", "")
    if not target:
        raise RuntimeError("DISCORD_DM_USER environment variable is not set")
    return target


def _format_dm(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def ask_information(
    writer: ObserverWriter,
    question: str,
    reason: str | None = None,
    importance: str | None = None,
    related_ids: str | None = None,
    initiated_by: str = "hermes",
) -> dict[str, Any]:
    prior = writer.find_prior_answer(question)
    if prior is not None:
        return {
            "source": "cache",
            "request_id": prior["original_id"],
            "outcome": prior["outcome"],
        }

    request_id = writer.record_human_request(
        type="ask_information",
        question=question,
        reason=reason,
        importance=importance,
        related_ids=related_ids,
        initiated_by=initiated_by,
    )

    body = _format_dm(
        f"**Information needed**\n{question}",
        f"_Reason: {reason}_" if reason else "",
    )
    _send_dm(_dm_target(), body)

    return {"source": "human", "request_id": request_id, "outcome": None}


def ask_judgment(
    writer: ObserverWriter,
    question: str,
    options: str | None = None,
    reason: str | None = None,
    importance: str | None = None,
    related_ids: str | None = None,
    initiated_by: str = "hermes",
) -> dict[str, Any]:
    request_id = writer.record_human_request(
        type="ask_judgment",
        question=question,
        reason=reason,
        importance=importance,
        related_ids=related_ids,
        initiated_by=initiated_by,
    )

    body = _format_dm(
        f"**Judgment call**\n{question}",
        f"Options: {options}" if options else "",
        f"_Reason: {reason}_" if reason else "",
    )
    _send_dm(_dm_target(), body)

    return {"source": "human", "request_id": request_id, "outcome": None}


def request_approval(
    writer: ObserverWriter,
    proposal: str,
    cost_estimate: str | None = None,
    risk: str | None = None,
    importance: str | None = None,
    related_ids: str | None = None,
    initiated_by: str = "hermes",
    blocking: bool = True,
) -> dict[str, Any]:
    request_id = writer.record_human_request(
        type="request_approval",
        question=proposal,
        reason=risk,
        importance=importance,
        blocking=blocking,
        related_ids=related_ids,
        initiated_by=initiated_by,
    )

    body = _format_dm(
        f"**Approval needed**\n{proposal}",
        f"Estimated cost: {cost_estimate}" if cost_estimate else "",
        f"Risk: {risk}" if risk else "",
        "Reply yes/no.",
    )
    _send_dm(_dm_target(), body)

    return {"source": "human", "request_id": request_id, "outcome": None}


def request_action(
    writer: ObserverWriter,
    action: str,
    reason: str | None = None,
    importance: str | None = None,
    related_ids: str | None = None,
    initiated_by: str = "hermes",
    blocking: bool = True,
) -> dict[str, Any]:
    request_id = writer.record_human_request(
        type="request_action",
        question=action,
        reason=reason,
        importance=importance,
        blocking=blocking,
        related_ids=related_ids,
        initiated_by=initiated_by,
    )

    body = _format_dm(
        f"**Action needed (only Nahar can do this)**\n{action}",
        f"_Reason: {reason}_" if reason else "",
    )
    _send_dm(_dm_target(), body)

    return {"source": "human", "request_id": request_id, "outcome": None}


def complete_human_interface_request(
    writer: ObserverWriter,
    request_id: str,
    outcome: str,
    human_minutes: float | None = None,
    avoidable: bool | None = None,
    initiated_by: str | None = None,
) -> str:
    return writer.complete_human_request(
        original_id=request_id,
        outcome=outcome,
        human_minutes=human_minutes,
        avoidable=avoidable,
        initiated_by=initiated_by,
    )
