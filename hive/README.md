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

```sh
mix deps.get
mix test
mix run --no-halt
```
