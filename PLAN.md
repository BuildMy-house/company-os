# Company Ops Build Plan — Hermees V0 (Postgres backbone + Human Interface)

Distinct claim board for the `company-ops/` workstream (autonomous-company
instrumentation for Hermees/BuildMy.house). This is **not** the Homely app
board — do not add these tickets to the root `PLAN.md`, and Homely tickets
never land here. Spec: `/home/nahar/.claude/plans/ticklish-conjuring-horizon.md`
(read that file for full phase descriptions; this board only carries the
phases actually being executed).

Every ticket is dispatched to an opencode worker and independently verified
by the manager (Claude) before being marked `done` — the manager never edits
`company_ops/` source directly. Concurrent opencode dispatches are capped at
3 at any time.

**Standing rule (2026-09-06, applies to every ticket on this board from here
forward): never deprecate-in-place.** When a ticket replaces or removes a
system/library/schema/config, its Definition of Done requires *full removal*
of the old thing in the same change — no commented-out code, no "kept for
reference" stubs, no dual-documented paths, no leftover state/session files,
no unused env vars left in `.env.example`.

**Credential boundary:** no live Neon Postgres project, no pgEdge account,
exists yet. Nothing in this batch invents or guesses those credentials.
Everything is built/tested against a local/dev Postgres (docker-compose) and
exact SQL for Nahar to run once he has real infra lives in `company-ops/sql/`.
Every blocked/deferred item is tracked in `company-ops/NAHAR-TODO.md`.

> Claim rule: to claim a ticket, edit ONLY your row (Claimed-by + Status),
> commit `board: claim <TICKET-ID>`. Status moves `todo → claimed →
> in_progress → review → done`. Only the manager (Claude) sets `done`, after
> independent verification.

## Claim Board

