# Homely Company Ops

Small, portable control-plane foundation for the autonomous Homely workflow.
It keeps exact company state in Postgres (the `company`/`observer` schemas) and emits separate JSONL telemetry. The
R2, Slack, Hermes, Claude, and OpenCode integrations are intentionally
adapters/configuration points until credentials and deployment policy exist.

## Runtime direction

Hermes is the primary agent and the only conversational entry point:

```text
user -> Hermes CEO -> native MCP tools -> OpenCode / other workers
```

Hermes owns planning, memory, approvals, budgets, and delegation. OpenCode is
an execution worker exposed through `@kud/mcp-opencode`; it must not replace
Hermes as the user-facing agent.

## Quick start

Spin up a local Postgres first (see `company-ops/scripts/test-db-up.sh` for
the docker-compose setup and role creation):

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
export COMPANY_DATABASE_URL=postgresql://hermes_company:localtest_company@localhost:5544/homely_company
python -m company_ops --db "$COMPANY_DATABASE_URL" init
python -m unittest discover -s tests -v
python -m company_ops --db "$COMPANY_DATABASE_URL" status
```

The CLI also supports `plan`, `charge`, `revenue`, `route`, `telemetry`,
`worker`, `deployment`, `resource-pool`, and `dispatch_capture`.

Copy `mcp-workers.example.json` to `mcp-workers.json`. It uses the public
`@kud/mcp-opencode` for all three replaceable roles. This means the current
Claude and OpenCode roles both run through OpenCode, while Hermes uses the
free/cheap model slot. Change only the `command`, `tool`, or `model` under a
role when you later move that role to Claude.
Preview a dispatch without executing it:

```sh
python -m company_ops worker seo "Draft an SEO improvement" --config mcp-workers.json
```

Add `--execute` only when the configured MCP worker is trusted. The client
uses stdio JSON-RPC, sends one task, polls asynchronous Claude sessions when
configured, enforces a timeout, and returns the MCP result as structured JSON.
Permission requests fail closed; expand `allowedTools` only after reviewing
the server and repository scope.

## Data boundaries

- Postgres (the `company` schema) is the source of truth for plans, actions,
  credits, revenue, and provider usage.
- JSONL telemetry is optimization evidence, never financial reporting data.
- Git stores policies and human-readable records; large artifacts belong in an
  S3-compatible store such as Cloudflare R2.
- R2 settings are placeholders in `.env.example`; no network calls
  happen in the local implementation.

`company.finance_expenses` tracks real recurring costs (Claude
subscription, server hosting, OpenCode paid usage) entered manually via
`company-ops expense add <category> <amount_cents> <recurrence>`.
`company.finance_revenue` exists as a schema placeholder but stays
empty — payment processing (Stripe or similar) is deliberately deferred
until there is something to sell, not forgotten.

`company.resource_pools` tracks subscription/quota-based tool usage
across providers (OpenCode, Codex, Antigravity CLI, future Claude).
Each pool has a stable caller-supplied `pool_id` (e.g.
`opencode-go-weekly`), a `level` for the dispatch hop it tracks
(default `claude_to_worker`), and period/quota metadata:

```sh
# Record a pool (e.g. OpenCode Go paid $30/week budget)
python -m company_ops resource-pool record opencode-go-weekly opencode-go dollars 30.00 weekly \
  2026-09-07T00:00:00+00:00 2026-09-14T00:00:00+00:00 \
  --consumed 12.50 --source "manual check"

# Check pool status (quota, consumed, remaining fractions, thin flag)
python -m company_ops resource-pool status opencode-go-weekly

# List all pools
python -m company_ops resource-pool list
```

## Dispatch capture

Record real per-dispatch token usage from the three CLI tools (opencode, codex,
agy/Antigravity) into `company.provider_usage` and optionally update a matching
resource pool's `consumed_amount`:

```sh
# Capture from a saved opencode run output file
python -m company_ops.dispatch_capture opencode run-output.txt \
  --model mimo-v2.5-free --db "$COMPANY_DATABASE_URL"

