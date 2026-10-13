# Composability Audit — BuildMy.house coupling in company-os

Goal: can `hive + pm-agent` (or the engineering worker pool) be deployed for a
different project without forking manifests?

Classification: **(A)** hard-coded in code/config, no env override · **(B)**
env-overridable, default is BuildMy-specific · **(C)** manifests/docs only ·
**(D)** harmless/cosmetic. "Code/config" includes baked YAML config files
(`hermes/config.yaml`, `pm-agent/hermes-config.yaml`) that are read verbatim.

> **Path note:** a concurrent session refactored `k8s/*.yaml` into
> `k8s/base/<component>/*.yaml` + `k8s/overlays/<project>/` while this audit
> ran. Manifest refs below use the new paths (same files, line numbers shifted
> slightly in `postgres`, `engineering`, `pm-agent-gateway`, `hermes`).

## Summary

Coupling is **shallow and mostly class B/C**. Nothing structural (schemas,
protocols, DB layout, ports-as-contract) carries a project name; the hard A-class
couplings are a handful of literal strings and two org-pinned git URLs.

- Only **~15 A-class code couplings** exist across the whole repo, and they fall
  into three buckets: brand strings (persona/refusal/agent-card), two git URLs
  (`sync-repo.sh`, `entrypoint.sh`), and two policy literals in the builder
  (`REGISTRY_HOST`, `github.com/BuildMy-house/` org regex).
- The **namespace `company-ops`, secret `company-ops-secrets`, registry
  `localhost:30500`, and DB `homely_company`** are the four deployment-level
  tokens; all are manifest-level (C), so a kustomize overlay handles them.
- The overlay (`k8s/overlays/buildmy-prod/kustomization.yaml`) does **not**
  include `infisical-sync.yaml`, `pm-agent-infisical-sync.yaml`,
  `mcp-gateway.yaml`, or any `buildmyhouse-dev-*` manifest — secrets are created
  by `scripts/bootstrap-secrets.sh`, and the app-project dev-cluster stack is
  already outside the deployment surface.
- Two genuine bugs surfaced: `ENGINEERING_A2A_URL` in `k8s/base/hive/hive.yaml:25` is
  **never read** by hive (dead env var), and `pm-agent/hermes-config.yaml:70`
  hard-codes `HIVE_URL` instead of interpolating it.
- Discord: **no numeric channel/user IDs are hard-coded anywhere**; all via
  `.env.example:37-46` with empty defaults (B/D).

## Per-component portability verdict

| Component | Verdict | What must change to run standalone for another project |
|---|---|---|
| **hive** | **needs code change** (small) | `router.ex:11-13` agent-card name/desc/url (+ `hive/test/hive/router_test.exs:27`); `engineering.ex:20,38` A2A URL is effectively fixed (`Application.get_env`, no `hive/config/` dir) — wire it to `System.get_env("ENGINEERING_A2A_URL", …)` so the existing `k8s/base/hive/hive.yaml:25` env works. Everything else (port, DB, Axiom dataset/service) is env. |
| **pm-agent** | **needs code change** (small) | `pm-agent/hermes-config.yaml:70` literal `HIVE_URL` → `${HIVE_URL}` (hermes already interpolates `${PM_DATABASE_URL}` there); `guard.py:39` refusal text; `SOUL.md` persona (any new project must rewrite this anyway). Image build (`pm-agent/Dockerfile`) is self-contained. |
| **engineering worker image** | **needs code change** (medium) | `Dockerfile.engineering:5,78` `shared` build context = `github.com/BuildMy-house/workspace.git` (point at your own workspace repo, or `COPY` a local path); `scripts/sync-repo.sh:20-27,51,55` org + fixed repo→path map; `scripts/builder-manager-mcp.js:279` private-org regex. Repo URLs, GitHub App IDs and steward URL are all env (manifest values in `k8s/base/engineering/*.yaml`, defaults in `github-app-token.js:26-27`, `generate-agent-mcp-config.js:103`). |
| **hermes** | **needs code change** (medium) | `hermes/SOUL.md` (persona + dispatcher policy), `hermes/config.yaml:142` literal steward URL, `scripts/entrypoint.sh:94` `git@github.com:BuildMy-house/hermees-diary.git`, `hermes-plugins/axiom_usage/__init__.py:42` dataset `bmh-company`. MCP service endpoints (`browser-adversary:8000`, `hermes-build-dispatcher:8000`) are config-level, edit once. |
| **builder / registry** | **needs code change** (medium) | `scripts/builder-manager-mcp.js:134` `REGISTRY_HOST` is explicitly **not** env-overridable (comment `:124`); `:642` `context_ref` fixed to `BuildMy-house/company-os.git`; `:619` returns `localhost:30500/…` (nodePort `k8s/base/registry/registry.yaml:74`). `registry-manager-mcp.js:11` is already env (B). |
| **postgres** | **needs env-only** | DB name `homely_company` in `k8s/base/postgres/postgres.yaml:43,56,61` (C) and `scripts/provision-db-roles.sh:206` (B, `PGDATABASE`); one-line A in `scripts/init-postgres.sh:9,12` (`-d homely_company`). Schemas (`sql/*.sql`) are project-neutral. |

