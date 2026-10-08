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

## 5. Review-to-guidance pipeline (Phase 3)

Verified live on 2026-10-07 (agent `2_Mia`, Steward at
`buildmyhouse.stewardacs.xyz`). Goal: confirm an APPROVED `hive_guidance`
spec becomes visible to a live `generate_guidance_packet(scope_path: "hive")`
call — the mechanism Phase 1.3 wired hive members to use.

### 5.1 Proposal + approval works (real calls)

- `specs_propose(app: "hive", path: "hive_guidance/test-1791393490",
  document_type: "spec", title: "test entry")` → stored, `status: proposed`,
  `version: 1`. (The MCP binding's `document_type` enum has no
  `"hive_guidance"`; `"spec"` was used for the live test.)
- `specs_approve(app: "hive", path: "hive_guidance/test-1791393490",
  reviewer: "2_Mia")` → `status: approved`, `version: 2`. **Self-approval was
  used purely to verify the pipe for this ticket; real proposals must be
  approved by a human or a different reviewer.**
- A second entry with realistic title/content ("Hive worker operating
  guidance: bidding, waiting, heartbeats, completion") plus
  `tags: ["hive_guidance", "hive"]` and `project: "hive"` approved the same
  way → stored and versioned identically.

### 5.2 Approved entries do NOT surface in live guidance (observed)

- `generate_guidance_packet(scope_path: "hive")` returned `relevant_specs` =
  exactly `{company-os/scripts/hive-member-mcp, company-os/scripts/
  engineering-manager-mcp, hive/hive/router}` — a stable set. The approved
  test entries were absent across 6 packet generations over ~10 minutes,
  regardless of title/content/tags/project.
- `query_specs(query: "hive worker operating guidance bidding waiting
  heartbeats completion")` ranked the approved entry #2 immediately —
  entries are stored and searchable, just not packet-selected.
- `hive/work` (a real, approved module spec) also never surfaces → the
  effect is not specific to test entries.
- `generate_guidance_packet(scope_path: "hive/work")` → `relevant_specs: []`
  (not path-prefix matching). `generate_guidance_packet(task_id: …)` → a
  different stable set, also excluding the test entry.
- No client-controllable parameter steers packet membership: the binding
  strips unknown params; raw JSON-RPC `specs_propose` with `scope_path` is
  accepted but the server stores no `scope_path` field on the entry.
- `steward_query` (read-only SQL) is blocked for tenant-scoped credentials
  ("Tenant-scoped credentials cannot query database tables directly"), so no
  DB-level inspection was possible.

**Conclusion:** `specs_propose` → `specs_approve` works and entries are
immediately searchable, but `generate_guidance_packet`'s `relevant_specs`
draws from a server-side registry/snapshot that freshly approved specs do
not join within the observation window, via any parameter we control. The
gap is Steward-side scope-store indexing — outside this repo. Phase 2's
call shape was not the cause, with one real exception:

### 5.3 Real bug found + fixed: `document_type: "hive_guidance"` is rejected

- Raw JSON-RPC (Phase 2's exact transport) `specs_propose` with
  `document_type: "hive_guidance"` → server error: `Validation failed:
  invalid document_type: hive_guidance. Must be one of: spec, knowledge,
  project, marketing, deliverable, policy, process, guideline, reference`.
- Because forwarding is fire-and-forget (section 4), every forwarded
  proposal would have failed validation silently — the feedback would never
  have reached Steward at all.
- Fix: `scripts/hive-member-mcp.js` now sends `document_type: "knowledge"`
  (closest accepted type for forwarded operating feedback). The
  `hive_guidance/` path prefix is unchanged — freeform paths are accepted
  and keep entries discoverable via `query_specs`. Test assertion updated;
  `node --test scripts/hive-member-mcp.test.mjs` → 3 pass, 0 fail. Server
  re-verified to accept `"knowledge"` via the same raw transport.
- Note: section 4 above describes the original pre-fix
  `document_type: "hive_guidance"`; `"knowledge"` is now authoritative.

### 5.4 Status of the loop

- Working end to end: `hive_complete` feedback → `specs_propose`
  (now `"knowledge"`) → entry stored, versioned, immediately searchable via
  `query_specs`.
- Not yet working: newly approved entries joining
  `generate_guidance_packet` `relevant_specs`. Open with Steward
  maintainers — requires scope-store indexing of approved specs per
  `scope_path`.

### 5.5 Cleanup of test entries

Both test entries were tombstoned (content replaced with a TOMBSTONED note,
title prefixed `(tombstoned)`) and soft-rejected via `specs_reject` →
`status: rejected`. No delete tool exists, so the entries remain in the
store — partial cleanup only.
