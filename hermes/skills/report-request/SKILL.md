---
name: report-request
description: Ask engineering for any product metric via a Hive report-request task and block for the reply
version: 1.0.0
metadata:
  hermes:
    tags: [metrics, hive, reporting, engineering]
    category: reporting
---

# report-request

You have no analytics tools and you must never estimate a metric. Whenever you
need a number — user counts, activation, D7 retention, conversion, render
volume, error rates, performance — do NOT guess it, do NOT ask the Board to
route it, and do NOT present a recalled number as current. Submit a
report-request task to Hive addressed to the engineering agents and wait for
their reply. Until the reply arrives, the metric is **Unverified**.

## Request template (exact)

Send with `hive_submit`, metadata `{"tier": "cheap"}` (use `"standard"` only
for genuinely complex multi-metric investigations):

```
[REPORT-REQUEST]
Requester: hermes-ceo
Slug: <kebab-case-short-name>
Question: <one or two sentences: what decision is this for>
Metrics: <comma list, e.g. total_users, new_users_7d, activation_rate, d7_retention>
Period: <ISO dates or "last 7 days">
Definitions: <how you define each metric, or "use engineering's current definition and state it">
Format: report
Deadline: <ISO date or "none">
```

The whole convention lives in the message text. Do not add metadata keys
beyond `tier`.

## Worked example

```
[REPORT-REQUEST]
Requester: hermes-ceo
Slug: weekly-growth
Question: Board update needs the current growth and activation picture.
Metrics: total_users, new_users_7d, activation_rate, d7_retention
Period: last 7 days
Definitions: use engineering's current definition and state it
Format: report
Deadline: none
```

Then `hive_wait(task_id)`. It blocks (SSE). If it times out, re-call
`hive_wait(task_id)` — never fall back to polling `hive_status` in a loop.

## Reading the reply

The answer is in `result.report`:

- `answered_at`, `period`, `source` (system + query description, no
  credentials), `notes`.
- `metrics`: list of `{name, value, unit, definition, row_count, caveats}`.
- `unavailable`: list of `{name, reason}`.

On success: record each metric in your status/memory **as verified**, with its
source, the date, and the slug (cite the slug when you later use the number).
A metric listed under `unavailable` stays Unverified; never estimate a
substitute for it. A measured value with caveats is still verified — pass the
caveats along when you report it.

On failure: the task completes with state `failed` and
`result.report.error` explaining what to resend. Fix the request (usually a
malformed template or an unknown metric name) and submit a corrected one.

## Rate limits

- At most one open (unanswered) request per topic. Do not re-ask while a
  request on the same topic is still pending.
- Before submitting, check memory/status for an answer younger than 24h on the
  same slug/topic and reuse it instead of re-asking.

## Boundary

In `[AGENT QUERY]` sessions (bounded read-only answers to engineering agents)
you must NOT submit report requests — those sessions answer only from what
you already know.
