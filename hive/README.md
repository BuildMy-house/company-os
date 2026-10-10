# Hive coordinator prototype

This is the first A2A slice for the Company OS Hive. It currently supports:

- `GET /.well-known/agent-card.json`
- JSON-RPC `message/send`
- `GET /tasks/:task_id`
- `GET /events?task_id=...`
- `GET /events/subscribe?task_id=...` (SSE)

With `COMPANY_DATABASE_URL`, work, leases, agents, and lifecycle events are
stored in Postgres. SSE is only a low-latency wake-up path; consumers can
replay the durable event rows by task id after reconnecting.

The current lifecycle emits `work.created`, `work.allocated`, `work.started`,
and either `engineering.completed` or `engineering.failed`.

Each item has a stable human-readable `slug` (provided in request metadata or
derived from its title). Responses return both `id` and `slug`; agents should
use the slug for task status, bids, claims, heartbeats, completion, and event
lookups. The opaque `id` remains the durable event/foreign-key value.

An expired lease emits `engineering.failed` with `reason: lease_expired`, the
attempt number, previous owner, and retry state before the item becomes
available again. The task status retains its attempt count and last failure.
Worker completion is a report for manager review: code work is not marked done
until the verified commit is pushed and that exact remote commit is confirmed.

The Hive coordinator is not automatically redeployed when code reaches `prod`.
An authorized manager builds a digest-pinned image from the approved `prod`
commit with `builder_build_and_push`, verifies a candidate with
`container_test`, then deploys it through `container_upgrade` and checks
health. This manual MCP flow keeps runtime updates under explicit release
control.

## Worker tiers

Submitters add `tier: cheap|standard|power` to the `hive_submit` metadata. The
coordinator stores a known tier on the work payload (`payload.tier`); anything
else is dropped and workers treat a missing tier as `standard`. Each worker
pool declares `AGENT_TIER` (`cheap` for OpenCode, `power` for Claude) and
`AGENT_COST_FACTOR` (true relative cost: Claude 1, GLM Flash 0.1, MiMo free
0.05) in its Deployment env; `hive-member-mcp.js` publishes them in
`capabilities` at registration. The ranking formula is unchanged
(`confidence * benefit / cost`): the worker multiplies its estimated cost by
`AGENT_COST_FACTOR` and by `2^|task tier - pool tier|` for non-`standard`
tasks (`engineering-manager-mcp.js`, `tierAffinityMultiplier`). The
`confidence >= 0.5` fit gate is untouched, so tier shifts preference only and
never forces an unqualified pool onto a task. A non-matching pool waits
`HIVE_TIER_GRACE_MS` (default 30000) before assessing and skips if the item was
already claimed. Before `hive_allocate` a worker checks `hive_work_bids` and
only allocates when its own bid ranks first, because allocation leases the item
to the top bidder, not the caller.

### Already-queued items

Items queued before this scheme have no `tier`, so they are `standard`: the
cost factor alone makes cheaper pools win comparable bids, and no migration is
needed. To steer an existing item, a manager can
set `payload.tier` directly (`UPDATE company.hive_work_items SET payload =
payload || '{"tier":"cheap"}' WHERE slug...` via an authorized DB path) or
cancel and `hive_submit` again with the tier. Items with expired leases
re-enter the queue and are picked up the same way.

### Rollout (manual, per release control)

Pushing to `main` changes nothing live. The worker scripts ship in the
engineering image and the payload change in the Hive image: build both from an
immutable approved `prod` commit with `builder_build_and_push`,
`container_test` each candidate, then a manager (not the pod being changed)
runs `container_upgrade` for `hive-coordinator` first, then
`engineering-opencode`, `engineering-opencode-direct`, and
`engineering-agent`. Verify with a `tier: cheap` test submit and confirm via
`hive_work_bids`/`hive_status` that an OpenCode pool holds the lease.

```sh
mix deps.get
mix test
mix run --no-halt
```
