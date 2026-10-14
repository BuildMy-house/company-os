# Telemetry normalizer — design (no code, nothing deployed)

Status: proposal for review. Steward task
`design-telemetry-normalizer-for-company-ops-docs-telemetry-n`. Companion to
[AXIOM-FIELD-LIMIT.md](AXIOM-FIELD-LIMIT.md), which describes the stop-gap
(`sanitizeTelemetryEvent` / `TELEMETRY_FIELD_ALLOWLIST`) this replaces.

Legend: **[V]** verified against repo code or a cited doc, **[S]** verified
against the live `bmh-company` schema (2026-10-14, read-only Axiom MCP
`getschema`), **[?]** unverified, must be tested before relying on it.

## 1. Problem and what the live check showed

`bmh-company` hits Axiom's 256-field cap; an event that adds a column is
rejected whole. Columns come from three writers: Claude Code (and OpenCode,
which inherits the same `OTEL_*` env) via OTLP, flattened `args.*` from the
manager's `tool_call` events, and ~50 first-party fields.

Findings from this design pass (all [S] unless noted):

- The schema was still **258 fields with no `extra` column**. The "cleaned"
  state was not visible at check time, so the PR #39 sanitizer would still
  400 on any event with non-allowlisted keys. It may have been vacuumed after
  this query; re-check.
- **No rows were queryable.** The Axiom org license limits queries to the last
  30 days and every row query returned zero blocks in that window. Schema
  queries work; event counts, distinct values and metric-vs-log shape could
  not be verified. Either ingest has been rejected for 30+ days or the window
  excludes everything; the dataset (`axiom:events:v1`, created 2026-09-12)
  cannot say which from here.
- The Axiom MCP tool `getDatasetInfoAndSchema` errors server-side (Zod
  "Required maxTime/minTime/quickQueries"); `| getschema` via `queryApl` works
  and was used instead.
- The 107 `attributes.*` are **not all Claude Code**. `attributes.db.*`,
  `http.*`, `url.*`, `rpc.jsonrpc.version`, `network.transport`, `fiberId`,
  the top-level `span_id`/`trace_id`/`parent_span_id`/`events`/`body`/
  `status.*`/`duration` and `resource.opencode.*` look like OpenCode's Effect
  OpenTelemetry output [?] (OpenCode inherits `OTEL_EXPORTER_OTLP_*` from the
  entrypoint; see the comment in `scripts/engineering-entrypoint.sh`).
- The dataset has both flat `model`, `provider`, `tool_name`, ... and
  `attributes.model`, `attributes.tool_name`, ...: OTLP log attributes land
  under an `attributes.` prefix, flat JSON ingest does not. **Sending OTLP
  straight to Axiom can therefore never produce one schema**, whatever the
  collector does to attribute values. This drives the architecture in section 3.
- `role` is used both for deployment role (`manager`/`worker`/`coordinator`)
  and message role (`user`/`assistant`, opencode and hermes message events);
  `call_id` is `int` while `callID`/`tool_call_id` are strings; `error` is
  already `boolean|string`; `cost` is typed `int` although costs are fractional.

## 2. Fixed field list (29 fields, plus Axiom's own `_sysTime`)

Every event leaving the normalizer contains only these keys. Anything else is
folded into `extra` (JSON string, capped 500 chars), as today. String caps:
1000 chars (`text` cap is a question, section 8); `args` and `result` are
capped JSON strings so they never flatten.

