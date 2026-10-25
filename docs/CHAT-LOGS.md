# Agent and CEO chat logs (Axiom)

Every conversation turn, tool call and LLM call from the CEO (Hermes), the
engineering managers and workers, and the Hive coordinator lands in the Axiom
dataset `bmh-company`, with full message text. Nothing extra needs to be
deployed to read them: the `axiom_usage` Hermes plugin
(`hermes-plugins/axiom_usage/__init__.py`) and the worker/manager telemetry
already write them. Query with `mcp__axiom__queryApl` (Claude Code, or the
CEO's own `axiom` MCP server, which uses the read-only `AXIOM_QUERY_TOKEN`).

Queries take 30-85 s on 3 days of data. Always `project` and truncate text
fields with `substring(...)`: a bare `take 1` returns a very large row.

## Shape of the data

| Field | Meaning |
|---|---|
| `service` | Emitting pod, e.g. `hermes-gateway-k8s`, `engineering-k8s`, `engineering-opencode-k8s`, `engineering-opencode-direct-k8s` |
| `role` | `hermes` (CEO), `manager`, `worker`, `coordinator`, or `user`/`assistant` on `message` rows |
| `event` | `message`, `turn`, `llm_call`, `tool_call`; manager `hive_work`, `hive_bid_assessment`, `hive_work_dispatch`, `hive_subscription`, `manager_upstream_call`, `a2a_task`; coordinator `hive.feedback_recorded` |
| `session_id` | Hermes conversation. `2026MMDD_HHMMSS_xxxxxxxx` is interactive (Discord/API); `cron_<id>_<date>_<time>` is a scheduled CEO run |
| `task_id` | Hive task id, or `sa-...` for a Hermes subagent |
| `text`, `user_text`, `assistant_text` | Message bodies |
| `tool_name`, `args.*`, `result` | Tool calls (`result` is capped at 8000 chars) |
| `provider`, `model`, `duration_ms`, `error`, `outcome` | LLM and dispatch details |

Roughly 695 rows in two days carry only `error = "true"` with no event, role or
service; they are not attributable to an agent. Treat them as noise until the
emitter that writes them is identified.

## Queries

Set the window with `_time > ago(...)` in each one.

### CEO: what the Board actually said (interactive, not cron)

Board messages are prefixed `[Nahar]`. The learning-review prompt that Hermes
injects after a session also appears as a user message; exclude it.

```apl
['bmh-company']
| where _time > ago(3d) and ['event']=="message" and role=="user"
    and service=="hermes-gateway-k8s"
    and not(session_id startswith "cron_") and not(task_id startswith "sa-")
    and not(['text'] startswith "Review the conversation above")
| project _time, session_id, t=substring(['text'],0,500)
| order by _time desc | take 50
```

### One full CEO conversation, in order

```apl
['bmh-company']
| where session_id == "<session_id>" and ['event']=="message"
| project _time, role, t=substring(['text'],0,1500)
| order by _time asc
```

Message rows can repeat when Hermes retries a request; dedupe on `turn_id`
and `role` if a transcript looks doubled.

### Scheduled CEO runs (status update, dream audit)

```apl
['bmh-company']
| where _time > ago(1d) and ['event']=="turn" and session_id startswith "cron_"
| project _time, session_id, a=substring(['assistant_text'],0,800)
| order by _time desc | take 20
```

### Errors by agent

```apl
['bmh-company']
| where _time > ago(2d) and isnotempty(['error'])
| summarize n=count() by role=tostring(['role']), service=tostring(['service']),
    event=tostring(['event']), err=substring(tostring(['error']),0,120)
| order by n desc | take 20
```

### CEO tool failures

Match on the result starting with `{"error"`; matching the word "error"
anywhere also catches ordinary page content from `web_extract`.

```apl
['bmh-company']
| where _time > ago(2d) and ['event']=="tool_call" and service=="hermes-gateway-k8s"
    and tostring(['result']) startswith '{"error"'
| summarize n=count() by tool_name=tostring(['tool_name']),
    r=substring(tostring(['result']),0,160)
| order by n desc | take 20
```

### Model failover and slow calls

```apl
['bmh-company']
| where _time > ago(1d) and ['event']=="llm_call" and service=="hermes-gateway-k8s"
| summarize calls=count(), p50=percentile(['duration_ms'],50),
    p95=percentile(['duration_ms'],95) by bin(_time,1h), provider=tostring(['provider']),
    model=tostring(['model'])
| order by _time desc
```

The `provider` column is `custom` for the Nous lane and `custom:zai_coding_plan`
(sometimes plain `custom` with model `glm-5.3-flash`) for the fallback, so read
`model` as well. Failover configured by `agent.api_max_retries` in
`hermes/config.yaml` shows up as calls moving to `glm-5.3-flash`.

### Hive manager and worker trail for one task

```apl
['bmh-company']
| where task_id == "<task_id>"
| project _time, service=tostring(['service']), event=tostring(['event']),
    outcome=tostring(['outcome']), err=tostring(['error']), tool=tostring(['tool_name'])
| order by _time asc
```

### Dispatches nobody picked up

```apl
['bmh-company']
| where _time > ago(2d) and role=="manager"
    and (['error'] contains "no interested bids" or ['error'] contains "timed out")
| project _time, service=tostring(['service']), event=tostring(['event']),
    task_id=tostring(['task_id']), err=substring(tostring(['error']),0,120)
| order by _time desc
```

## What the logs do not hold

- The original ticket body of a Hive task. Events carry only the slug and
  title; the body lives in the coordinator. Look for it in the CEO's
  `hive_submit` tool call (`tool_name == "hive_submit"`, `args.*`) instead.
- Rows for `pm-agent`: confirm it emits to `bmh-company` before assuming a
  silent pm-agent has nothing to say (query by its `service` value).
- Content of anything Hermes ran with `session_id` empty.

## Findings from the first read (2026-10-23 to 2026-10-25)

Use these as a checklist; each is something the queries above surface.

- `hindsight_recall` failed 14 times with "Cannot connect to host hindsight:8888"
  because Hindsight is crash-looping (see the Hindsight ticket in Hive).
- `mcp__steward__manage_files` failed argument validation 15 times and "file
  not found" 7 times; `mcp__steward__query` is refused for tenant-scoped
  credentials 10 times. The CEO is retrying calls that cannot succeed.
- `mcp__postgres_analytics__postgres_mcp_query` failed 6 times on a
  non-existent `created_at` column.
- Model latency, 2026-10-24 12:00 to 2026-10-25 13:30 UTC: the CEO made 486
  calls on `poolside/laguna-s-2.1:free` (p50 51 s, p95 323 s), 58 on
  `meituan/longcat-2.5-preview:free` (p50 7 s, p95 36 s) and 68 on
  `glm-5.3-flash` (p50 18-21 s). The boot-time resolver is picking a free model
  that is several times slower than the alternatives, which is the likeliest
  cause of slow CEO replies.
- The 2026-10-25 12:00 dream-audit cron ended with "I stopped retrying because
  I kept running mcp__steward__query 8 times without making progress": the
  Steward query tool is refused for tenant-scoped credentials, and the cron
  prompt still calls it.
- Managers logged "no interested bids" 6 times and bid-assessment timeouts
  (90 s) 5 times on `engineering-opencode*`.
