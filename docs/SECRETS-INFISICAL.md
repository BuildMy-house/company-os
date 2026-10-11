# Secrets — Infisical Bootstrap & Inventory

Infisical is the single source of truth for every company-os secret. On a new
host the ONLY manually provisioned secret is the Infisical bootstrap machine
identity; everything else is pulled by `scripts/bootstrap-secrets.sh`
(static k8s Secrets) and then kept live by the Infisical Secrets Operator
(`k8s/infisical-sync.yaml`, `k8s/buildmyhouse-dev-infisical-sync.yaml`).

- Infisical host: `https://eu.infisical.com`, environment `dev`
- Project **Build My house** (`8806c2b0-73d2-4bea-8537-5b874c5ff592`) — company-os runtime
- Project **buildmyhouse-app** (`58b43b81-effb-4392-a937-46f2448efb78`) — app runtime (buildmyhouse-dev namespace)

Verified against live Infisical (key names only) and the live cluster on 2026-10-10.

## Inventory (key names only — never store values here)

All `Build My house` paths below merge into the `company-ops-secrets` Secret
(namespace `company-ops`), which every workload consumes via `envFrom`
(single-source pattern already used by all manifests). Dedicated secrets:
`company-ops-steward-*` and `buildmyhouse-dev-secrets`.

