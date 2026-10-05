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
RUNNER_MODEL=oc-zai-coding-plan/glm-5.3-flash
HIVE_AGENT_ID=website-builder
```

Sensitive abilities remain Kubernetes RBAC and secret concerns, not prompt
or capability-string concerns.

## Connection recovery and diagnostics

Worker adapters retry Hive subscriptions with capped exponential backoff and
jitter. A `hive_subscription=connected` Axiom event means the worker received
an HTTP 200 `text/event-stream` response; `connecting`, `failed`, and
`reconnecting` describe the actual retry state. Each event includes the pool's
`agent_id`; failures also include stage, duration, attempt, safe cause/code,
and HTTP status when available. Bid-assessment events include the runner,
provider, model, stage, and duration so a failed assessment can be separated
from subscription transport failures.

From the affected worker container, call the `hive_diagnostics` MCP tool. It
checks DNS, TCP, the HTTP agent-card route, and the work SSE route in that
order. It opens and closes the stream before reading data, so the check does
not claim work. The first failing stage narrows the fault to name resolution,
connectivity, HTTP, or SSE routing. Read the matching Axiom events and
container logs from the independent container-manager MCP before restarting
anything.

Completion is retried with the same task, state, and result while the lease
heartbeat stays active. Hive accepts an identical repeat from the original
agent idempotently and emits no duplicate durable event. A different result,
state, agent, or lease attempt is not treated as a retry. On `Transport
closed`, inspect Hive task status and events first: the completion may already
have committed even though the client missed its response.