## Coupling table

| # | Coupling | Refs | Cls | Note |
|---|---|---|---|---|
| 1 | "buildmy.house Hive" agent card + `http://hive-coordinator:4100` url | `hive/lib/hive/router.ex:11-13`, test `hive/test/hive/router_test.exs:27` | A | url not env-read; test asserts the literal |
| 2 | engineering A2A URL `http://engineering-agent:8001` | `hive/lib/hive/engineering.ex:20,38`; dead env `k8s/base/hive/hive.yaml:25` | A | no `hive/config/` dir → `Application.get_env` never sees env |
| 3 | Axiom ingest dataset `bmh-company`, EU edge URL | `hive/lib/hive/telemetry.ex:13`; `hermes-plugins/axiom_usage/__init__.py:42,48`; `model_retirement_watch/__init__.py:51` | B / A | dataset env in hive; **hard-coded in both plugins** |
| 4 | Telemetry dataset `buildmy-house-telemetry` | `company_ops/axiom_client.py:9` | B | `AXIOM_TELEMETRY_DATASET` |
| 5 | Steward MCP `https://buildmyhouse.stewardacs.xyz/mcp/sse` | `hermes/config.yaml:142` (A); `company_ops/dns_healthcheck.py:51`; `scripts/generate-agent-mcp-config.js:103`; `model_retirement_watch/__init__.py:359` (B) | A/B | `STEWARD_MCP_URL`/`STEWARD_URL` already exist |
| 6 | pm-agent `HIVE_URL` literal | `pm-agent/hermes-config.yaml:70` | A | while `agent.py:40` + `pm_tools_mcp.py:107` are B |
| 7 | pm-agent refusal + persona | `pm-agent/guard.py:39`; `pm-agent/SOUL.md:1-4`; `pm-agent/agent.py:1` | A / D | must be rewritten per project |
| 8 | GitHub org `BuildMy-house` (code path) | `scripts/sync-repo.sh:51,55`; `scripts/builder-manager-mcp.js:279,642`; `Dockerfile.engineering:5,11,78` | A | `sync-repo.sh` also fixes repo names→paths `:20-27` |
| 9 | GitHub App id/installation defaults + PEM search paths | `scripts/github-app-token.js:26-31` | B | `GITHUB_APP_ID`/`_INSTALLATION_ID`/`_PRIVATE_KEY(_PATH)`; `.env.example:78` `GITHUB_REPOSITORY=BuildMy-house/house-designer-ops` (B) |
| 10 | Six repo URLs `https://github.com/BuildMy-house/*.git` | `k8s/base/engineering/{engineering,engineering-opencode,engineering-opencode-direct}.yaml:81-91` | C→B | plain env values (`APP_REPO_URL` …), read by `sync-repo.sh` |
| 11 | `hermes-diary` clone/push loop | `scripts/entrypoint.sh:94`; `HIVE_AGENT_ID=hermees` `k8s/base/hermes/company-ops.yaml:38` | A / C | |
| 12 | Registry host `registry.company-ops.svc.cluster.local` | `scripts/builder-manager-mcp.js:134` (A, no env); `scripts/registry-manager-mcp.js:11` (B) | A/B | |
| 13 | Registry nodePort `30500` / `localhost:30500` images | `k8s/base/registry/registry.yaml:74`; `scripts/builder-manager-mcp.js:619`; every `k8s/base/**` manifest `image:` | C / A | manifest-wide; builder's returned ref is code |
| 14 | DB name `homely_company` | `k8s/base/postgres/postgres.yaml:43,56,61` (C); `scripts/init-postgres.sh:9,12` (A); `scripts/provision-db-roles.sh:206` (B); `docker-compose.test.yml:9`; `scripts/test-db-up.sh:31,51-53`; `scripts/staging-promote.sh:47-48` | mixed | role names `hermes_company`/`hermes_observer_writer`/`hermes_analytics` are B/C |
| 15 | Backup/client identifiers `homely-*` | `company_ops/backup.py:172` (`homely-backup-*.sql`); `company_ops/mcp_client.py:46` (`homely-company-ops`) | A / D | filename prefix + clientInfo string |
| 16 | Namespace `company-ops`, secret `company-ops-secrets` (+ `pm-agent-secrets`, `builder-manager-token`, `company-ops-steward-*`) | every `k8s/base/**` manifest (`namespace:`/`secretName:`); `scripts/bootstrap-secrets.sh:42,56,62` | C | see `k8s/overlays/buildmy-prod/kustomization.yaml` for what is actually rendered |
| 17 | `*.company-ops.svc.cluster.local` service DNS defaults | `company_ops/agent_query_interface.py:73`; `scripts/hermes-messenger-mcp.js:25` (B); `builder-manager-mcp.js:134` (A) | B/A | `HERMES_API_HOST`, `REGISTRY_HOST` |
| 18 | Infisical project id `8806c2b0-…` | `.env.example:28`; `scripts/bootstrap-secrets.sh:40`; `k8s/infisical-sync.yaml:53,56` | B/C | |
| 19 | `buildmyhouse-dev` dev-cluster stack | `k8s/buildmyhouse-dev-*.yaml`, `k8s/base/core/rbac.yaml:64,74,81`, `scripts/dev-deploy-poller.js:31-39,161`, `k8s/base/browser-adversary/browser-adversary.yaml:26` | C | **not in the overlay** — drop for a new project, no code change |
| 20 | Axiom/telemetry + model strings in prompts/docs | `hermes/SOUL.md:1-3,34,70,112`; `agent-config/AGENTS.md:13-20`; `hermes-engineering/**/AGENTS*.md`; `Dockerfile:18` | A/C/D | content must be re-authored per project |
| 21 | `/opt/company-ops` absolute paths | `pm-agent/Dockerfile:35,40`; `hermes/config.yaml:197,202`; `scripts/engineering-entrypoint.sh:109,123,195` | D | internal layout, rename is cosmetic |
| 22 | `KUBERNETES_SERVICE_HOST=10.43.0.1` | `hermes/config.yaml:174-175`; `k8s/base/engineering/*.yaml` | B/D | k3s default, cluster-coupled not project-coupled |
| 23 | Discord config | `.env.example:37-46`; `company_ops/human_interface.py:15,49,56` | B | no numeric IDs anywhere; UA string `human_interface.py:36` is D |
| 24 | LLM providers/models | `hermes/config.yaml:14-26`; `pm-agent/hermes-config.yaml:27-40` | B | vendor defaults, project-neutral |