| Field | Type | Meaning |
|---|---|---|
| `_time` | datetime | event time (source time if present, else receive time) |
| `service` | string | emitting workload (`engineering-k8s`, `hive-coordinator-k8s`, `mcp-gateway`, ...) |
| `environment` | string | `DEPLOYMENT_ENVIRONMENT` |
| `role` | string | deployment role only: `manager`/`worker`/`coordinator`/`gateway` |
| `agent` | string | **new**: emitting family: `claude`, `opencode`, `codex`, `hermes`, `hive`, `manager`, `gateway` |
| `provider` | string | **new** consistent: `anthropic`, `zai-coding-plan`, `opencode`, ... |
| `model` | string | model id as reported |
| `event` | string | normalized event name |
| `state` | string | `started`/`completed`/`failed`/... (maps `success`, `outcome`, hive `state`) |
| `session_id` | string | one CLI session |
| `task_id` | string | Hive/A2A task |
| `agent_id` | string | worker identity (`engineering-agent`, ...) |
| `call_id` | string | tool-call id (unifies `callID`, `tool_call_id`, `tool_use_id`, `call_id`) |
| `tool_name` | string | unifies `tool` and `tool_name` |
| `duration_ms` | int | |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens` | int | flattened from `tokens.*` / `attributes.*_tokens` |
| `cost_usd` | float | replaces int `cost` / `attributes.cost_usd` |
| `error` | string | message, redacted, capped |
| `error_type` | string | `error_type` / `error_name` / gateway `error_class` |
| `status_code` | int | HTTP status where applicable |
| `msg_role` | string | **new**: `user`/`assistant` (frees `role`) |
| `text` | string | prompt/response/message text (content-visible by the 2026-09-18 decision) |
| `args` | string | tool input as capped JSON string |
| `result` | string | tool output as capped JSON string |
| `extra` | string | capped JSON of everything else |

Relative to PR #39's allowlist: `pid` and `claimed_by` move into `extra`, `tool`
merges into `tool_name`. Existing dashboards on `tool`, `callID`, `sessionID`,
message-valued `role`, `cost`, `tokens.*`, `args.*`, `attributes.*` need
rewrites (section 6; the `axiom-dashboard-panel-upsert` Steward skill covers the API).

### 2.1 Source mapping per agent type

| Source | Where it comes from | Mapping |
|---|---|---|
| Claude Code OTLP **log events** | `scripts/engineering-entrypoint.sh`; event list per [Claude Code monitoring docs](https://code.claude.com/docs/en/monitoring-usage) [V] | `agent=claude`, `provider=anthropic`; `event.name`->`event`; `model`; `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`->`cache_write_tokens`; `cost_usd`; `duration_ms`; `tool_name`; `tool_use_id`->`call_id`; `session.id`->`session_id`; `success`->`state`; `error`, `error_type`, `status_code`; `prompt`/`response`->`text` with `msg_role`; `tool_input`/`tool_parameters`->`args` only if `OTEL_LOG_TOOL_DETAILS` is enabled (it is not today [V]) |
| OpenCode plugin `axiom-usage.js` | `scripts/opencode-plugins/axiom-usage.js` [V] | `llm_call`: `sessionID`->`session_id`, `model`, `provider`, `tokens.{input,output,reasoning,cache.read,cache.write}`->flat token fields, `cost`->`cost_usd`; its `agent` (opencode agent name) -> `extra.agent_name` because `agent` now means family; `messageID`, `title` -> `extra`. `tool_call`: `callID`->`call_id`, `tool`->`tool_name`, `args`, `result`. `message`: `role`->`msg_role`, `text` |
| OpenCode native OTLP (Effect spans/logs) | inferred from `attributes.db.*`, `fiberId`, `span_id` columns [?] | **dropped** at the collector (noise; traces are already disabled by the entrypoint) |
| Hermes plugin `axiom_usage` | `hermes-plugins/axiom_usage/__init__.py` [V] | `agent=hermes`; `llm_call`: `provider`, `model`, `duration_ms`, usage fields; `tool_call`: `tool_name`, `tool_call_id`->`call_id`, `args`, `result`; `message`: `role`->`msg_role`; `turn` duplicates message text (`user_text`/`assistant_text`): proposal is to drop its text and keep `model`/`caller_channel` in `extra` (question 5); `session_id`; `turn_id`->`extra` |
| Manager events | `emitTelemetry` call sites in `scripts/engineering-manager-mcp.js` [V] | `agent=manager`, `role=manager`; existing allowlisted keys unchanged; `tool_call` built from ai-cli `agentOutput.tools` (`call.tool`->`tool_name`, `call.input`->`args`, `call.output`->`result`); the AXIOM-FIELD-LIMIT "moved to extra" fields stay in `extra` |
| ai-cli | no emitter of its own; results reach telemetry only through the manager parsing above [V] | `agent` = runner agent when known (`claude`/`opencode`/`codex`), else `manager`; the 76 `args.*` columns disappear because `args` is one string |
| Hive coordinator (Elixir) | `hive/lib/hive/telemetry.ex`, `router.ex` [V, two emit sites read] | `agent=hive`, `role=coordinator`; `a2a_task` (`task_id`, `state`, `duration_ms`; `work_slug`->`extra`); `hive.feedback_recorded` (`estimated_cost`, `actual_cost`, `estimated_benefit`, `actual_benefit` -> `extra`); other Hive emit sites not enumerated [?] |
| MCP gateway `audit()` | `scripts/mcp-gateway.js` [V] | `agent=gateway`; `event`; `outcome`->`state`; `error_class`->`error_type`; `duration_ms`; `tool`->`tool_name`; `client_id`, `server`, `args_sha256`, `request_bytes`, `response_bytes`, `child`, `code`, `signal`, `restarts`, `reason`, `origin`, `count` -> `extra`. The gateway also logs each record to stdout, so a normalizer outage loses no audit trail |

### 2.2 Claude Code: which events and attributes to keep

Keep what answers "which model, how many tokens, what cost, which tool, how
long, did it fail, which session".

- **Events kept**: `user_prompt`, `assistant_response`, `api_request`,
  `api_error`, `api_refusal`, `tool_result`, `mcp_server_connection`,
  `internal_error`. **Dropped**: `tool_decision`, `permission_mode_changed`,
  `auth`, `plugin_installed`, `plugin_loaded`, `skill_activated`,
  `managed_settings_resolved`, `api_request_body`, `api_response_body` (raw
  bodies are not enabled today [V]; drop defensively, they are large).
- **Into fixed fields**: see the table above.
- **Into `extra`** (small, occasionally useful): `query_source`, `effort`,
  `speed`, `attempt`, `request_id`, `mcp_server.name`, `mcp_tool.name`,
  `agent.name`, `decision_source`.
- **Dropped** (identity, per-version or structural noise): `user.id`,
  `user.account_uuid`, `user.account_id`, `user.email`, `organization.id`,
  `terminal.type`, `app.version`, `app.entrypoint`, `prompt.id`,
  `event.sequence`, `event.timestamp`, `message.uuid`, `client_request_id`,
  `tool_input_size_bytes`, `tool_result_size_bytes`, `mcp_server_scope`,
  `server_scope`, `transport_type`, plugin/marketplace/skill attributes,
  `managed_settings.*`, `*_path_count`, `has_hooks`/`has_mcp`/`host_owned_mcp`,
  and the compaction/cleanup counters in the live schema (`pre_tokens`,
  `post_tokens`, `files_*`, `history_entries_pruned`, ...). Reasons: identity
  data (email/uuid) and unbounded or per-version attributes add no debugging
  value and are what consumed the column budget.
- **Metrics**: recommend `OTEL_METRICS_EXPORTER=none` for workers. Cost and
  tokens are already on `api_request` log events [V, monitoring docs], so the
  counters are redundant. Axiom documents that OTLP metrics need a separate
  metrics dataset and the `X-Axiom-Metrics-Dataset` header
  ([OpenTelemetry integration](https://axiom.co/docs/send-data/opentelemetry));
  the entrypoint sends only `X-Axiom-Dataset`, and the live dataset kind is
  `axiom:events:v1` with no metric columns [S], so metrics may never have
  landed usefully [?].

## 3. Architecture

```
Claude Code / OpenCode (OTLP logs, http/protobuf)      Hermes + OpenCode plugins, manager,
        |                                              hive, gateway (JSON events)
        v                                                       |
 [otel-collector-contrib: otlp receiver -> memory_limiter ->    |
  filter (drop non-kept) -> batch -> otlphttp encoding=json]    |
        |                                                       |
        +----------------->  telemetry-normalizer (Node)  <-----+
                  sanitize: fixed 29 fields + extra, redact, cap
                                |
                                v
              Axiom JSON ingest (only writer, only holder of AXIOM_TOKEN)
