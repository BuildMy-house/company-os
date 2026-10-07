# HIVE Guidance & Feedback Spec (Phase 0 deliverable)

Input for the Phase 1 rewrite of `scripts/hive-member-mcp.js`. That adapter
currently returns no `instructions` key from its `initialize` handler (see
line 219: `serverInfo`, `capabilities`, nothing else) and every tool
description is a flat one-liner with no "what to call next" guidance. The
style to mirror is the Steward MCP server: a real lifecycle `instructions`
block, and per-tool descriptions that name the next tool ("...after X, call Y
before Z"). Copy-paste targets are marked verbatim.

## 1. Initialize instructions block

Add an `instructions` key to the `initialize` response `result` object in
`scripts/hive-member-mcp.js`. Exact literal string (copy verbatim):

```text
Hive member tools for the company-os coordination service (Elixir/Postgres work, leases, durable events). Lifecycle: 1) hive_available_work lists open work; bid with hive_bid (confidence, approach, estimated_cost, expected_benefit). 2) Block, never poll: idle members wait inside hive_next_work; members awaiting a specific task wait inside hive_wait(task_id) — do NOT loop hive_status (one-off inspection only) or hive_available_work. 3) While holding a lease, renew with hive_heartbeat every few minutes and always before lease_seconds elapse (default 900) — an expired lease lets the task be re-allocated. 4) hive_complete is terminal: exactly one call per task with state completed|failed and a result.feedback object; after it the lease is gone and hive_wait returns. When any stream call errors, call hive_diagnostics first. hive_events replays durable history; hive_status reads current state; hive_work_bids audits who won and why.
```

## 2. Tool-by-tool description rewrites

New `description` strings for the 13 tools in the `tools` array. Copy each
verbatim (backticks delimit the string, they are not part of it):

| Tool | New description |
|---|---|
| `hive_submit` | `Submit work to Hive and return its durable task id. After submitting, block on hive_wait(task_id) for the outcome — never poll hive_status.` |
| `hive_bid` | `Submit or update this agent's bid for Hive work (confidence, approach, estimated_cost, expected_benefit). After bidding, wait for allocation via hive_next_work or hive_wait(work_id); once allocated, hive_heartbeat the task while working.` |
| `hive_work_bids` | `Read the ranked bids (with each bidder's stored rationale) for Hive work, by id or slug. Call before hive_allocate to sanity-check the ranking, or after allocation to audit why a bidder won.` |
| `hive_available_work` | `List currently available Hive work items (id, slug, payload). Pick items worth bidding on and call hive_bid; to learn of new arrivals, wait inside hive_next_work instead of re-calling this in a loop.` |
| `hive_agents` | `List registered Hive agents with their A2A endpoint and declared capabilities. Call before hive_submit when you need to route or verify a target agent.` |
| `hive_allocate` | `Allocate available Hive work to its highest-ranked interested bidder (creates a lease). The allocated agent should hive_heartbeat the returned task_id while working and hive_complete when done.` |
| `hive_heartbeat` | `Renew this agent's temporary Hive work lease (default 900s). Call every few minutes while still working, always before lease_seconds elapse — an expired lease lets the task be re-allocated. When finished, call hive_complete instead.` |
| `hive_complete` | `Record terminal completion or failure for work held by this agent; exactly one call per task, and the lease ends. result must include the required feedback object (calibration, friction, optional suggested_guidance_change). After this, hive_wait on the task returns immediately — use hive_status only to verify the recorded state.` |
| `hive_status` | `Read the durable status of a Hive task — one-off inspection only, never poll in a loop. To block on completion use hive_wait; for the event history use hive_events.` |
| `hive_events` | `Replay durable lifecycle events for a task (bids, allocation, heartbeats, completion). Use to debug a stuck task, then hive_status for current state or hive_wait to block on the outcome.` |
| `hive_wait` | `Wait for a task's completion/failure event over SSE without polling (timeout up to 900s; re-call to keep waiting). This is the default way to await any task you submitted or hold — on timeout, re-call rather than switching to hive_status polling.` |
| `hive_next_work` | `Wait for the next available Hive work item over SSE without polling. Returns the allocated task — then hive_heartbeat it while working and hive_complete when done. Idle members should live inside this call, not loop on hive_available_work.` |
| `hive_diagnostics` | `Check DNS, TCP, the Hive HTTP health route, and the worker SSE subscription from this container (opens and immediately closes an unclaimed stream; no work is consumed). Call this first when hive_wait or hive_next_work error or time out unexpectedly.` |

## 3. hive_complete feedback block schema

A required `feedback` object nested in `hive_complete`'s existing `result`
object. Because `feedback` lives inside `result`, `result` becomes required
too (it previously was optional). JSON Schema fragment for the feedback
block:

```json
{
  "result": {
    "type": "object",
    "required": ["feedback"],
    "properties": {
      "feedback": {
        "type": "object",
        "required": ["calibration", "friction"],
        "properties": {
          "calibration": {
            "type": "object",
            "required": ["estimated_cost", "actual_cost", "estimated_benefit", "actual_benefit"],
            "properties": {
              "estimated_cost": { "type": "number" },
              "actual_cost": { "type": "number" },
              "estimated_benefit": { "type": "number" },
              "actual_benefit": { "type": "number" }
            },
            "additionalProperties": false
          },
          "friction": {
            "type": "string",
            "description": "What guidance was missing or wrong; \"\" if none."
          },
          "suggested_guidance_change": {
            "type": "string",
            "description": "Concrete change to guidance/specs; omit or empty if none."
          }
        },
        "additionalProperties": false
      }
    },
    "additionalProperties": true
  }
}
```

Full updated `hive_complete` inputSchema (Phase 1 copy-paste target):

```json
{
  "type": "object",
  "required": ["task_id", "state", "result"],
  "properties": {
    "task_id": { "type": "string" },
    "state": { "type": "string" },
    "result": {
      "type": "object",
      "required": ["feedback"],
      "properties": {
        "feedback": {
          "type": "object",
          "required": ["calibration", "friction"],
          "properties": {
            "calibration": {
              "type": "object",
              "required": ["estimated_cost", "actual_cost", "estimated_benefit", "actual_benefit"],
              "properties": {
                "estimated_cost": { "type": "number" },
                "actual_cost": { "type": "number" },
                "estimated_benefit": { "type": "number" },
                "actual_benefit": { "type": "number" }
              },
              "additionalProperties": false
            },
            "friction": {
              "type": "string",
              "description": "What guidance was missing or wrong; \"\" if none."
            },
            "suggested_guidance_change": {
              "type": "string",
              "description": "Concrete change to guidance/specs; omit or empty if none."
            }
          },
          "additionalProperties": false
        }
      },
      "additionalProperties": true
    }
  },
  "additionalProperties": false
}
```

Notes: `state` is left a free string to match the current schema; tightening
it to `enum: ["completed", "failed"]` is a separate decision for Phase 1.
`result` keeps `additionalProperties: true` so callers can attach other
result data alongside `feedback`.

## 4. Feedback forwarding rule (Phase 2 implementation contract)

Forwarding is a **client-side** responsibility in
`scripts/hive-member-mcp.js`, which already holds the Steward MCP
connection. The Elixir side validates shape only.

Rule, evaluated on every `hive_complete` call carrying a `feedback` object:

1. **`suggested_guidance_change` is non-empty** → the member adapter forwards
   to Steward `specs_propose` with `document_type: "hive_guidance"` (plus
   title/content derived from the change text and the task context), so the
   proposal enters the existing `specs_approve` review pipeline as a
   reviewable document. Nothing is auto-approved.
2. **`suggested_guidance_change` is empty/omitted, but `calibration` or
   `friction` data exists** → the adapter forwards to Steward
   `submit_task_feedback` (calibration numbers and friction text ride in
   `learned_for_agents` / feedback fields), feeding the system review loop
   without creating a spec entry.
3. **No feedback or all-empty fields** → no forwarding.

Responsibility split:

- `scripts/hive-member-mcp.js` — client-side forwarding: calls Steward
  `specs_propose` / `submit_task_feedback` after a successful
  `hive_complete`; forwarding failures are logged (via
  `notifications/message`) and never fail the terminal complete call.
- `hive/lib/hive/tasks.ex` + `hive/lib/hive/work.ex` — server-side schema
  validation only: reject `hive_complete` results whose `feedback` object
  violates the schema in section 3 (required `calibration` numbers,
  `friction` string present). The Elixir services never call Steward; the
  actual Steward forwarding call lives in `hive-member-mcp.js` because that
  is where the Steward MCP connection already exists.