| Ticket | Title | Deps | Owner paths | Phase | Claimed-by | Status | Notes |
|--------|-------|------|--------------|-------|------------|--------|-------|
| P1-A | Company/Observer PG schema + role grants (SQL only) | — | company-ops/sql/ | 1 | opencode | done | commit 1ddc95c; independently re-verified: idempotent re-run + Postgres-level permission boundary proven (observer_writer INSERT ok/UPDATE+DELETE denied; analytics SELECT ok/INSERT denied) |
| P1-B | Local/dev Postgres test harness + env convention + psycopg dep | — | company-ops/docker-compose.test.yml, company-ops/scripts/test-db-up.sh, company-ops/scripts/test-db-down.sh, company-ops/.env.example, company-ops/pyproject.toml | 1 | opencode | done | commit 330fb47; independently re-ran test-db-up.sh/down.sh + verified all 3 role connection strings connect + psycopg installs in throwaway venv |
| P1-C | pgEdge analytics MCP placeholder wiring | — | company-ops/hermes/config.yaml | 1 | opencode | done | commit 37a9000 + fix 32... (see P1-C2) — pgedge_analytics stanza correct; two unrelated regressions it introduced (matrix display block, opencode_manager env paths) reverted by P1-C2 |
| P1-C2 | Fix-up: revert P1-C scope leakage (matrix display block, opencode_manager env paths) | P1-C | company-ops/hermes/config.yaml | 1 | opencode | done | re-verified 2026-09-06: commit 37a9000 is a clean, single-file diff (only adds pgedge_analytics stanza) — no regression present in HEAD; matrix display block and opencode_manager env paths in config.yaml are unmodified from before P1-C and correct as-is. No redispatch needed; closed by direct manager inspection, not a worker dispatch. |
| P1-D | ledger.py + cli.py: sqlite3 → psycopg (storage swap only) | P1-A, P1-B | company_ops/ledger.py, company_ops/cli.py, tests/test_ops.py | 1 | opencode | done | superseded by P1-D2's redo (commit 0145efb); see P1-D2 for verification detail |
| P1-D2 | Fix-up redo: undo P1-D's snapshot-table invention, restore --db flag, uniform txn error handling, delete invented test_ledger.py | P1-D | company_ops/ledger.py, company_ops/cli.py, sql/company_schema.sql (snapshots-table removal only), tests/test_ledger.py (delete) | 1 | opencode | done | commit 0145efb. Independently re-verified 2026-09-06: read ledger.py/cli.py diff directly — no snapshots-table remnants anywhere (grep clean across sql/py), --db flag restored (not --dsn), uniform try/except/rollback in every write method, tests/test_ledger.py deleted, tests/test_ops.py present. Delegated DB-backed test run to a separate opencode task against live local Postgres: 8/8 test_ops.py tests passed (0 failed/errored); --db init and --db status both returned valid JSON with no traceback; zero collateral file edits confirmed via git status. |
| P1-E | observer.py: append-only Observer PG writer module | P1-A, P1-B | company_ops/observer.py, tests/test_observer.py | 1 | opencode | done | commit eec965d; independently re-ran full test suite against live Postgres (11/11 pass incl. permission-boundary tests) |
| P2-F | Matrix → Discord bridge swap (hard delete of Matrix) | P1-B | company-ops/discord_bridge.py, company-ops/matrix_bridge.py (delete), company-ops/hermes-matrix.service (delete), company-ops/hermes-discord.service (new), company-ops/.matrix_session.json (delete), company-ops/.matrix_store/ (delete), company-ops/Dockerfile.bridge, company-ops/.env.example, company-ops/README.md, company-ops/pyproject.toml, tests/test_discord_bridge.py | 2 | opencode | done | commit 5115d33 (bridge swap) + commit 54d89a5 (DM allow-list bypass fix). Independently re-verified 2026-09-06: diff for 54d89a5 is a clean 2-file, 42-line change (discord_bridge.py + tests/test_discord_bridge.py only, confirmed via git diff --stat) adding is_user_allowed() and gating the is_dm branch on it, leaving the existing guild/mention is_allowed() check untouched. Ran tests/test_discord_bridge.py directly in a throwaway venv: 24/24 passed incl. 4 new IsUserAllowedTests. Grepped tracked tree for 'matrix' (case-insensitive, .py/.json/.md/.service/.yml/.env*): zero hits outside this PLAN.md's own historical notes; the only 'matrix' remnants anywhere (__pycache__/matrix_bridge.cpython-312.pyc, .venv/.../matrix_nio dist-info) are gitignored, untracked local artifacts, not part of the repo. |
| P2-G | Structured Human Interface (ask_information/ask_judgment/request_approval/request_action) | P1-E, P2-F | company_ops/human_interface.py, tests/test_human_interface.py, company-ops/hermes/SOUL.md | 2 | opencode | done | commit 175b731. Independently re-verified 2026-09-06: read human_interface.py directly — matches spec exactly (urllib-only Discord REST, search-before-ask scoped to ask_information only via find_prior_answer, all 4 typed calls write the correct human_requests type, complete_human_interface_request thin wrapper). Read test_human_interface.py directly — substantive assertions, not shallow: cache-hit test confirms zero send calls AND zero new rows written (row count stays at 2: original+completion), each type-specific test confirms the DB row's type column. Ran tests myself in a throwaway venv against live local Postgres: 18/18 passed (7 new human_interface tests + 11 test_observer.py, zero regressions). Diff confirmed touches only the 3 owner paths (203+178+62 lines, all new/additive, SOUL.md's existing paragraphs untouched). Documented simplification (matching incoming Discord replies to open requests) correctly deferred, not attempted, per ticket scope. |

## Phase 9 — Testing rigor + CI/CD + canary deployment (this run)

