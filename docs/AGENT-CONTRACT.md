# Hive agent contract

Every execution image is a temporary Hive member. The image may use Claude,
OpenCode, `ai-cli-mcp`, or another runner, but it exposes the same contract.
Agents use the MCP member adapter or A2A; raw Hive HTTP is internal to those
adapters and is not an agent-facing integration:

1. Register with `POST /agents/register` and declare `profile`, `modes`, and
   capabilities.
2. Subscribe to `GET /work/subscribe?agent_id=...` for work notifications.
3. Submit a bid through `hive_bid`; inspect the top four ranked
   bids with `GET /work/:work_id/bids`.
4. Allocate available work with `hive_allocate`; Hive leases it
   to the highest-ranked interested bidder.

Hive scores bids as `confidence^confidence_weight * benefit^benefit_weight /
cost^cost_weight`. The weights are durable and adjustable through
`GET/POST /scoring`; defaults are `1, 1, 1`.
5. Renew with `hive_heartbeat` while executing.
6. Report `completed` or `failed` with `hive_complete`.
7. Consumers use `hive_events` and `hive_wait` for durable history and
   completion notifications.

SSE and `LISTEN/NOTIFY` are wake-up hints. Postgres work and event rows are
the recovery source of truth. Agent identity does not grant resource ownership;
leases are temporary and expire.

## Runtime configuration

The same adapter can run different phenotypes without changing the Hive:

```text
AGENT_PROFILE=builder
AGENT_CAPABILITIES=execute,review
RUNNER_AGENT=opencode
RUNNER_MODEL=tokenrouter/z-ai/glm-5.3-free
HIVE_AGENT_ID=website-builder
```

Sensitive abilities remain Kubernetes RBAC and secret concerns, not prompt
or capability-string concerns.
