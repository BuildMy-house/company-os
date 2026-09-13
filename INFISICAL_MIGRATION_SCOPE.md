# Infisical Migration Scope: Secrets Infrastructure

**Status:** Infrastructure-only scope document (NOT implementation). Code changes identified but not committed. Actual Infisical account setup is a Nahar-only action.

## Problem

Current state: production `.env` files contain plaintext secrets (API keys, DB passwords, Discord tokens, GitHub App private keys, AWS/R2 credentials) that are:
1. World-readable inside containers (`chmod 644` by default)
2. Included in multiple copies across the repo (e.g., `company-ops/.env`, `buildmyhouse/.env` for the app)
3. Only protected by gitignore, not by any access-control boundary
4. Manually rotated when exposed (no programmatic rotation/audit trail)

Found this session: two Steward MCP bearer tokens exposed — one via public GitHub push to `.mcp.json`, a second untracked in `opencode.json`/`worker/opencode.json`.

Goal: Infisical as the single source of truth for all secrets, with access control enforced at the API level (not file permissions).

## Proposed Architecture

### Layer 1: Infisical Projects (API-Level Access Control)

Create two separate Infisical projects, each with its own environment:

1. **Hermees-Operational Secrets** (project: `buildmy-house-ops`)
   - Path: `/secrets/hermes` + `/secrets/agents` + `/secrets/services`
   - Machine identity: `hermees-runtime` (scoped to read only these paths)
   - Contents: Discord bot token, model API keys, Postgres role connection strings (`hermes_company`, `hermes_observer_writer`, `hermes_analytics`), Axiom query token, OpenCode credentials
   - Used by: Hermes gateway container startup, engineering-manager spawned processes, scheduled observers
   - **Explicit non-access:** Hermees identity cannot read `/secrets/infra/*`