```

Why not "Collector -> Axiom exporter" as first proposed: Axiom's OTLP path
writes log attributes under `attributes.*` plus envelope columns (`severity*`,
`trace_id`, `span_id`, `observed_time`, `scope.*`, `body`, ... seen in the
live schema [S]). That creates a second parallel schema next to the flat
first-party fields, and the normalizer would not be the only writer. So the
collector is the OTLP *front door* (protobuf, batching, back-pressure,
dropping) and forwards OTLP/JSON to the normalizer, which flattens log records
to the fixed schema and performs the single Axiom write. One schema definition
(`TELEMETRY_FIELD_ALLOWLIST`, extended) covers every source.

**Simpler fallback if the collector proves awkward**: Claude Code supports
`OTEL_EXPORTER_OTLP_PROTOCOL=http/json` [V, monitoring docs], so the Node
service could accept OTLP/JSON logs directly and the collector could be
dropped. Cost: hand-parse OTLP JSON, no collector batching or filtering, and
OpenCode's protocol support is unverified [?]. Decision for Nahar (section 8).

Pod shape: one Deployment `telemetry-normalizer` in `company-ops` with two
containers sharing `localhost` (collector, normalizer). That is one pod, not
one container; a single image with both binaries means building and pinning a
custom collector image. Flagged as a question.

### 3.1 Collector config approach (documented pieces only)

What was read and will be relied on:

- OTLP receiver: `protocols: {grpc, http}`; defaults are **localhost**:4317 and
  :4318; HTTP paths `/v1/logs`, `/v1/metrics`
  ([otlpreceiver README](https://github.com/open-telemetry/opentelemetry-collector/blob/main/receiver/otlpreceiver/README.md)).
  In a pod the HTTP endpoint must be set explicitly to `0.0.0.0:4318` so
  other pods can reach the Service.
- `filter` processor: OTTL `log_conditions` drop matching records; `error_mode`
  defaults to `ignore`
  ([filterprocessor README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/filterprocessor/README.md)).
  Use it for events outside the section 2.2 keep-list and any span-shaped
  OpenCode records.
- `transform` processor: OTTL `log_statements`, `error_mode` default `ignore`,
  functions `keep_keys`, `delete_key`, `set`
  ([transformprocessor README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/transformprocessor/README.md)).
  Optional here: because the Node service does the final mapping, the
  collector only needs `keep_keys(log.attributes, [...])` and
  `keep_keys(resource.attributes, [...])` as a size reduction, not as the
  correctness mechanism.
- `otlphttp` exporter: `endpoint` is a base URL, `logs_endpoint` overrides the
  full URL for logs, `encoding` is `proto` (default) or `json`, **gzip
  compression is on by default**
  ([otlphttpexporter README](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlphttpexporter/README.md)).
  The Node service must accept `Content-Encoding: gzip`, or the exporter sets
  `compression: none` (loopback in-pod; prefer `none`).
- Axiom's own collector example, for the throwaway test only: `otlphttp`
  exporter with `endpoint: https://AXIOM_DOMAIN` and headers `authorization:
  Bearer ...`, `x-axiom-dataset: ...`
  ([OTel integration](https://axiom.co/docs/send-data/opentelemetry)).

Not asserted as fact, to be proven in section 4: the OTTL needed to read the
Claude Code event name (log attribute vs body), any `debug` exporter options,
the contrib image tag, and the Axiom edge domain for OTLP for this org (today's
scripts use `eu-central-1.aws.edge.axiom.co` for both JSON ingest and OTLP [V]).

### 3.2 Node endpoint

New `scripts/telemetry-normalizer.js` (new image `company-os-telemetry`), no
new dependencies (Node `http`, `fetch`). Routes: `POST /v1/events` (JSON array
or object), `POST /v1/otlp/logs` (OTLP/JSON from the collector), `GET /healthz`.
The sanitizer must be **extracted** from `scripts/engineering-manager-mcp.js`
into a small shared module imported by both, because that file spawns the Hive
member on import when `HIVE_URL` is set and must not be required by the
service (same warning as AXIOM-FIELD-LIMIT gives for the gateway). The service
adds `agent`/`provider` defaulting, `msg_role`, the renames in section 2.1,
a bounded in-memory batch (flush at 100 events or 2 s), rejection logging
reusing `shouldLogIngestRejection`, and a counted hard drop (logged once per
minute) when the queue is full so it can never back-pressure workers. Emitters
then use `AXIOM_TELEMETRY_URL` and stop needing `AXIOM_TOKEN`.

## 4. What must be tested on a throwaway dataset before prod

All with a new dataset (e.g. `bmh-telemetry-test`), never `bmh-company`:

1. **Envelope columns**: send one Claude-shaped and one OpenCode-shaped OTLP
   log through a collector straight to Axiom's OTLP endpoint and `getschema`
   it. Record which non-attribute columns Axiom creates; this confirms or
   refutes the "second schema" argument in section 3.
2. **Does OpenCode emit logs, spans or both** through the inherited `OTEL_*`
   env? Run one `opencode run` against a collector with a debug/logging
   exporter and list signals plus `scope.name`/`service.name`, so the filter
   is written against facts.
3. **Claude Code event shape**: one real `claude -p` against the collector;
   capture where the event name lives and the real attribute names for
   tokens and cost (the docs list `input_tokens`, `cost_usd`,
   `cache_creation_tokens`; the live schema was built by whichever version
   the image shipped).
4. **Normalizer end-to-end**: replay recorded events from each emitter; assert
   the dataset ends with at most ~30 fields however many garbage keys are fed
   in; assert `extra` truncation, `text`/`args`/`result` caps and gzip/no-gzip.
5. **Type conflicts**: `call_id` (int today) vs string, `cost_usd` float,
   `error` bool|string. A fresh dataset avoids the old types; confirm what
   happens if a source still sends the wrong type.
6. **Schema lock**: Axiom documents "lock its schema" to prevent new fields
   ([limits](https://axiom.co/docs/reference/limits)); test whether events with
   unknown keys are dropped or whole-event rejected before using it as a
   second guard.
7. **Outage**: kill the normalizer; confirm workers and collector do not stall
   and the gateway's stdout audit still logs.
8. **Query window**: confirm the 30-day query limit also applies to the new
   dataset.

## 5. Kubernetes outline (follows existing patterns)

- `k8s/base/telemetry-normalizer/telemetry-normalizer.yaml` +
  `kustomization.yaml` (same layout as `k8s/base/hive`): `ConfigMap`
  (collector config), `Deployment` (`replicas: 1`, namespace `company-ops`,
  `runAsNonRoot`, `allowPrivilegeEscalation: false` as in
  `k8s/base/engineering/engineering.yaml`, requests/limits), `Service`
  (ClusterIP; OTLP 4318 and an events port), readiness/liveness on `/healthz`,
  and a `NetworkPolicy` limiting ingress to `company-ops` pods (pattern in
  `k8s/base/mcp-gateway/mcp-gateway.yaml`, which notes enforcement is unproven
  on this cluster).
- Secret: `AXIOM_TOKEN` via `envFrom` `company-ops-secrets` as hive/engineering
  do, or a narrower Infisical-synced secret so only this pod holds the ingest
  token (question 7).
- Wire into `k8s/overlays/buildmy-prod/kustomization.yaml`, or leave it
  admin-applied once like `mcp-gateway` (question 9).
- Images ([IMAGES.md](IMAGES.md)): normalizer is
  `localhost:30500/company-os-telemetry`, new `Dockerfile.telemetry`, context
  repo root, built by `build-push-images.sh` / `builder_build_and_push`. Add a
  row to the IMAGES.md image map and to `scripts/build-push-images.sh`'s table
  in the same commit. Manifest refs are immutable
  `:<component>-<git-sha>@sha256:<digest>` (IMAGES.md tag scheme), the digest
  from `build-push-images.sh --only telemetry --print-digests`. The collector
  is an upstream image like `postgres:16-alpine`: pin by digest, never a
  floating tag; the exact reference and version are not decided here [?].
- Rollout ([DEPLOY-ENGINEERING.md](DEPLOY-ENGINEERING.md)): build from an
  immutable commit on `prod`; `container_upgrade` upgrades existing
  Deployments but **cannot create** one, so first creation needs an authorized
  cluster manifest controller (same constraint documented for
  `engineering-build-dispatcher`). Any `prod` merge or deploy needs Nahar's
  explicit approval; this design authorizes neither.
- Worker Deployments (`engineering*.yaml`, `hive.yaml`, `company-ops.yaml`,
  `mcp-gateway.yaml`) later change env only (section 6); `hermes-gateway`
  upgrades need explicit approval per DEPLOY-ENGINEERING.md.

## 6. Cutover plan and rollback

Preconditions: section 4 passed; new-vs-existing dataset decided (question 2).

1. Build and deploy the normalizer in `company-ops` writing to a **new**
   dataset (ideally a new ingest token scope). Nothing points at it yet.
2. Canary: point one low-volume emitter (the manager) at
   `AXIOM_TELEMETRY_URL`; compare counts and fields against `bmh-company` for
   a day.
3. Switch OTLP producers in `scripts/engineering-entrypoint.sh`:
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://telemetry-normalizer.company-ops.svc:4318`,
   `OTEL_METRICS_EXPORTER=none`, drop the `Authorization`/`X-Axiom-Dataset`
   headers. This needs a new `company-os-engineering` image (prod build and
   upgrade, approval required).
