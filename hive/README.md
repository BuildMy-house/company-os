# Hive coordinator prototype

This is the first A2A slice for the Company OS Hive. It currently supports:

- `GET /.well-known/agent-card.json`
- JSON-RPC `message/send`
- `GET /tasks/:task_id`

Hive forwards submitted work to the engineering A2A endpoint and keeps the
remote task reference in its local in-memory registry. The registry is still
only a protocol smoke-test store; replace it with Postgres-backed
work/events/leases before production, then add streaming completion events
and the k3s Job adapter.

```sh
mix deps.get
mix test
mix run --no-halt
```
