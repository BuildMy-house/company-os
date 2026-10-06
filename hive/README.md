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

Work subscribers replay currently available tasks on every connection, scoped
to tasks that subscriber has not already bid on. A worker records a
`interested: false` bid when it declines, so the same item cannot block its
queue while remaining available to other agents. The manager consumes one
task at a time and waits until its fit assessment or execution finishes before
reading another.

Managers can call the manager-only `hive_prompt_workers` MCP tool with a
human-readable `work_slug` and a bounded `timeout_seconds` (5–120). Hive wakes
currently subscribed eligible workers; the tool reports each worker's bid,
decline, timeout, or unavailable outcome. The wake-up delivers that exact
item, even when a worker has more than 100 older available items. Workers
write bids with their own identity, and the prompt tool never allocates.

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

```sh
mix deps.get
mix test
mix run --no-halt
```
