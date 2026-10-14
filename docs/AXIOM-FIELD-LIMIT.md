# Axiom column limit — how telemetry survives `bmh-company` being full

## The error

```
adding 'attempt' to dataset fields would exceed the column limit of 257
```

Axiom HTTP 400 from `POST /v1/ingest/bmh-company`. The dataset already has
258 fields, so **any event that introduces a new field is rejected as a
whole** — the event is dropped entirely, including the failure events meant
to explain what went wrong. One new key can silently stop an entire event
family from ever landing.

Until this was diagnosed, the response body was never logged, so the
rejection was invisible in production.

## What the code now does

All in `scripts/engineering-manager-mcp.js`:

1. **`sanitizeTelemetryEvent(event)`** — applied inside `emitTelemetry`
   before the ingest body is built:
   - Values for allowlisted keys pass through unchanged, except strings are
     capped at 1000 chars and non-scalars are `JSON.stringify`-ed and capped
     at 500 chars (so `result` stays one column instead of auto-flattening
     into new `result.*` columns the way `args` once did).
   - Every other key (scalar or not) is folded into a single `extra` key:
     `JSON.stringify` of all of them, capped at 500 chars.
   - **No emitted key may exist outside `TELEMETRY_FIELD_ALLOWLIST` plus
     `extra`.** Adding a field to an emitter therefore never risks a 400 —
     it lands in `extra` until it is deliberately allowlisted.
2. **Rejection visibility** — a non-OK ingest response logs one
   `telemetry ingest rejected: HTTP <status> ... body="..."` line per
   distinct status+body per 60 seconds, with Bearer tokens and URLs
   redacted and the body truncated to 300 chars.
3. **Subscription failure lines** — `subscribeHive` logs one
   `hive subscription failure: attempt=... name=... message=... code=...
   cause=... http_status=... stage=...` line per failure, and
   `hive-member` logs `[hive-member] hive_next_work failed: {...}` (fields
   from `safeErrorDetails`: `name`, `message`, `code`, `cause`,
   `http_status`, `stage`), both single-line and redacted.

Run the checks with:

```
node --test scripts/engineering-manager-mcp.test.mjs scripts/hive-member-mcp.test.mjs
```

## The allowlist

`TELEMETRY_FIELD_ALLOWLIST` (exported): `_time`, `service`, `environment`,
`role`, `event`, `state`, `task_id`, `agent_id`, `call_id`, `tool`,
`tool_name`, `duration_ms`, `error`, `pid`, `result`, `provider`, `model`,
`claimed_by`. Every entry was verified to exist as a column of the current
dataset — only these keys may be emitted directly.

## Fields moved to `extra`

Grouped by event (before sanitization they were flat keys):

| Event | Fields in `extra` |
|---|---|
| `manager_upstream_call` | `purpose` |
| `hive_lease`, `hive_completion`, `hive_work_dispatch` | `stage`, `attempt(s)`, `error_name`, `error_code`, `error_cause`, `http_status`, `error_stage` |
| `tool_call` | `args` (object → JSON string) |
| `hive_work` | `reason`, `task_tier`, `pool_tier`, `top_bidder`, `claimed_by` stays allowlisted, `severity`, `consecutive_failures` |
| `hive_bid_assessment` | `stage`, `runner_agent`, `consecutive_failures`, `severity`, `error_name`, `error_code`, `error_cause`, `http_status`, `error_stage` |
| `hive_subscription` (manager) | `attempt`, `delay_ms`, `error_name`, `error_code`, `error_cause`, `http_status`, `error_stage` |
| `hive_subscription` (relayed member, incl. failures) | `stream`, `name`, `message`, `code`, `cause`, `http_status`, `stage` |

Dashboards or APL queries that grouped on a flat column now in `extra`
(e.g. `args`, `attempt`, `stage`) must switch to parsing `extra`, e.g.
`| extend extra = parse_json(extra) | extend attempt = extra.attempt`.

## Operational requirement: `extra` must exist as a column

`extra` is itself a new column. Until the dataset has it, every event with
non-allowlisted fields still 400s (now visible in the logs as a rejected
body). Pick one:

- Point `AXIOM_DATASET` at a fresh dataset (the env var is already
  honored), or
- Free space in the existing dataset (drop/vacuum unused columns in the
  Axiom UI) so `extra` fits under the limit.

## Other ingest callers (audited, not changed)

- `scripts/mcp-gateway.js` — `audit()` posts free-form
  `{_time, service, environment, ...event}` objects (child restarts,
  client counts, auth failures) to the same dataset. It carries the same
  column-limit risk; do **not** import the engineering manager into the
  gateway to reuse its sanitizer — loading that module spawns the Hive
  member client when `HIVE_URL` is set.
- `scripts/opencode-plugins/axiom-usage.js` — ships full usage records to
  the same dataset.
- `hive/lib/hive/telemetry.ex` — Elixir-side emitter, out of scope here.
