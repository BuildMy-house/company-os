# Hive coordinator prototype

This is the first A2A slice for the Company OS Hive. It currently supports:

- `GET /.well-known/agent-card.json`
- JSON-RPC `message/send`
- `GET /tasks/:task_id`

The task registry is intentionally in memory for the protocol smoke test.
Next: replace it with Postgres-backed work/events/leases, then add streaming
completion events and the k3s Job adapter.

```sh
mix deps.get
mix test
mix run --no-halt
```
