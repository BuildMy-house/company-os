# Homely Company Ops

Small, portable control-plane foundation for the autonomous Homely workflow.
It keeps exact company state in SQLite and emits separate JSONL telemetry. The
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

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
python -m company_ops --db company-ops.sqlite3 init
python -m unittest discover -s tests -v
python -m company_ops --db company-ops.sqlite3 status
```

The CLI also supports `plan`, `charge`, `revenue`, `route`, `telemetry`, and
`worker`.

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

- SQLite is the local source of truth for plans, actions, credits, revenue, and
  provider usage.
- JSONL telemetry is optimization evidence, never financial reporting data.
- Git stores policies and human-readable records; large artifacts belong in an
  S3-compatible store such as Cloudflare R2.
- R2 settings are placeholders in `.env.example`; no network calls
  happen in the local implementation.

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