4. Switch JSON emitters (`axiom-usage.js`, Hermes plugin, gateway, `hive/lib/hive/telemetry.ex`,
   manager `emitTelemetry`) to the normalizer URL, removing their direct
   Axiom ingest code and `AXIOM_TOKEN` need in the same change (no
   deprecate-in-place).
5. Repoint dashboards/monitors to the new dataset and renamed fields; workers
   keep only the read-only `AXIOM_QUERY_TOKEN`.
6. After a stable week: vacuum or retire `bmh-company`; delete the
   `AXIOM_OTEL_DATASET`/`AXIOM_DATASET` wiring.

Rollback at each step: revert the env change and redeploy the previous digest
(`container_rollback` or `kubectl rollout undo` per DEPLOY-ENGINEERING.md).
Emitters keep a direct-ingest fallback only until step 6, so a normalizer
outage costs telemetry, never work. Old and new datasets are separate, so
rollback loses nothing already in `bmh-company`.

## 7. Risks

- Renames break every existing panel/query; inventory dashboards first.
- Prompt/response text is content shipped to a third-party SaaS by explicit
  decision (2026-09-18). The normalizer is the right single place to apply
  redaction or caps, but redaction rules are not designed here.
- Handling of `scope.*`, `telemetry.sdk.*`, `resource.*` depends on test 4.1.
- Collector contrib is a large upstream surface; keep the config minimal.

