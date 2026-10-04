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

```sh
mix deps.get
mix test
mix run --no-halt
```