## Cross-component dependency graph

```
postgres (homely_company + company/observer/pm schemas)
   ^  ^            ^                ^
   |  |            |                └─ pm-agent-secrets.PM_DATABASE_URL (own secret)
   |  |            └─ hermes-gateway: COMPANY/OBSERVER/ANALYTICS_DATABASE_URL
   |  └─ hive (COMPANY_DATABASE_URL optional → in-memory fallback)
   └─ engineering worker pool (company-ops-secrets; Infisical fetch at boot)

hive-coordinator:4100  <── HIVE_URL ── pm-agent:4200, engineering-agent:8001,
   ^                                    hermes-gateway (hermees), mcp-gateway
   └─ ENGINEERING A2A → engineering-agent:8001   [env var currently dead]

pm-agent-gateway (nginx, NodePort 30420, PM_AGENT_AUTH_TOKEN
   from company-ops-secrets) ──► pm-agent:4200

engineering pool ──► registry (registry.company-ops.svc:5000, nodePort 30500)
   ▲                     ▲
   │                     └── builder/registry scripts (BUILDER_SCOPE,
   │                          GitHub App token, BuildMy-house org regex)
   └── builder-manager-token (SA), k8s API (rbac SA), GitHub, hive

hermes-gateway ──► hive, hermes-build-dispatcher:8000, browser-adversary:8000,
                   postgres, Steward MCP (buildmyhouse.stewardacs.xyz),
                   Axiom, Discord, LLM providers, k8s API (mcp server)
```

