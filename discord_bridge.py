from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import discord

from company_ops.workers import load_workers, run_worker

# Discord bridge for the Hermes CEO bot.
#
# Setup:
#   pip install -e '.[discord]'
#
# Run (env vars):
#   DISCORD_BOT_TOKEN           bot token from Discord developer portal   (required)
#   DISCORD_WORKER_CONFIG       mcp-workers.json
#   DISCORD_WORKER              default hermes
#   DISCORD_TASK_TYPE           default policy
#   DISCORD_TIMEOUT             default 180
#   DISCORD_ALLOWED_USERS       optional comma list of user IDs
#   DISCORD_ALLOWED_CHANNELS    optional comma list of channel IDs
#   DISCORD_ALLOW_ALL_USERS     optional; if "true", bypass user allow-list
#
#   python discord_bridge.py
#
# DM the bot, or mention it in a guild channel.

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
WORKER_CONFIG = os.environ.get("DISCORD_WORKER_CONFIG", "mcp-workers.json")
WORKER = os.environ.get("DISCORD_WORKER", "hermes")
TASK_TYPE = os.environ.get("DISCORD_TASK_TYPE", "policy")
TIMEOUT = float(os.environ.get("DISCORD_TIMEOUT", "180"))
CHUNK = 2000

ALLOWED_USERS = {u.strip() for u in os.environ.get("DISCORD_ALLOWED_USERS", "").split(",") if u.strip()}
ALLOWED_CHANNELS = {c.strip() for c in os.environ.get("DISCORD_ALLOWED_CHANNELS", "").split(",") if c.strip()}
ALLOW_ALL_USERS = os.environ.get("DISCORD_ALLOW_ALL_USERS", "").lower() in ("true", "1", "yes")

ANNOUNCE_CHANNEL = os.environ.get("DISCORD_ANNOUNCE_CHANNEL", "")
DM_USER = os.environ.get("DISCORD_DM_USER", "")
DAILY_PROMPT = os.environ.get("DISCORD_DAILY_PROMPT",
    "You are Hermes, CEO of Homely. Write today's daily update for the team: current priorities, "
    "what moved forward, and what needs attention. Concise, chat-friendly.")
INVESTOR_PROMPT = os.environ.get("DISCORD_INVESTOR_PROMPT",
    "You are Hermes, CEO of Homely. Write the investor update: progress vs milestones, traction, "
    "risks, and asks. Professional and concise.")
QUESTIONS_PROMPT = os.environ.get("DISCORD_QUESTIONS_PROMPT",
    "You are Hermes, CEO of Homely. If you currently have 2-3 sharp questions Nahar "
    "must answer to keep the company moving, list them. If you have nothing "
    "urgent to ask right now, reply with the single word NONE.")
INTERVAL_DAILY = float(os.environ.get("DISCORD_INTERVAL_DAILY", "86400"))
INTERVAL_INVESTOR = float(os.environ.get("DISCORD_INTERVAL_INVESTOR", "86400"))


def extract_text(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("result", "response", "text", "message", "content"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val
            if isinstance(val, list):
                texts = [i.get("text") for i in val if isinstance(i, dict) and i.get("type") == "text"]
                joined = "\n".join(t for t in texts if t)
                if joined:
                    return joined
            if isinstance(val, dict):
                sub = extract_text(val)
                if sub:
                    return sub
        return json.dumps(payload, default=str)
    return json.dumps(payload, default=str)


def split_reply(text: str) -> list[str]:
    if len(text) <= CHUNK:
        return [text]
    return [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]


def is_user_allowed(sender_id: str) -> bool:
    if ALLOW_ALL_USERS:
        return True
    if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
        return False
    return True


def is_allowed(sender_id: str, channel_id: str) -> bool:
    if ALLOW_ALL_USERS:
        return True
    if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
        return False
    if ALLOWED_CHANNELS and channel_id not in ALLOWED_CHANNELS:
        return False
    return True


async def ask_hermes(prompt: str) -> str:
    try:
        result = await asyncio.to_thread(
            run_worker, WORKER, TASK_TYPE, prompt, load_workers(WORKER_CONFIG),
            True, False, TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return f"[bridge error] {exc}"
    if result.status != "success":
        return f"[worker {result.status}] {result.error or ''}".strip()
    return extract_text(result.result) or "[empty reply from Hermes]"


intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
client = discord.Client(intents=intents)


@client.event
async def on_ready() -> None:
    print(f"Hermes Discord bridge online as {client.user}", flush=True)


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author == client.user:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    mentioned = client.user in message.mentions

    if not is_dm and not mentioned:
        return

    if is_dm and not is_user_allowed(str(message.author.id)):
        return

    if not is_dm and not is_allowed(str(message.author.id), str(message.channel.id)):
        return

    body = message.content.strip()
    for mention in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
        body = body.replace(mention, "").strip()
    prompt = body or "(no message)"

    async with message.channel.typing():
        reply = await ask_hermes(prompt)
    for part in split_reply(reply):
        await message.channel.send(part)


async def announce(label: str, prompt: str) -> None:
    if not ANNOUNCE_CHANNEL:
        print(f"announce[{label}] skipped: DISCORD_ANNOUNCE_CHANNEL unset", flush=True)
        return
    text = await ask_hermes(prompt)
    if text.startswith("["):
        print(f"announce[{label}] skipped: worker error", flush=True)
        return
    channel = client.get_channel(int(ANNOUNCE_CHANNEL))
    if channel is None:
        print(f"announce[{label}] skipped: channel {ANNOUNCE_CHANNEL} not found", flush=True)
        return
    body = f"**{label}**\n{text}"
    for part in split_reply(body):
        await channel.send(part)


async def maybe_ask_questions() -> None:
    if not DM_USER:
        print("maybe_ask_questions skipped: DISCORD_DM_USER unset", flush=True)
        return
    text = (await ask_hermes(QUESTIONS_PROMPT)).strip()
    if not text or text.upper().startswith("NONE") or text.startswith("["):
        return
    user = await client.fetch_user(int(DM_USER))
    dm = await user.create_dm()
    body = f"**Questions from Hermes**\n{text}"
    for part in split_reply(body):
        await dm.send(part)


async def scheduler() -> None:
    jobs = [
        ("Daily update", DAILY_PROMPT, INTERVAL_DAILY, True),
        ("Investor update", INVESTOR_PROMPT, INTERVAL_INVESTOR, True),
    ]
    next_run = [0.0] * len(jobs)
    while True:
        now = time.monotonic()
        for i, (label, prompt, interval, followup) in enumerate(jobs):
            if interval and now >= next_run[i]:
                next_run[i] = now + interval
                try:
                    await announce(label, prompt)
                    if followup:
                        await maybe_ask_questions()
                except Exception as exc:  # noqa: BLE001
                    print(f"scheduler[{label}] error: {exc}", flush=True)
        await asyncio.sleep(60)


async def main() -> None:
    if not TOKEN:
        sys.exit("Set DISCORD_BOT_TOKEN environment variable")

    async with client:
        await asyncio.gather(
            client.start(TOKEN),
            scheduler(),
        )


if __name__ == "__main__":
    asyncio.run(main())