## 8. Open questions for Nahar

1. **Collector in front of Node, or Node-only with `http/json` OTLP?** The
   collector gives protobuf, batching and filtering at the cost of a second
   container and a pinned upstream image.
2. **New dataset vs vacuumed `bmh-company`?** New is cleaner (no old types, no
   `attributes.*` leftovers) but breaks dashboards and history.
3. **Is the Axiom plan Personal (256 fields) or Cloud (1,024, soft)?** The
   limits page documents both; Cloud may let the cap itself be raised, though
   the normalizer is still worth having.
4. **Axiom map fields** ([docs](https://axiom.co/docs/apl/data-types/map-fields)):
   a pre-created map field for `args`/`extra` holds nested objects without
   per-key columns, at higher query cost. Use instead of capped JSON strings?
5. Keep full prompt/response `text` for Claude Code too, and at what cap?
   Drop Hermes `turn` text duplicates?
6. One pod with two containers acceptable (the brief asked for one container)?
7. Ingest token: reuse `company-ops-secrets`, or a dedicated secret so only
   the normalizer holds `AXIOM_TOKEN`?
8. Auth on the normalizer: none inside the namespace (like hive-coordinator)
   plus NetworkPolicy, or a shared bearer?
9. Include in the `buildmy-prod` kustomization, or admin-apply once like
   mcp-gateway?
10. At check time the dataset was still at 258 fields with no `extra` and no
    rows in the last 30 days. Is ingest currently dead, or was a vacuum done
    after our query?