**Secret fan-in:** `company-ops-secrets` feeds hive, all 3 engineering
deployments, both dispatchers, hermes, browser-adversary, postgres, backups,
dns-healthcheck **and** pm-agent-gateway (`k8s/base/pm-agent/pm-agent-gateway.yaml:47-51`) —
the one place pm-agent's isolated posture still touches the shared secret.
`pm-agent` itself uses only `pm-agent-secrets`.

## Suggested parameterisation points

1. **hive** — `System.get_env("ENGINEERING_A2A_URL", "http://engineering-agent:8001")`
   in `engineering.ex` (fixes the dead manifest var); `HIVE_NAME`/`HIVE_URL` in
   `router.ex:11-13`; assert on env in `router_test.exs:27`.
2. **pm-agent** — `${HIVE_URL}` interpolation in `hermes-config.yaml:70`
   (pattern already used for `${PM_DATABASE_URL}` there); move the refusal/brand
   strings out of `guard.py`/`SOUL.md` into a per-project content file.
3. **git org** — `GITHUB_ORG` env in `sync-repo.sh` (two URL fallbacks) and in
   `builder-manager-mcp.js:279` regex; make `context_ref` in `:642` come from
   env (`COMPANY_OS_REPO` / `SOURCE_REPO`).
4. **steward URL** — `${STEWARD_MCP_URL}` in `hermes/config.yaml:142`; the
   `generate-agent-mcp-config.js:103` and plugin defaults already honour env.
5. **registry** — make `REGISTRY_HOST`/`REGISTRY_PORT` env in
   `builder-manager-mcp.js:134` (mirror `registry-manager-mcp.js:11`); keep the
   nodePort in the overlay, not the code.
6. **postgres** — one `POSTGRES_DB` value referenced by
   `k8s/base/postgres/postgres.yaml`, `scripts/init-postgres.sh`,
   `scripts/provision-db-roles.sh`.
7. **manifests** — a per-project kustomize overlay varying exactly four tokens:
   namespace `company-ops`, secret `company-ops-secrets`, image host
   `localhost:30500`, DB `homely_company`. Code needs no fork for these.
8. **telemetry** — env-guard `_DATASET`/ingest URL in both hermes plugins (hive
   and `axiom_client` already read env).