2. **Infrastructure-Critical Secrets** (project: `buildmy-house-infra`)
   - Path: `/secrets/infra/*` (TLS certificates, Cloudflare API tokens, DNS registrar credentials, GitHub App private keys, R2 access keys, Stripe keys if added later)
   - Machine identity: `infra-provisioner` (only Nahar's personal token/key can access, or a dedicated operator identity)
   - **Hermees has zero access, structurally, not by convention**

### Layer 2: Runtime Injection (Replace File-Based .env)

Three integration points:

1. **Docker Container Startup**
   - `company-ops/scripts/entrypoint.sh`: use `infisical run --token $INFISICAL_TOKEN -- <cmd>` to wrap the actual service startup, injecting secrets as environment variables at runtime
   - `.env` files become **read-only config templates** only (schema/defaults, no real secrets), never sources of truth
   - Alternative (if `infisical run` is too heavyweight): use Infisical CLI to fetch secrets at startup → temp env-var sourcing → clear temp file
   
2. **Engineering Container Worker Dispatch**
   - Worker processes spawned by `ai-cli-mcp` need their own scoped Infisical access
   - Option A: `engineering-entrypoint.sh` mints a short-lived Infisical access token for this session, passes it to the worker
   - Option B: Each worker gets a per-session service account (created/destroyed with the worker)
   - Both options maintain separation: workers can only read `/secrets/hermes/*`, never `/secrets/infra/*`

3. **MCP Server Registration**
   - `@infisical/mcp-server` (official npm package, if available) registered as an MCP server for Hermes → Claude → workers
   - Hermees asks for a secret → Claude dispatches a fetch through the MCP → Infisical returns it (or denies per policy)
   - Fallback: CLI-based fetching via `infisical get /path/to/secret --token $TOKEN`

### Layer 3: What Code Changes

**No application logic changes.** Only secrets *delivery* changes. The code already reads:
- `os.environ.get("DISCORD_BOT_TOKEN")` — will work whether TOKEN comes from .env or Infisical
- `os.environ.get("ANTHROPIC_API_KEY")` — same

Changes are infrastructure/deployment only:

1. **`company-ops/scripts/entrypoint.sh`**
   - Wrap startup with Infisical injection mechanism
   - Example: `infisical run --token $INFISICAL_TOKEN -- hermes gateway run`
   - OR: fetch all secrets into a temp env file, source it, delete it immediately

2. **`company-ops/scripts/engineering-entrypoint.sh`**
   - Similar: inject secrets for worker spawning
   - Ensure each spawned worker can only reach `/secrets/hermes/*`

3. **`company-ops/.env.example`** (documentation only, no secrets)
   - Add comments showing which secrets live in Infisical paths
   - Example: `# Set via Infisical /secrets/hermes/discord; leave empty here`
   - `DISCORD_BOT_TOKEN=` (empty, will be injected)

4. **CI/CD Secrets Rotation** (future ticket, not in scope)
   - A scheduled job that rotates short-lived tokens (e.g., Anthropic API key for fresh dispatch each month)
   - Audit trail: every rotation is logged in Infisical with timestamp, operator, rotation reason

5. **GitHub Actions / Deploy Secrets** (if applicable)
   - If a GitHub Actions workflow needs a secret, fetch it from Infisical at job time (via CLI or MCP) instead of storing in GitHub Secrets
   - Reduces credential surface (one store, not GitHub + Infisical duplicates)

### Layer 4: Constitutional Boundary (Policy)

Add to `company-ops/policies/autonomy.md` (under hard limits):

> **Hermees cannot access infrastructure-critical secrets** (TLS certificates, PKI material, registrar/DNS credentials, payment processor keys, GitHub OAuth/App keys for Nahar's account). These live in a separate Infisical project with explicit access policy excluding Hermees's machine identity, enforced at the API level. Even if a request, prompt, or incident claims urgency, this boundary is non-negotiable — no code change, no "temporary override" will grant access.

## Out of Scope for This Ticket

1. **Actual Infisical account/workspace setup** — only Nahar can do this; this document is the spec he'll follow
2. **Credential rotation policies** — defined separately in an on-call runbook once infra is live
3. **Audit logging/compliance export** — Infisical has built-in audit; specific export/reporting is a later ticket if needed
4. **Emergency access procedures** — a separate "if Infisical is down" runbook, outside this scope
5. **Migrating existing plaintext secrets in Git history** — out of scope; a separate `git filter-branch` / BFG operation if ever needed

## Implementation Steps (for future dispatch)

1. **Infisical account created** (Nahar action; outside this ticket)
   - Two projects (`buildmy-house-ops`, `buildmy-house-infra`)
   - Three environments each (dev, staging, production)
   - Machine identities configured

2. **Code changes dispatched** (one ticket)
   - `entrypoint.sh` wrappers for Infisical injection
   - `.env.example` updated with Infisical path notes
   - Docker build/startup tested in a fresh `docker-compose up`
   - VERIFY: secrets injected, not read from file

3. **MCP server integration** (separate ticket, dependent on step 2)
   - `@infisical/mcp-server` (or equivalent) installed
   - Registered in `hermes/config.yaml`
   - Verified: Hermes can query a secret via MCP, gets it correctly

4. **Worker dispatch safety test** (separate ticket)
   - Spawn a worker via engineering-manager
   - Worker attempts to access `/secrets/hermes/*` — succeeds
   - Worker attempts to access `/secrets/infra/*` — fails at Infisical API level
   - VERIFY: policy boundary enforced structurally

5. **Rotate initial secrets** (Nahar action; outside tickets)
   - All plaintext tokens in current `.env` → new Infisical values
   - Old .env files deleted or sanitized
   - Audit trail: Infisical records who set each secret and when

## Why This Design

**Hermees-only path** separates concerns: builders/agents operate normally, operators handle cert/key/payment infrastructure separately, no risk of a prompt injection or confused-deputy attack escalating to PKI/payment access.

**No code changes required** means "only secrets are the variable missing" — infrastructure is ready to run once Nahar provides the Infisical workspace URL and a provisioner token.

**MCP server path** lets coding agents query secrets on demand (e.g., "what's the current Postgres version?" → agent fetches it via Infisical MCP) without embedding secrets in tickets/logs, and audit trail is automatic.
