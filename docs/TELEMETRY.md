# Telemetry (Axiom)

Everything the cluster emits goes to Axiom over the **eu-central-1 edge**
(`https://eu-central-1.aws.edge.axiom.co/v1/ingest/<dataset>`; the default
`api.axiom.co` domain rejects these datasets with HTTP 400). Credentials
(`AXIOM_TOKEN` ingest, `AXIOM_QUERY_TOKEN` read-only) live in Infisical and
reach pods through `company-ops-secrets`; never paste them anywhere.

| Dataset | What | Env var |
|---|---|---|
| `bmh-company` | agent and ops events from every component below | `AXIOM_DATASET` (default `bmh-company`) |
| `buildmy-house-telemetry` | the **app's** product telemetry (written by the app, read by `python -m company_ops telemetry-query` / `opencode/telemetry`) | `AXIOM_TELEMETRY_DATASET` |

The website does not emit to Axiom by design.

## Event shape (shared by all emitters)

Every event carries the same envelope so one query works for all components:

| Field | Meaning |
|---|---|
| `_time` | ISO-8601 UTC (Python/Elixir/JS) or epoch seconds (Hermes plugin) |
| `service` | `AXIOM_SERVICE_NAME` from the manifest, e.g. `hive-coordinator-k8s` |
| `environment` | `DEPLOYMENT_ENVIRONMENT`, e.g. `local-k8s` |
| `role` | `manager`, `worker`, `coordinator`, `agent`, `job`, `hermes` |
| `event` | event name (`startup`, `heartbeat`, `backup`, `llm_call`, ...) |

OTLP emitters (Claude Code) carry the same data as resource attributes:
`service.name`, `deployment.environment.name`, `role`. Rules for new events:
fail-open (telemetry must never break the component), no secrets, no PII, no
object keys or user text unless the emitter is an explicit content-visible
agent plugin.

## Component coverage

| Component | Dataset | `service` / role | Emitter | Events |
|---|---|---|---|---|
| engineering-agent (Claude Code manager) | bmh-company | `engineering-k8s` / manager (OTLP) | `scripts/engineering-entrypoint.sh` env vars | Claude Code metrics + logs |
| engineering-opencode, engineering-opencode-direct (managers) | bmh-company | `engineering-opencode-k8s`, `engineering-opencode-direct-k8s` / manager (OTLP) | same | same |
| OpenCode workers inside those pods | bmh-company | `opencode` / worker | `scripts/opencode-plugins/axiom-usage.js`, registered by `agent-config/opencode.json` | `llm_call`, `message`, `tool_call` |
| hermes-gateway | bmh-company | `hermes-gateway-k8s` / hermes | `hermes-plugins/axiom_usage` | `startup`, `heartbeat` (5 min), `llm_call`, `turn`, `message`, `tool_call`; plus `model_retirement_watch` events |
| hive-coordinator | bmh-company | `hive-coordinator-k8s` / coordinator | `hive/lib/hive/telemetry.ex`, `heartbeat.ex` | `startup`, `heartbeat` (5 min, `HIVE_HEARTBEAT_MS`), `a2a_task`, feedback events |
| pm-agent | bmh-company | `pm-agent-k8s` / agent | `company_ops/axiom_emit.py` | `startup`, `heartbeat` (5 min), `chat` (status code only, no text/user id) |
| postgres-backup CronJob | bmh-company | `postgres-backup-k8s` / job | `company_ops/backup.py` `main()` | `backup` (`outcome`, `duration_ms`, `bytes_uploaded`, `deleted_count`, `error_type` on failure) |
| hermes-data-backup CronJob | bmh-company | `hermes-data-backup-k8s` / job | same | same, `kind=hermes-data` |
| dns-healthcheck CronJob | bmh-company | `dns-healthcheck-k8s` / job | `company_ops/dns_healthcheck.py` | `dns_healthcheck` (`outcome`: ok / stale_dns_detected / error) |
| the app (buildmy-house-app) | buildmy-house-telemetry | app-defined | the app repo | product telemetry |
| postgres, registry, browser-adversary, build dispatchers | none | - | - | not instrumented (see follow-ups) |
| provision-db-roles.sh | none | - | - | operator-run shell script, not a workload; deliberately skipped |

