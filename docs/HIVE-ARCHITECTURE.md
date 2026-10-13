# Hive Architecture Guide

The canonical workspace guide is
[`../../.agents/hive-architecture.md`](../../.agents/hive-architecture.md).

Company OS implements the first slice: an Elixir/OTP coordination service with
durable Postgres work/events/leases, `LISTEN/NOTIFY` or PubSub wake-ups,
supervised consumers, and task-ID-based completion/failure signals. Use
Postgrex and plain SQL initially; Ecto is optional. Hermes remains on its
current Nous configuration; worker model selection belongs to the engineering
manager, which should prefer available free OpenCode models such as MiMo or
GLM Flash. Elixir is the default for new Company OS infrastructure, while
Python remains appropriate for existing integrations, agent tooling, and data
work where it is the simpler choice.

This is a development environment: quality and maintainability outrank
backwards compatibility with the current manager/MCP wiring. Refactor that
wiring when a clean A2A boundary is the better design.

Each agent container should expose A2A for tasks, status, artifacts, and
completion signals. Keep MCP for tools and resources; A2A and MCP are
complementary boundaries.

Do not introduce a second runtime, a token currency, or a full auction market
until the event/lease path is reliable and measured.

## Allocation and cost

### Diagnosis: why cost did not drive allocation (2026-10-13)

Evidence is from reading the code at `9152a6e`; the live cluster logs were not
available in this session, so the lease_expired root cause on the live pods is
unverified (see the last bullet).

- **Hive has no allocator of its own.** `hive_allocate` (`POST
  /work/:id/allocate`) is only ever called by a worker, from `runHiveWork` in
  `scripts/engineering-manager-mcp.js`: assess fit, `hive_bid`, check that its
  own bid is ranked first, then allocate. `Hive.Reaper` only recovers expired
  leases. The `work.allocated` event about 10 s after creation is therefore the
  fastest bidder allocating to itself.
- **First bid won, not best bid.** The ranking formula only compares bids that
  already exist when `hive_allocate` is called. A lone first bid is trivially
  ranked first and allocated immediately. The Claude pool (`engineering-agent`)
  assesses and bids fastest; the OpenCode pools assess through `ai-cli-mcp`
  with free models and are slower, so their cheaper bid arrived after the item
  was claimed (their `hive_allocate` then fails with `work unavailable`).
- **Cost only mattered inside a race.** Workers multiply their estimate by
  `AGENT_COST_FACTOR` (Claude 1, GLM Flash 0.1, MiMo 0.05) and by
  `tierAffinityMultiplier`, but for a task with no `metadata.tier`
  `taskTierOf` is `standard`: the multiplier is 1 and no tier head start
  applies, so default tasks never got a cheap-pool preference at all.
- **The `confidence >= 0.5` gate was documentation only.** `evaluateFit` and
  `isValidAssessment` only range-check confidence (0..1); the only gate is the
  LLM's own `interested` flag. Nothing in Hive enforced a floor.
- **Workers bid passively, and a busy worker does not bid.** Every subscribed
  worker (`GET /work/subscribe`) is woken for each available item and bids on
  its own; `hive_prompt_workers` is only an extra wake-up for items a worker
  may have missed. But `runHiveWork` holds a single slot: while a worker is
  executing or assessing another item it defers and submits no bid, so a busy
  cheap pool silently drops out of the race.
- **No memory of failure.** An expired lease deletes only that holder's bid
  for that item; nothing remembered that `engineering-agent` had just expired
  the same item three times, so the retry could go straight back to it.
- **`hive_work_bids` "work not found".** The manager-role schema declared
  `work_slug`, the shared handler read `args.work_id`, so the request went to
  `/work/undefined/bids` and Hive correctly answered 404. Bids are not purged
  (only an expired holder's own bid is). Fixed: the tool accepts `work_id`,
  `work_slug` or `task_id` and is registered once.
- **Unverified:** why the five live allocations to `engineering-agent` expired
  their leases (never ran, runner start failure, or heartbeat failure). Check
  the engineering-agent pod's `hive_work` / `hive_lease` telemetry for those
  task ids once cluster logs are reachable.

### Selection rule: cheapest bidder that can deliver

`Hive.Selection` (pure, `hive/lib/hive/selection.ex`) decides the winner, used
by both `GET /work/:id/bids` (so the listed order is the allocation order) and
`POST /work/:id/allocate`. An eligible bid is `interested`, has
`confidence >= HIVE_MIN_CONFIDENCE` (default 0.5), comes from an agent seen
within `HIVE_AGENT_HEALTH_WINDOW_SECONDS` (default 900: connected now, or
registered/heartbeat/bid newer than the window), whose lease on this work did
not already expire, and which has fewer than `HIVE_LEASE_EXPIRED_STRIKES`
(default 2) consecutive `lease_expired` failures inside
`HIVE_LEASE_EXPIRED_WINDOW_SECONDS` (default 6 h) that no later
`engineering.completed` by that agent has cleared. Among eligible bids the
lowest `estimated_cost` wins; ties go to the higher legacy score, then to the
earliest bid. If no bid is eligible the legacy score ranking is the fallback
(demoted or low-confidence workers can still get work, otherwise they could
never earn their way back). `HIVE_SELECTION_MODE=score` restores the legacy
ranking. Each bid in the response carries `eligible`, `ineligible_reasons`,
`effective_cost` and `selected`, and `work.allocated` records the `selection`
mode (`cheapest_eligible`, `score_fallback`, `score`).

`estimated_cost` is already the effective cost: workers multiply their raw
estimate by `AGENT_COST_FACTOR` and the tier-mismatch penalty before bidding, so
Hive does not scale it again.

**Bid window.** While a connected worker that registered a `tier` (an engineering
pool) has not bid on the item yet, `allocate` replies `202 {deferred: true,
reason: "bid_window_open", retry_after_seconds}` and leases nothing, until
`HIVE_BID_WINDOW_SECONDS` (default 60, `0` disables) after the item became
available. `runHiveWork` waits and retries, re-checking it is still the selected
bidder. A connected worker that is busy submits no bid, so it can cost the
others up to one window; that is the price of not letting the fastest, most
expensive bidder win by speed.

A retry after `lease_expired` does not go back to the agent that just expired
while another eligible bidder exists (`lease_expired_on_this_work`).

### Cost ledger

`company.hive_cost_ledger` (created with `CREATE TABLE IF NOT EXISTS` at boot,
owned by the Hive's own DB role) holds one row per allocation: `work_id`,
`slug`, `attempt`, `agent_id`, `estimated_cost`, `cost_factor` and `agent_tier`
(from the agent's registration), `tier` (the work's tier), `selection`,
`allocated_at`, `outcome` (`allocated` while running, then `completed`,
`failed`, `lease_expired`, `escalated`), `outcome_at`, `runtime_seconds`. Direct
`claim` calls are not ledgered. `GET /ledger?days=30&limit=20` (MCP:
`hive_cost_ledger`) is read-only and returns per-agent allocations, failures,
runtime and total estimated cost, plus recent rows and the active selection
settings.