Dispatched by a fresh manager instance, 2026-09-06. GitHub remote
launch-readiness gap turned out to already be resolved (origin existed, `gh`
authenticated) — the real issue was 138 unpushed commits; pushed this run,
`ci.yml` now runs on push for real. Two production Discord gaps found during
audit (DISCORD_DM_USER unset, DISCORD_ALLOW_ALL_USERS no-op) and site-homely's
broken/non-live deployment are recorded in `company-ops/NAHAR-TODO.md`, not
tickets — they need Nahar's real values/policy call, not code.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| P9-A | Add company-ops test job to ci.yml (Postgres service container + pytest) | — | .github/workflows/ci.yml | opencode | done | commit 1e840eb. Independently re-verified: diff is exactly 1 file (+52/-0), ADD-only (homely/equivalence jobs untouched). Worker deviated from my ticket's suggested port 5432 -> used 5544 + password localtestpw -- checked myself: test_observer.py/test_human_interface.py have a hardcoded `SUPERUSER_DSN` at exactly that port/password, so this was a necessary correct fix, not drift. Ran the DoD myself (not just trusting the commit message): reused the already-running company-ops-test-postgres container, installed into a scratch venv, ran `pytest tests/ -q` with the two TEST_*_DATABASE_URL vars -- 52 passed, 0 failed, 0 skipped, matches worker's own claim exactly. |
| P9-B | company-ops staging-container promotion path + `deployment` CLI verb | P1-A/B/D2/E | company-ops/scripts/staging-promote.sh, company_ops/cli.py, company_ops/ledger.py, tests/test_ops.py, company-ops/README.md | opencode | done | commits 525f0e7 + b6347bf. Fix-up (b6347bf) independently re-verified by a fresh manager instance: read the diff myself (exactly staging-promote.sh, +13/-1, adds `TEST_OBSERVER_DATABASE_URL` export matching test-db-up.sh's actual role/password exactly, plus a skip-count gate that exits non-zero on any skipped test). Re-ran the real script myself (`staging-promote.sh testcommit123 --dry-run`, reused the already-running company-ops-test-postgres container): 52 passed, 0 failed, 0 skipped, exit 0 -- confirms the previously-skipped 18 Observer/Human-Interface tests now actually execute (34+18=52 matches exactly). No fake deployment recorded (used --dry-run). |
| P9-C | Homely pre-release/beta channel via tag-pattern-driven `prerelease` flag | — | .github/workflows/release.yml, docs/RELEASE.md | opencode | done | commit 7928be1. Independently re-verified: diff is exactly the 2 owner files, +211/-0. Validated YAML directly (`python3 -c "import yaml; yaml.safe_load(...)"` -> valid). Traced the exact bash regex myself (not trusting worker's claim) against 4 sample tags: `v0.9.9-beta.1`->true, `v0.9.9-rc.2`->true, `v0.9.9`->false, `v0.9.9-beta10` (no dot)->false, correct. RELEASE.md's stale remote prerequisite fixed, new beta section inserted before "Cutting a release", rest of file untouched. Side finding during this verification: release.yml, flatpak.yml, snap.yml were all untracked/never pushed before now (gh workflow list only showed CI+Dependency Graph) -- flatpak.yml/snap.yml committed separately as pure bookkeeping (commit 566c97c, no content changes, read both first to confirm safe/tag-gated). |
| P9-D | Purge leftover Matrix references in Dockerfile/entrypoint.sh/docker-compose.yml/hermes/config.yaml (never-deprecate-in-place violation found in audit) | P2-F | company-ops/Dockerfile, company-ops/docker-compose.yml, company-ops/scripts/entrypoint.sh, company-ops/hermes/config.yaml | opencode | done | commit a7b83db. Independently re-verified: diff is exactly the 4 owner files (+30/-14). Pre-existing uncommitted infra changes (opencode-ai install, /opt/data paths, curl dep) preserved unchanged. Cross-checked entrypoint.sh's new DISCORD_* passthrough list directly against `grep os.environ discord_bridge.py` myself -- exact 1:1 match, all 15 vars it reads are present, no extras invented. config.yaml's dead `platforms.matrix` stanza removed (worker's documented choice: no discord-equivalent added since top-level display settings already apply globally and nothing else ever overrode them). `docker compose -f company-ops/docker-compose.yml config` validates OK. Tree-wide case-insensitive matrix grep outside company-ops (release.yml build-matrix syntax, docs-matrix/equivalence-matrix naming, matrixWorld.determinant() Three.js call) -- all confirmed false positives, no real Matrix-chat leftovers anywhere. |
| P9-F | site-homely: fix/rename Cloudflare deploy workflow + document gradual/staged deployment procedure | — | site-homely/.github/workflows/*.yml, site-homely/wrangler.jsonc (new), site-homely/docs/DEPLOY.md (new) | opencode | in_progress | separate git repo (github.com/NaharEmet/homely-site.git); cannot go live without CLOUDFLARE_API_TOKEN/ACCOUNT_ID (tracked in company-ops/NAHAR-TODO.md item 6) — build config+docs only |

## Phase 3-8 (drafted, not dispatched this run)

See bottom of file for drafted ticket text once Phase 1-2 land. Do not
dispatch until explicitly resumed in a future run.

## Readiness/consistency pass (2026-09-06, fresh manager instance)

Audit found: SOUL.md and config.yaml's pgedge_analytics stanza are both
already internally consistent with what P1/P2 actually landed (verified by
direct read, no fix needed). mcp-workers.json's shared-free-model routing is
still accurately documented as a bootstrapping placeholder in README.md
(lines 34-38). Found genuinely stale docs: README.md still describes SQLite
as the storage layer (P1-D2 migrated ledger.py to Postgres/psycopg months
before this pass) in 3 places incl. a rollback snippet that literally calls
`sqlite3.connect()` on a Postgres schema-qualified table name (broken, not
just stale-sounding); `.env.example` has a dead `COMPANY_OPS_DB=
company-ops.sqlite3` var (zero references anywhere in code, confirmed via
grep) and is missing `PGEDGE_API_KEY=`, which `hermes/config.yaml`'s
pgedge_analytics stanza already reads via `${PGEDGE_API_KEY}` with nowhere
documented to set it.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| P-DOC1 | Fix stale SQLite references in README.md + .env.example env-var drift | — | company-ops/README.md, company-ops/.env.example | opencode | done | commit b934987. Independently re-verified: grep -in sqlite README.md / grep COMPANY_OPS_DB .env.example both zero hits, grep PGEDGE_API_KEY .env.example exactly 1 hit. Read diff directly: quick-start's new `postgresql://hermes_company:localtest_company@localhost:5544/homely_company` example cross-checked byte-for-byte against scripts/test-db-up.sh's actual printed connection string -- exact match, not invented. Rollback snippet now uses working psycopg against COMPANY_DATABASE_URL instead of the broken sqlite3.connect() call. Only the 2 owner files touched. |

## Phase 3-4 (this run, resumed per plan's own note that Phase 3-9 can proceed in parallel once Phase 1 schemas exist)

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| P3-A | Real cost tracking: finance_expenses/finance_revenue tables, expense CLI verb, ads action cost | P1-A/B/D2 | company-ops/sql/company_schema.sql, company_ops/ledger.py, company_ops/cli.py, company_ops/policy.py, company-ops/README.md, tests/test_finance.py (new) | opencode | todo | dispatching now, parallel with P4-A (disjoint files) |
| P4-A | Observer provenance: typed decision/prediction/experiment/relationship helpers on ObserverWriter | P1-E | company_ops/observer.py, tests/test_observer.py | opencode | todo | dispatching now, parallel with P3-A (disjoint files); observer.decisions/predictions/experiments/relationships tables already exist from P1-A, no schema change needed |

## Note for whoever writes the Phase 6 ticket (policies/autonomy.md doesn't exist yet)

Per the plan's "Secrets and certificate management (Infisical, hard exclusion
for Hermees)" section (added 2026-09-06, see the plan file), when Phase 6's
`company-ops/policies/autonomy.md` is actually written, it MUST include this
constitutional boundary line verbatim alongside the other hard limits (spend
caps, no debt, no direct App PG access, etc.):

> Hermees cannot access certificate/PKI material under any circumstances,
> regardless of what other operational secrets it holds.

This is a hard exclusion, not a convention — same "boundary enforced at the
infrastructure layer, not just code discipline" principle as the Phase 1
Observer Postgres role. Corresponding account-setup item tracked in
`company-ops/NAHAR-TODO.md` Group E (Infisical).

## Backup mechanism (new, per architecture correction 2026-09-06)

Production Postgres is now self-hosted/local, not Neon (see NAHAR-TODO.md
Group B1's correction) — "constantly running, needs real backups" is a
concrete gap, not implied by anything already landed.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| P1-F | Scheduled pg_dump (company+observer schemas) -> Cloudflare R2 backup, with retention | P1-A/B | company-ops/scripts/pg-backup.sh (new), company_ops/backup.py (new), tests/test_backup.py (new), company-ops/pyproject.toml, company-ops/docs/BACKUP.md (new), company-ops/.env.example | opencode | todo | dispatching now, parallel with P3-A/P4-A (disjoint files; deliberately does NOT touch cli.py or README.md, both currently in-flight) |

## Manager container — Hermees's path to the Claude engineering manager (new, fresh manager instance 2026-09-06)

Spec: `/home/nahar/.claude/plans/ticklish-conjuring-horizon.md`, "Manager/worker
configurability" section, final paragraph. Goal: replace the `claude` role's
current OpenCode placeholder in `mcp-workers.json` with a real
`@steipete/claude-code-mcp` wrapper running in its own dedicated container, so
Hermees can dispatch to the identical Claude engineering-manager persona
Nahar already uses manually. This is additive/isolated infra work — does NOT
touch `company_ops/ledger.py`, `cli.py`, `policy.py`, `observer.py`,
`sql/company_schema.sql` (owned by a concurrent Phase 3/4/9 manager instance's
in-flight tickets).

Pre-dispatch audit findings (this manager instance, 2026-09-06):
- `codex` CLI here is installed as a standalone binary
  (`~/.codex/packages/standalone/...`, symlinked from `~/.local/bin/codex`),
  NOT an npm global — but `@openai/codex` IS a real, current npm package
  (`npm view @openai/codex version` -> 0.153.4) and is the correct install
  path for a Dockerfile. `opencode` is already installed via
  `npm install -g opencode-ai@latest` in the main `Dockerfile` — mirror that
  exactly. `claude` is `@anthropic-ai/claude-code` (npm, confirmed
  2.1.263 locally).
- The naive "clone github.com/BuildMy-house/app at build time" approach
  researched in the plan is a real option (repo is public, confirmed via
  `gh repo view`) BUT is stale as of this audit: that repo's last commit is
  2026-09-06T11:36:10Z, while the LOCAL working tree has since-modified,
  uncommitted changes to `.claude/agents/opencode-manager.md` (234
  insertions, e.g. the `Skill` tool grant) and two entirely untracked/
  never-pushed directories, `.claude/skills/` and `.agents/skills/`
  (frontend-design + impeccable installs from earlier this session). A
  git-clone-based Dockerfile would silently ship a stale/incomplete
  persona. Do not use that approach as primary.
- Recommended instead: **Docker Buildx additional build contexts**
  (`--build-context name=path`, supported — confirmed `docker buildx
  v0.36.1` installed) pointed directly at the live host paths at build
  time: `--build-context manager-def=../.claude/agents --build-context
  skills-src=../.agents/skills`, then in `Dockerfile.manager`:
  `COPY --from=manager-def opencode-manager.md /opt/company-ops/.claude/agents/opencode-manager.md`
  and `COPY --from=skills-src . /opt/company-ops/.claude/skills/` (flatten
  into `.claude/skills/`, since that's the path OpenCode/Claude actually
  discover skills from — no need to preserve the host's
  `.claude/skills/frontend-design -> ../../.agents/skills/frontend-design`
  symlink, `.agents/skills/` already has the real directories for both
  skills). This always builds from current on-disk state, needs no GitHub
  push, and avoids making the ~7.6GB `house_designer` root (no
  `.dockerignore` there) the primary build context.
- Whether the *operational* `workFolder` for `claude_code` tool calls
  should be a live-mounted `/workspace/house_designer` (mirroring the main
  `docker-compose.yml` service's existing `..:/workspace/house_designer`
  volume) vs. the image's frozen build-time copy is a real design
  decision — the manager's whole job is dispatching against live current
  repo state, so a live volume mount is almost certainly correct for
  `workFolder`, with the Buildx-copied `.claude/agents/opencode-manager.md`
  + skills serving only as a documented fallback for a standalone/no-mount
  deployment. Ticket text below directs the worker to decide and document
  this, not guess silently.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| P-MGR1 | Dockerfile.manager: containerize the Claude engineering-manager (claude+opencode+codex CLIs + claude-code-mcp wrapper) for Hermees's `mcp-workers.json` claude role | — | company-ops/Dockerfile.manager (new), company-ops/docker-compose.yml, company-ops/mcp-workers.json, company-ops/mcp-workers.example.json, company-ops/.env.example, company-ops/NAHAR-TODO.md | codex | dispatching | see ticket prompt in dispatch log; does not touch ledger.py/cli.py/policy.py/observer.py/sql/company_schema.sql (other instance's territory) |

**Containment bug caught before landing (2026-09-06, coordinator catch, not self-found):**
first Codex dispatch's `docker-compose.yml` draft for `claude-manager` had
`volumes: - ..:/workspace/house_designer` — a live bind-mount of the actual
host `house_designer` working tree into a container running
`@steipete/claude-code-mcp` with `--dangerously-skip-permissions` (required
for headless operation). That combination defeats the whole point of
"contained to its own container" as the accepted mitigation for the
bypass-permissions risk — a bug/bad action there would write to the real
repo instantly, no review step. Caught and flagged before any `docker
build`/commit happened (independently confirmed: that dispatch's `docker
build` actually failed on its own first, on a sandbox Docker-socket
permission error, and its commit attempt failed on a `.git/index.lock`
read-only-filesystem error — so nothing unsafe ever actually ran, but the
design was wrong regardless and needed fixing before redispatch). Corrected
design dispatched: the container clones its own isolated copy into a named
Docker volume (`manager-checkout`, not a host bind-mount) via a new
`company-ops/scripts/manager-entrypoint.sh`, using the same
`certs/homely-deploy` SSH key mount the existing `company-ops` service
already uses; changes reach the real repo only via explicit `git push` from
inside that isolated clone. `mcp-workers.json`/`.example.json`'s `claude`
role `workFolder` updated to `/workspace/app-checkout` to match. In flight,
not yet verified.

**Second architectural correction (2026-09-06, coordinator-driven research, not self-found):**
confirmed by reading `company_ops/mcp_client.py`/`workers.py` directly that
`mcp-workers.json` is NOT wired to `hermes gateway run` at all — the vendor
tool reads its own native `hermes/config.yaml`'s `mcp_servers:` block
instead, which already supports both `command:`/stdio entries (see
`opencode_manager`) and `url:`/remote HTTP-SSE entries. `mcp-workers.json`
is being reverted to its original state (serves a separate manual-dispatch/
Discord-bridge use case, out of scope for this ticket). New standing
pattern (Nahar's call): every department is its own container exposing MCP
over the network, one `url:` entry per department in `hermes/config.yaml`.
For the engineering department's `@steipete/claude-code-mcp` (stdio-only,
no native HTTP mode), front it with `supergateway` inside the container.
Also renamed the service/files from "claude-manager"/"manager" to
"engineering" throughout (this container holds both the Claude
engineering-manager and the opencode/codex CLI workers it dispatches to,
not just "the manager"): `Dockerfile.manager` -> `Dockerfile.engineering`,
`manager-entrypoint.sh` -> `engineering-entrypoint.sh`, compose service
`claude-manager` -> `engineering`, volume `manager-checkout` ->
`engineering-checkout`. Isolated-clone-not-live-mount fix from the prior
round is preserved unchanged. Redesign dispatched to Codex
(background task, in flight). Nothing committed yet — holding per
standing verification discipline until this lands and is independently
checked.