Important: new emitters only take effect once an image containing them is
built and its digest pin updated (`docs/IMAGES.md`, "Digest pins that must
move together"). `dns-healthcheck` uses the `company-os:dns-remediation` tag,
the backups use the digest-pinned `company-os`, pm-agent and hive have their
own images. Until a rebuild+rollout, those components keep emitting nothing.

## Ready-to-paste APL (dataset `bmh-company`)

Last event per service, 24h (anything missing from this list is silent):

```
['bmh-company']
| where _time > ago(24h)
| summarize last_event=max(_time), events=count() by service
| order by last_event desc
```

Expected-service silence check (flags services with no event in 24h; edit the
list to what should be alive):

```
let expected = datatable(service:string)[
  "engineering-k8s","engineering-opencode-k8s","engineering-opencode-direct-k8s",
  "hermes-gateway-k8s","hive-coordinator-k8s","pm-agent-k8s",
  "postgres-backup-k8s","hermes-data-backup-k8s","dns-healthcheck-k8s"];
['bmh-company']
| where _time > ago(24h)
| summarize last_event=max(_time) by service
| join kind=rightouter (expected) on service
| where isnull(last_event)
| project missing_service=service1
```

Role tagging check (rows with a null role are untagged emitters):

```
['bmh-company']
| where _time > ago(24h)
| extend role = coalesce(tostring(role), tostring(['resource.role']))
| summarize events=count() by service, role
| order by service asc
```

Heartbeat gap (a long-lived service should beat every 5 minutes):

```
['bmh-company']
| where _time > ago(24h) and event == "heartbeat"
| summarize beats=count(), last_beat=max(_time) by service
```

Backup health (one row per job per day; `failure` or a missing day is the alert):

```
['bmh-company']
| where _time > ago(7d) and event == "backup"
| summarize runs=count(), failures=countif(outcome == "failure"),
            last_bytes=max(todouble(bytes_uploaded)), p95_ms=percentile(todouble(duration_ms), 95)
  by kind, bin_auto(_time)
```

DNS healthcheck: any non-ok in the last day:

```
['bmh-company']
| where _time > ago(24h) and event == "dns_healthcheck" and outcome != "ok"
```

Field names that contain dots (OTLP resource attributes) need the bracket
form `['resource.role']`; if a column name differs in your dataset schema,
open the dataset's field list in Axiom or ask the Axiom MCP
(`getDatasetInfoAndSchema`) first.

From an agent: the Axiom MCP (`queryApl`) uses the read-only
`AXIOM_QUERY_TOKEN`; from the CLI, `python -m company_ops telemetry-query`
reads the app dataset.

## Follow-ups not done (and why)

- **Pod restart counts / k8s liveness.** Needs a CronJob that lists pods in
  `company-ops`. The only existing ServiceAccount with that verb
  (`company-ops`, `k8s/rbac.yaml`) also holds deployment create/patch/delete,
  so reusing it would be over-privileged and a dedicated read-only SA+Role
  would be new RBAC. Proposed: SA `restart-reporter` with `get,list` on
  `pods` in `company-ops` only, hourly CronJob emitting
  `event=pod_restarts` per pod (`pod`, `restarts`, `reason`). Needs sign-off.
- postgres, registry, browser-adversary and the build dispatchers emit
  nothing; the pod-restart job above covers their liveness cheaply.
- `buildmy-house-telemetry` last received an event on 2026-10-06; that is an
  app-side emitter question (the app repo), not this repo.
- Hermes events are tagged `role=hermes`; the OpenCode plugin's `worker` tag
  only appears after the engineering image that contains the plugin fix is
  running; verify with the role tagging check above.