# Capture from codex with a resource pool update
python -m company_ops.dispatch_capture codex turn-log.txt \
  --model o4-mini --resource-pool-id opencode-go-weekly

# Capture from agy
python -m company_ops.dispatch_capture agy result.json \
  --model glm-5.3-flash
```

The parsers sum token counts across multiple events in a session (opencode and
codex can emit several step/turn events per run). Dollar cost is taken from the
CLI output when available; otherwise `policy.model_cost()` estimates it for
known models. Resource pool updates are best-effort and never prevent the
underlying `provider_usage` record from being written.

## Portable container

```sh
docker compose build
docker compose run --rm company-ops status
```

The image installs Hermes and includes a credential-free provider config;
provide `TOKENROUTER_API_KEY` through `.env` at runtime. The MCP package is
downloaded by `npx` only when a worker is actually executed. Mount the
repository and provide secrets at runtime. Do not bake credentials into the
image. `scripts/git-backup.sh` commits and pushes a backup branch when
the checkout has a configured GitHub remote.

Configure the Discord bot account in `.env` using `.env.example`, then
start the user-facing Hermes CEO gateway with:

```sh
docker compose up
```

For the first setup, run `docker compose run --rm company-ops hermes gateway
setup` and choose Discord. The gateway responds to DMs and, by default, only
mentioned messages in guild channels. Keep `DISCORD_ALLOWED_USERS` restricted
to you.

The default CEO identity is in `hermes/SOUL.md`; it is planning-only until
execution policies and credentials are deliberately enabled.

## Policy defaults

The starting daily allowance is 100 credits. Costs and the 70% net-revenue
allocation live in `company_ops/policy.py` and are versioned with the repo.
Provider cost is recorded separately, so free OpenCode capacity is preferred
but never treated as guaranteed.

The current model assignment is TokenRouter MiMo for Hermes and OpenCode Zen
MiMo-V2.5 Free for the engineering manager/worker
TokenRouter GLM 5.3 Flash for the Claude/OpenCode roles. Keep the API key in
runtime secrets; Nous Research is reserved as a future provider swap.

## Staging promotion

Promote a commit to staging by running the promotion script. It starts a test
Postgres, runs pytest, and records the deployment in the company ledger only
if all tests pass.

```sh
./company-ops/scripts/staging-promote.sh <commit-hash>
```

Options:
- `--dry-run` — run tests without recording the deployment.

The script uses the company-ops test database (`test-db-up.sh`) and requires
a Python venv at `.venv` with `company-ops[test]` installed.

To update an existing deployment's status:

```sh
python -m company_ops deployment update DEPLOY-<id> deployed --result "all tests passed"
```

Deployments are recorded in the `company.deployments` table with columns:
`id`, `environment`, `version`, `status`, `created_at`, `result`.

### Rollback

Previous versions are recoverable via git alone — every deployment recorded in
`company.deployments` has a `version` column that stores a git commit hash, so
reverting is always just `git checkout <that-commit>` (or equivalent). The
ledger itself does not keep a copy of the artifact.

To roll back a bad staging deployment:

1. Find the last-known-good commit hash:

   ```sh
   python -c "
   import os, psycopg
   c = psycopg.connect(os.environ['COMPANY_DATABASE_URL'])
   with c.cursor() as cur:
       cur.execute(\"SELECT id, version, status FROM company.deployments
         WHERE environment = 'staging' ORDER BY created_at DESC LIMIT 5\")
       for r in cur.fetchall(): print(r)
   "
   ```

2. Re-promote the good commit (this re-validates and re-records it):

   ```sh
   ./company-ops/scripts/staging-promote.sh <previous-good-commit>
   ```

3. Mark the bad deployment as rolled back in the ledger:

   ```sh
   python -m company_ops deployment update DEPLOY-<bad-id> rolled_back \
     --result "regression in <describe>"
   ```

This is deliberately NOT a bespoke rollback orchestrator — it is git commit
history (already recoverable) plus the existing deployment CLI (already built),
following the project's "don't overbuild infrastructure" convention.
