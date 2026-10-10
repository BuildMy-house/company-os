# Hive agent contract

Every execution image is a temporary Hive member. The image may use Claude,
OpenCode, `ai-cli-mcp`, or another runner, but it exposes the same contract.
Agents use the MCP member adapter or A2A; raw Hive HTTP is internal to those
adapters and is not an agent-facing integration:

1. Register with `POST /agents/register` and declare `profile`, `modes`, and
   capabilities.
2. Subscribe to `GET /work/subscribe?agent_id=...` for work. On connect and
   reconnect, Hive sends available items this agent has not already bid on.
   The manager finishes its current assessment or execution before reading
   another item, so an SSE wake-up cannot be consumed and dropped while busy.
3. Assess fit, then submit the bid through `hive_bid` using this adapter's own
   identity. Record an explicit `interested: false` bid for a decline; Hive
   then leaves that item visible to other agents without sending it repeatedly
   to this one. The fit rationale is carried into this same manager's execution
   prompt if its bid wins allocation.
4. A manager can call `hive_prompt_workers` with the human-readable
   `work_slug` and a bounded timeout to wake currently subscribed workers.
   Hive delivers that exact item, even when a worker has more than 100 older
   available items. The tool reports each worker's bid, decline, timeout, or
   unavailable state. Workers submit their own bids. Prompting never creates
   bids or allocates work on behalf of a worker.
5. Allocate available work with `hive_allocate`; Hive leases it
   to the highest-ranked interested bidder.

Hive scores bids as `confidence^confidence_weight * benefit^benefit_weight /
cost^cost_weight`. The weights are durable and adjustable through
`GET/POST /scoring`; defaults are `1, 1, 1`.
6. Renew with `hive_heartbeat` while executing.
7. Report `completed` or `failed` with `hive_complete`.
8. Consumers use `hive_events` and `hive_wait` for durable history and
   completion notifications.

SSE and `LISTEN/NOTIFY` are wake-up hints. Postgres work and event rows are
the recovery source of truth. Agent identity does not grant resource ownership;
leases are temporary and expire.

## Prompting workers to bid (manager call order)

Passive polling can leave available work with no interested bids (for
example after a worker run exits and its lease is released, and a restart
produces no fresh bid). A manager can deliberately ask workers to bid with
the engineering-manager MCP tool `hive_prompt_workers`:

1. `hive_prompt_workers({ work: "<human-readable slug>", worker_ids?: [...], timeout_seconds?: 1-300 })`
   resolves the slug to the durable available Hive item, then sends each
   eligible registered worker (endpoint + `bid` and `execute` modes; `GET /agents`)
   an A2A fit-assessment request addressed to that worker's own id.
2. Each worker assesses fit itself (its own capabilities, health, and load;
   task text is fenced as untrusted data; only shared Steward guidance, never
   another pool's personal memories) and submits its **own** `hive_bid` with
   confidence, benefit, cost, risk, approach, and `evidence`. The manager never
   submits or edits a bid; a reported bid only counts if Hive holds it under
   that worker's id.
3. The tool returns per-worker `bid` / `declined` / `unavailable` (with a
   `reason`, e.g. `assessment_invalid`, `worker_busy`, `bid_not_recorded`) /
   `timeout` outcomes, including each stored rationale. It never allocates
   (`allocated: false`) and each worker call is bounded by `timeout_seconds`
   (default 120).
4. Review `hive_work_bids` (ranked, with stored rationale), then call
   `hive_allocate` separately. A prompted worker keeps its assessment and, when it
   runs the allocated work, forwards the same rationale into its execution prompt
   instead of re-assessing.

Order: `hive_prompt_workers` → inspect outcomes / `hive_work_bids` → `hive_allocate`.

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