| Key | Lives today | Consumed by | In Infisical? | Target path |
|---|---|---|---|---|
| POSTGRES_PASSWORD | company-ops-secrets | postgres.yaml, entrypoint.sh | yes | Build My house `/hermes` |
| COMPANY_PASSWORD, OBSERVER_PASSWORD, ANALYTICS_PASSWORD, PM_AGENT_WRITER_PASSWORD (each role's `password_key` in `sql/roles.d/*.yaml`) | read by `scripts/provision-db-roles.sh` from env or Infisical | role provisioning (see below) | yes | `/hermes` (NOT `/infra`) |
| COMPANY_DATABASE_URL, OBSERVER_DATABASE_URL, ANALYTICS_DATABASE_URL | company-ops-secrets | hermes-gateway, engineering pods | yes | `/hermes` |
| PM_DATABASE_URL | company-ops-secrets (orphaned — no live sync source) | company_ops/pm_store.py, pm-agent/agent.py | **NO — gap** | `/hermes` |
| DISCORD_BOT_TOKEN | company-ops-secrets | hermes-gateway | yes | `/hermes` |
| DISCORD_ALLOWED_CHANNELS, DISCORD_ALLOW_ALL_USERS, DISCORD_ANNOUNCE_CHANNEL, DISCORD_BLOG_CHANNEL, DISCORD_HIL_CHANNEL, DISCORD_FINANCE_CHANNEL, DISCORD_DM_USER | company-ops-secrets | hermes-gateway, company_ops/human_interface.py | yes | `/hermes` |
| NOUS_API_KEY | company-ops-secrets | hermes model inference | yes | `/hermes` |
| API_SERVER_KEY | company-ops-secrets | hermes api_server platform, hermes-messenger-mcp.js | yes | `/infra` |
| GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID | company-ops-secrets | hermes/engineering build dispatchers, github-app-token.js | yes | `/infra` |
| GITHUB_APP_PRIVATE_KEY (= certs/buildmyhouse-engineering-app.pem) | company-ops-secrets as env var; certs/ file on dev host only for local docker tests | github-app-token.js (inline env preferred), test-engineering-container.sh | yes | `/infra` |
| CLAUDE_CODE_OAUTH_TOKEN | company-ops-secrets | ai-cli / Claude Code dispatch in engineering pods | yes | `/infra` |
| OPENCODE_GO_API_KEY, ZAI_CODING_PLAN_API_KEY, TOKENROUTER_API_KEY | company-ops-secrets | opencode model providers | yes | `/infra` |
| STEWARD_TOKEN, STEWARD_URL | company-ops-secrets | Steward ACS MCP (all agents) | yes | `/infra` (hermes has its own STEWARD_TOKEN in `/hermes`) |
| company-ops-steward-engineering-agent → STEWARD_TOKEN | dedicated Secret | engineering-agent pod | yes | `/infra/steward/engineering-agent` |
| company-ops-steward-engineering-opencode → STEWARD_TOKEN | dedicated Secret | engineering-opencode pod | yes | `/infra/steward/engineering-opencode` |
| company-ops-steward-engineering-opencode-direct → STEWARD_TOKEN | dedicated Secret | engineering-opencode-direct pod | yes | `/infra/steward/engineering-opencode-direct` |
| AXIOM_TOKEN, AXIOM_ORG_ID | company-ops-secrets | company_ops/axiom_client.py, OTLP telemetry (engineering-entrypoint.sh) | yes | `/` + `/infra` |
| AXIOM_DASHBOARD_TOKEN | company-ops-secrets | Axiom dashboard sharing | yes | `/` |
| AXIOM_DATASET | local .env only (live Secret has orphaned copy) | axiom_client.py, engineering-manager-mcp.js, opencode-plugins/axiom-usage.js | **NO — gap** | `/hermes` |
| AXIOM_ENDPOINT | local .env only | axiom_client.py | **NO — gap** | `/hermes` |
| BUILDMYHOUSE_APP_URL, BUILDMYHOUSE_MCP_URL, BUILDMYHOUSE_MCP_BEARER_TOKEN, BUILDMYHOUSE_TEST_EMAIL, BUILDMYHOUSE_TEST_PASSWORD (= certs/buildmyhouse-test-account.env) | company-ops-secrets | buildmy.house MCP test account (AGENTS.md contract) | yes | `/` |
| R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_API_TOKEN | company-ops-secrets | pg-backup.sh, git-backup.sh (R2 backups) | yes | `/infra` |
| CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN | company-ops-secrets | Cloudflare/DNS ops | yes | `/infra` |
| NEON_API_KEY | company-ops-secrets | Neon infra ops | yes | `/infra` |
| VITE_AXIOM_TOKEN | company-ops-secrets | app telemetry ingest | yes | `/infra` |
| INFISICAL_HOST_URL, INFISICAL_PROJECT_ID, INFISICAL_ENV, INFISICAL_SECRET_PATH | local .env (non-secret config; ids also committed in sync manifests) | fetch-infisical-secrets.js, entrypoints | n/a — config, not secrets | hard-coded defaults in scripts |
| INFISICAL_UNIVERSAL_AUTH_CLIENT_ID, INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET | the one manual bootstrap credential | Infisical operator (via `infisical-bootstrap-credentials` Secret), fetch script | no — it authenticates TO Infisical | provisioned by hand, then seeded as k8s Secret `infisical-bootstrap-credentials` (keys: `clientId`, `clientSecret`) in `company-ops` + `buildmyhouse-dev` |
| builder-manager-token (ca.crt, namespace, token) | k8s token controller | builder-manager-mcp.js in engineering/dispatcher pods | no — cluster-issued SA token, not portable | recreated by `kubectl apply` of k8s/builder-rbac.yaml |
| buildmyhouse-dev-deployer-token (ca.crt, namespace, token) | k8s token controller | buildmyhouse-dev-deployer CronJob | no — cluster-issued SA token | recreated by `kubectl apply` of k8s/buildmyhouse-dev-deployer-rbac.yaml |
| buildmyhouse-dev-secrets (JWT_SECRET, ANALYTICS_ADMIN_TOKEN, BUILDMYHOUSE_MCP_TOKEN, LUXCORE_WORKER_TOKEN, R2_ACCOUNT_ID/R2_BUCKET_NAME/R2_PUBLIC_URL/R2_S3_ENDPOINT, VITE_AXIOM_DATASET/VITE_AXIOM_ENDPOINT, RESEND_*, plus app-side AXIOM_/CLOUDFLARE_/NEON_/R2_ keys) | dedicated Secret in buildmyhouse-dev | buildmyhouse-dev app + deployer CronJob | yes | buildmyhouse-app project `/` |
| browser-adversary-auth | referenced optional:true in browser-adversary.yaml; absent live | browser-adversary auth mount | no keys defined yet | out of scope until keys are defined; then `/infra/browser-adversary` |
| mcp-workers.json | tracked in git, verified identical to mcp-workers.example.json (commands/models only — no secret values) | company_ops/cli.py worker subcommand | n/a — contains no secrets | if tokens are ever added: `/infra` + env injection, never the JSON file |

## Adding a database role (agent)

Roles are declared one-per-file in `sql/roles.d/<role>.yaml` (format in
`sql/roles.d/README.md`) and reconciled by `scripts/provision-db-roles.sh`.
To add an agent: (1) add the manifest file; (2) add one Infisical key in
`/hermes` named by its `password_key` (URL-safe, `openssl rand -hex 24`)
plus its `dsn_key` DSN; (3) run the script (`--dry-run`, then `--only <role>`).
The script refuses to run if any selected role's password is empty or a
placeholder, so a blank secret can never clear a live role's password.

## Gap list — add these to Infisical (Nahar action, Infisical UI/CLI)

Project **Build My house**, environment **dev**, folder **`/hermes`** — create these three keys:

1. `AXIOM_DATASET`
2. `AXIOM_ENDPOINT`
3. `PM_DATABASE_URL`

Until added, `AXIOM_DATASET`/`AXIOM_ENDPOINT` reach pods only via the legacy
local `.env` path (scripts/k3s-local-up.sh, not usable on a new host), and
`PM_DATABASE_URL` survives in the live Secret only as drift from an older sync.

Optional additions if those features are used:
`DISCORD_ALLOWED_USERS`, `DISCORD_HOME_CHANNEL` (forwarded by entrypoint.sh
when set; currently empty everywhere). Note: entrypoint.sh also forwards
`STEWARD_MCP_URL`, but the real key is `STEWARD_URL` — entrypoint-side naming
drift, harmless (loop skips unset keys), owned by another ticket.

## New-host bootstrap procedure

Prereqs: kubectl (kubeconfig for the new cluster), node 18+, jq.

1. In Infisical, create a Universal Auth machine identity for company-os with
   read access to both projects (`Build My house`, `buildmyhouse-app`).
2. **The only manual secret provisioning step** — export the bootstrap identity:

   ```sh
   export INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=...     # identity clientId
   export INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=... # identity clientSecret
   # optional overrides: INFISICAL_HOST_URL, INFISICAL_ENV
   ```

3. Run the bootstrap (idempotent; `--dry-run` prints target Secrets and key
   names only, with zero cluster/Infisical access):

   ```sh
   scripts/bootstrap-secrets.sh
   ```

   It creates the namespaces, seeds `infisical-bootstrap-credentials`
   (`clientId`, `clientSecret`) into `company-ops` + `buildmyhouse-dev`,
   fetches everything else from Infisical and applies the k8s Secrets
   (`company-ops-secrets`, `company-ops-steward-*`,
   `buildmyhouse-dev-secrets`), applies the RBAC manifests so the token
   controller recreates the SA-token Secrets, and applies the live-sync
   manifests if the operator is already installed.

4. Install the Infisical Secrets Operator (once, if not present):

   ```sh
   helm repo add infisical-helm-charts https://dl.cloudsmith.io/public/infisical/helm-charts/helm.chart.repo
   helm install operator-namespaced infisical-helm-charts/secrets-operator \
     -n company-ops --create-namespace \
     --set scopedNamespaces="{company-ops,buildmyhouse-dev}" --set scopedRBAC=true
   kubectl apply -f k8s/infisical-sync.yaml -f k8s/buildmyhouse-dev-infisical-sync.yaml
   ```

   From then on the operator owns those Secrets (refreshInterval 60s) and the
   static seeds are superseded. Re-running `scripts/bootstrap-secrets.sh`
   remains safe at any time.

5. Deploy workloads (`kubectl apply -k k8s` plus the buildmyhouse-dev manifests)
   and restart pods so they pick up envFrom values.

**File-type secrets:** no pod file mounts are needed. The GitHub App private
key travels as the `GITHUB_APP_PRIVATE_KEY` env var (inline PEM —
github-app-token.js prefers the env var over any file path) and the test-account
values as `BUILDMYHOUSE_*` env vars, both inside `company-ops-secrets` via the
existing `envFrom` pattern. Host copies under `certs/` are only needed for
local docker tests (test-engineering-container.sh) and can be re-materialized
from Infisical when required. `mcp-workers.json` needs no migration (no
secrets in it).

**Verification (names only):**

```sh
scripts/bootstrap-secrets.sh --dry-run
kubectl get secret -n company-ops
kubectl get secret company-ops-secrets -n company-ops -o json | jq -r '.data|keys[]'
```

## Rotation

- Rotate values **in Infisical only** (UI/CLI). The operator propagates
  changes into the synced k8s Secrets within `refreshInterval: 60s`; restart
  affected pods to re-read env vars (values are captured at container start).
- Secrets on a host without the operator (static seeds) update only by
  re-running `scripts/bootstrap-secrets.sh` after the Infisical change.
- Bootstrap identity rotation: create a new Universal Auth identity, update
  `infisical-bootstrap-credentials` in both namespaces (re-run
  `scripts/bootstrap-secrets.sh` with the new vars exported), then disable the
  old identity in Infisical.
- Keys removed from Infisical keep lingering in an operator-owned Secret until
  it is deleted and re-synced — `PM_DATABASE_URL` today is exactly that drift;
  after the gap-list import it resolves normally.
