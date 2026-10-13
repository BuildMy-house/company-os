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
| P1-A | Company/Observer PG schema + role grants (SQL only) | — | company-ops/sql/ | 1 | opencode | done | commit 1ddc95c; independently re-verified: idempotent re-run + Postgres-level permission boundary proven (observer_writer INSERT… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-a)) |
| P1-B | Local/dev Postgres test harness + env convention + psycopg dep | — | company-ops/docker-compose.test.yml, company-ops/scripts/test-db-up.sh, company-ops/scripts/test-db-down.sh,… | 1 | opencode | done | commit 330fb47; independently re-ran test-db-up.sh/down.sh + verified all 3 role connection strings connect + psycopg installs in throwaway… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-b)) |
| P1-C | pgEdge analytics MCP placeholder wiring | — | company-ops/hermes/config.yaml | 1 | opencode | done | commit 37a9000 + fix 32... (see P1-C2) — pgedge_analytics stanza correct; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-c)) |
| P1-C2 | Fix-up: revert P1-C scope leakage (matrix display block, opencode_manager env paths) | P1-C | company-ops/hermes/config.yaml | 1 | opencode | done | re-verified 2026-09-06: commit 37a9000 is a clean, single-file diff (only adds pgedge_analytics stanza) — no regression present in HEAD; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-c2)) |
| P1-D | ledger.py + cli.py: sqlite3 → psycopg (storage swap only) | P1-A, P1-B | company_ops/ledger.py, company_ops/cli.py, tests/test_ops.py | 1 | opencode | done | superseded by P1-D2's redo (commit 0145efb); see P1-D2 for verification detail (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-d)) |
| P1-D2 | Fix-up redo: undo P1-D's snapshot-table invention, restore --db flag, uniform txn error handling, delete… | P1-D | company_ops/ledger.py, company_ops/cli.py, sql/company_schema.sql (snapshots-table removal only),… | 1 | opencode | done | commit 0145efb. Independently re-verified 2026-09-06: read ledger.py/cli.py diff directly — no snapshots-table remnants anywhere (grep… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-d2)) |
| P1-E | observer.py: append-only Observer PG writer module | P1-A, P1-B | company_ops/observer.py, tests/test_observer.py | 1 | opencode | done | commit eec965d; independently re-ran full test suite against live Postgres (11/11 pass incl. permission-boundary tests) (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-e)) |
| P2-F | Matrix → Discord bridge swap (hard delete of Matrix) | P1-B | company-ops/discord_bridge.py, company-ops/matrix_bridge.py (delete), company-ops/hermes-matrix.service… | 2 | opencode | done | commit 5115d33 (bridge swap) + commit 54d89a5 (DM allow-list bypass fix). (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p2-f)) |
| P2-G | Structured Human Interface (ask_information/ask_judgment/request_approval/request_action) | P1-E, P2-F | company_ops/human_interface.py, tests/test_human_interface.py, company-ops/hermes/SOUL.md | 2 | opencode | done | commit 175b731. Independently re-verified 2026-09-06: read human_interface.py directly — matches spec exactly (urllib-only Discord REST,… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p2-g)) |
| P2-I | Fix missing User-Agent header in human_interface.py Discord REST client (production-breaking bug, found live 2026-09-07) | P2-G | company_ops/human_interface.py, tests/test_human_interface.py | 2 | opencode (opencode/mimo-v2.5-free) | done | commit 0bf2c54. Found by the manager directly, not a worker: a REAL live end-to-end test of ask_information() against production Discord… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p2-i)) |
| P2-J | Switch Human Interface outbound messages from DM to #hil server channel | P2-I | company_ops/human_interface.py, company-ops/.env.example, tests/test_human_interface.py, company-ops/PLAN.md,… | 2 | antigravity | done | 2026-09-07: switched all 4 typed calls from user DM to #hil server channel (ID 1546470198825975961) via DISCORD_HIL_CHANNEL env var. (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p2-j)) |
| P1-G | Production Postgres deploy (docker-compose service + init SQL + role passwords) + fix dead pgedge_analytics… | P1-A, P1-B | company-ops/docker-compose.yml, company-ops/scripts/entrypoint.sh, company-ops/hermes/config.yaml,… | 1 | opencode | done | 2026-09-07: commit 9a894e7 (opencode/mimo-v2.5-free, 2 corrective rounds needed — first round used a host-exposed port 5432 and the… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-g)) |

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
| P9-A | Add company-ops test job to ci.yml (Postgres service container + pytest) | — | .github/workflows/ci.yml | opencode | done | commit 1e840eb. Independently re-verified: diff is exactly 1 file (+52/-0), ADD-only (homely/equivalence jobs untouched). (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p9-a)) |
| P9-B | company-ops staging-container promotion path + `deployment` CLI verb | P1-A/B/D2/E | company-ops/scripts/staging-promote.sh, company_ops/cli.py, company_ops/ledger.py, tests/test_ops.py,… | opencode | done | commits 525f0e7 + b6347bf. Fix-up (b6347bf) independently re-verified by a fresh manager instance: read the diff myself (exactly… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p9-b)) |
| P9-C | Homely pre-release/beta channel via tag-pattern-driven `prerelease` flag | — | .github/workflows/release.yml, docs/RELEASE.md | opencode | done | commit 7928be1. Independently re-verified: diff is exactly the 2 owner files, +211/-0. (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p9-c)) |
| P9-D | Purge leftover Matrix references in Dockerfile/entrypoint.sh/docker-compose.yml/hermes/config.yaml… | P2-F | company-ops/Dockerfile, company-ops/docker-compose.yml, company-ops/scripts/entrypoint.sh,… | opencode | done | commit a7b83db. Independently re-verified: diff is exactly the 4 owner files (+30/-14). (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p9-d)) |
| P9-F | site-homely: fix/rename Cloudflare deploy workflow + document gradual/staged deployment procedure | — | site-homely/.github/workflows/*.yml, site-homely/wrangler.jsonc (new), site-homely/docs/DEPLOY.md (new) | codex | in_progress | claimed 2026-09-07 for real deployment/build-status verification; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p9-f)) |

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
| P-DOC1 | Fix stale SQLite references in README.md + .env.example env-var drift | — | company-ops/README.md, company-ops/.env.example | opencode | done | commit b934987. Independently re-verified: grep -in sqlite README.md / grep COMPANY_OPS_DB .env.example both zero hits, grep PGEDGE_API_KEY… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-doc1)) |

## Phase 3-4 (this run, resumed per plan's own note that Phase 3-9 can proceed in parallel once Phase 1 schemas exist)

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| P3-A | Real cost tracking: finance_expenses/finance_revenue tables, expense CLI verb, ads action cost | P1-A/B/D2 | company-ops/sql/company_schema.sql, company_ops/ledger.py, company_ops/cli.py, company_ops/policy.py,… | opencode (tokenrouter/z-ai/glm-5.3-free) | done | commit 132a42e; manager read full diff (exact match to add_tool_request/add_deployment ledger pattern, idempotent SQL, ads cost tier… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p3-a)) |
| P4-A | Observer provenance: typed decision/prediction/experiment/relationship helpers on ObserverWriter | P1-E | company_ops/observer.py, tests/test_observer.py | opencode (tokenrouter/z-ai/glm-5.3-free) | done | commit 7618c93; manager read full diff (6 typed methods matching record_human_request pattern exactly, append-only, correct relationship… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p4-a)) |
| P5-A | Observer autonomy instrumentation: typed record_failure/record_recovery/record_autonomy_event helpers,… | P4-A, P2-G | company_ops/observer.py, tests/test_observer.py, sql/observer_schema.sql (append-only),… | 5 | opencode (opencode/mimo-v2.5-free) | done | commit fe0c209. Confirmed real gap by direct read before dispatch: observer.failures/recoveries/autonomy_events/human_minutes tables… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p5-a)) |
| P3-B | Resource-pool accounting: company.resource_pools table + resource_pools.py module + `resource-pool` CLI verb… | P1-A/B | company-ops/sql/company_schema.sql, company_ops/resource_pools.py (new), company_ops/cli.py,… | opencode (opencode/mimo-v2.5-free) | done | 2026-09-07: first attempt (commit ed19809) diff-read by manager and found to substantively diverge from spec -- wrong column names… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p3-b)) |
| P3-C | Wire record_provider_usage() capture from real opencode/codex/agy dispatch JSON output (per-CLI parsing; | P3-B | company-ops/company_ops/dispatch_capture.py (new), tests/test_dispatch_capture.py (new), company-ops/README.md | opencode (opencode/mimo-v2.5-free) | dispatching | fresh manager instance 2026-09-07; does not touch ledger.py/observer.py/policy.py/resource_pools.py/backup.py/cli.py/sql/company_schema.sql… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p3-c)) |
| P3-E | Verify existing finance expense CLI against production Postgres | P3-A | company-ops/company_ops/ledger.py, company-ops/company_ops/cli.py, company-ops/tests/test_finance.py | Codex | blocked | Claimed 2026-09-07; finance table/module/CLI already exist. (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p3-e)) |
| P3-D | Get P3-C's usage-capture mechanism a real path from INSIDE the engineering container into Company PG… | P3-C | company-ops/Dockerfile.engineering, company-ops/scripts/engineering-entrypoint.sh,… | -- | todo | Queued 2026-09-07 by manager instance handling P3-B/P3-C, per coordinator/Nahar catch: NOT dispatched this run, deliberately left for… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p3-d)) |

## Note for whoever writes the Phase 6 ticket (policies/autonomy.md doesn't exist yet)

| P6-A | Add the git-tracked research/memory workflow and constitutional autonomy policy | — | company-ops/memory/{research,hypotheses,experiments,learnings,decisions,playbooks}/, company-ops/policies/autonomy.md | Codex | done | Claimed and completed 2026-09-07. Added six substantive workflow READMEs and the constitutional policy, including all hard boundaries and… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p6-a)) |

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

## Phase 10 — Hermes provider fallback repair

| Ticket | Title | Deps | Owner paths | Phase | Claimed-by | Status | Notes |
|--------|-------|------|-------------|-------|------------|--------|-------|
| P10-A | Route Hermes conversation fallback to Z.AI Coding Plan GLM-5.3-Flash | — | hermes/config.yaml | 10 | 2_Chris | done | Commits `ab5be80` (main), `eb049a5` (prod). (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p10-a)) |
| P10-B | Replace retired `upstage/solar-pro4:free` (Nous) default — confirmed dead live (`openai.NotFoundError 404:… | — | hermes/config.yaml | 10 | 2_Alice | review | Root-caused + live-verified by the manager before dispatch (2026-10-07): queried Nous's real `/v1/models` listing via `kubectl exec… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p10-b)) |
| P10-C | Hermes plugin: detect a confirmed-permanent primary-model retirement and raise a Steward task with a vetted… | P10-B | hermes-plugins/model_retirement_watch/ (new: `__init__.py`, `plugin.yaml`),… | 10 | 2_Alice | review | Root-caused by the manager before dispatch (2026-10-07), via live read of the vendored Hermes agent package inside `hermes-gateway`… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p10-c)) |

## Phase 8 — Monthly spend review ritual (recurring Human Interface ritual)

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|---|---|---|---|---|---|---|
| P8-A | Monthly spend review ritual: report compilation + `spend-review` CLI verb + Human Interface integration | P3-A, P2-G | company-ops/company_ops/spend_review.py, company-ops/company_ops/cli.py, tests/test_spend_review.py | antigravity | done | 2026-09-07: report-compilation (`spend_review.py`) queries `company.finance_expenses`, `company.provider_usage`, and… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p8-a)) |

## Backup mechanism (new, per architecture correction 2026-09-06)

Production Postgres is now self-hosted/local, not Neon (see NAHAR-TODO.md
Group B1's correction) — "constantly running, needs real backups" is a
concrete gap, not implied by anything already landed.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| P1-F | Scheduled pg_dump (company+observer schemas) -> Cloudflare R2 backup, with retention | P1-A/B | company-ops/scripts/pg-backup.sh (new), company_ops/backup.py (new), tests/test_backup.py (new),… | opencode (tokenrouter/z-ai/glm-5.3-free) | done | 2026-09-07: prior-session code (backup.py/pg-backup.sh/test_backup.py/BACKUP.md, all untracked) read in full by manager -- correct as… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-f)) |
| P1-F2 | Real pg_dump & restore verification against production + 7-day retention default + R2 credential gap documentation | P1-F | company-ops/company_ops/backup.py, company-ops/.env.example, company-ops/NAHAR-TODO.md, company-ops/PLAN.md,… | antigravity | done | 2026-09-07: Verified end-to-end against live production Postgres (company-ops-postgres-1). (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p1-f2)) |

## Manager container — Hermees's path to the Claude engineering manager (new, fresh manager instance 2026-09-06)

Spec: `/home/nahar/.claude/plans/ticklish-conjuring-horizon.md`, "Manager/worker configurability" section, final paragraph. Goal: replace the `claude` role's current OpenCode placeholder in `mcp-workers.json` with a real `@steipete/claude-code-mcp` wrapper running in its own dedicated container, so Hermees can…

Full narrative moved verbatim to [docs/archive/PLAN-ticket-notes-2026-10-13.md](docs/archive/PLAN-ticket-notes-2026-10-13.md#section-narrative-manager-container--hermeess-path-to-the-claude-engineering-manager-new-fresh-manager-instance-2026-09-06) on 2026-10-13.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| P-MGR1 | Dockerfile.manager: containerize the Claude engineering-manager (claude+opencode+codex CLIs + claude-code-mcp… | — | company-ops/Dockerfile.manager (new), company-ops/docker-compose.yml, company-ops/mcp-workers.json,… | codex | done (superseded) | see ticket prompt in dispatch log; does not touch ledger.py/cli.py/policy.py/observer.py/sql/company_schema.sql (other instance's territory). (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr1)) |
| P-MGR2 | Extend engineering container to clone/sync all 5 BuildMy-house repos (app, website, company-os, hermees,… | P-MGR1 | company-ops/scripts/engineering-entrypoint.sh, company-ops/docker-compose.yml (engineering service only),… | codex | done | builds on top of P-MGR1's currently-uncommitted engineering-entrypoint.sh/docker-compose.yml already in the working tree (P-MGR1 itself… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr2)) |
| P-MGR3 | Fix-up: Dockerfile.engineering missing git + openssh-client (container has no git binary, entrypoint's… | P-MGR1, P-MGR2 | company-ops/Dockerfile.engineering | opencode | done | found by manager during live docker-build+run verification of P-MGR2: base image node:22-bookworm-slim ships neither git nor openssh-client; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr3)) |
| P-MGR4 | Fix-up: swap archived `@steipete/claude-code-mcp` for maintained `ai-cli-mcp` in engineering-entrypoint.sh | P-MGR1 | company-ops/scripts/engineering-entrypoint.sh, company-ops/NAHAR-TODO.md | opencode | done | coordinator-flagged mid-P-MGR2-dispatch: `@steipete/claude-code-mcp` (last push 2026-05-15) is archived/abandoned; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr4)) |
| P-MGR5 | Add Gemini CLI + register `ai-cli-mcp` as an MCP server for both `claude` and `codex` CLIs inside the… | P-MGR3 | company-ops/Dockerfile.engineering, company-ops/NAHAR-TODO.md, company-ops/scripts/engineering-entrypoint.sh… | opencode | done | Manager-verified 2026-09-07: commit 826adff is a clean 2-file diff (Dockerfile.engineering + NAHAR-TODO.md), zero 'gemini' references… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr5)) |

## Live-testing note (2026-09-07) — discord_bridge.py run for real

Per Nahar's explicit direction (accepted risk, private single-user Discord server): started `discord_bridge.py` as a genuine long-running process for Nahar to interact with directly (not a bounded ~25s test window). Not a board ticket — this is an operational run, logged here for the record.

Full narrative moved verbatim to [docs/archive/PLAN-ticket-notes-2026-10-13.md](docs/archive/PLAN-ticket-notes-2026-10-13.md#section-narrative-live-testing-note-2026-09-07--discordbridgepy-run-for-real) on 2026-10-13.


## Live-testing note (2026-09-07, later same day) — real ask_information() E2E test against production

Per explicit user authorization ("do that also" in response to being asked to send a real Discord test message via the bot and write a live test row to the production Observer database), ran a genuine end-to-end test of P2-G's `ask_information()` against production, not a mock:

Full narrative moved verbatim to [docs/archive/PLAN-ticket-notes-2026-10-13.md](docs/archive/PLAN-ticket-notes-2026-10-13.md#section-narrative-live-testing-note-2026-09-07-later-same-day--real-askinformation-e2e-test-against-production) on 2026-10-13.


## Full plan audit (2026-09-07, fresh manager instance)

Independent status pass through `/home/nahar/.claude/plans/ticklish-conjuring-horizon.md` phase by phase, cross-checked against actual repo state (source files, git log, `sql/*.sql`, `NAHAR-TODO.md`) rather than trusted from board text alone. A concurrent manager instance (working resource-pool tracking + P1-F backup)…

Full narrative moved verbatim to [docs/archive/PLAN-ticket-notes-2026-10-13.md](docs/archive/PLAN-ticket-notes-2026-10-13.md#section-narrative-full-plan-audit-2026-09-07-fresh-manager-instance) on 2026-10-13.

| # | Capability | Real status |
|---|---|---|
| 1 | Agent organization | Running (opencode-manager loop). Codex/Antigravity confirmed usable as workers. Engineering-container department path now real: `hermes/config.yaml`'s `engineering_manager: url: http://engineering:8000/sse` entry exists on disk, is functionally correct (self-test passing, see below), and **is now committed** (commit c94bf8b, 2026-09-07) — compose topology re-checked at commit time (`engineering` and `company-ops` share Compose's implicit default network, `engineering` exposes port 8000, so hostname/port are internally consistent; exact vendor YAML schema still unconfirmed, per the config's own inline comment). Board's P-MGR1 row corrected from stale `dispatching` to `done (superseded)`, pointing at P-MGR2-5 for the actual delivered container work. |
| 2 | Company OS (memory/policies/constitution/self-mod safety) | Self-modification safety mechanism landed this session (`test-engineering-container.sh` + `docs/DEPLOY-ENGINEERING.md`, commit e521db0, see below). Memory workflow (`company-ops/memory/{research,hypotheses,...}/`) and `policies/autonomy.md` (Phase 6) **do not exist** — confirmed via `ls`, zero hits. Not started. |
| 3 | Company PG | Schema/roles done (Phase 1). Finance (P3-A) done. Resource-pool accounting (P3-B) landed this run by the concurrent instance (commits ed19809 + a correction b583d84 — still being iterated as of this audit, not this instance's territory to verify). GA4/GSC/Clarity/web ingestion (Phase 7) not started — no `web.*`/`marketing.*` tables exist in `sql/company_schema.sql`. |
| 4 | Observer PG | Hard boundary done and re-verified (Phase 1). Typed decision/prediction/experiment/relationship helpers done (P4-A, commit 7618c93). Autonomy-dimension SQL views (Phase 5) **not built** — `grep -rn "CREATE VIEW" sql/*.sql` returns zero hits anywhere in the schema. |
| 5 | Human Interface | 4 typed calls + search-before-ask done (P2-G). Discord bridge live-tested for real (2026-09-07 log entry). **Public-rooms two-gate split (conversation vs. dispatch-triggering) confirmed NOT built** — read `discord_bridge.py` directly: both the DM path (`is_user_allowed`) and the mention path (`is_allowed`) gate identically before ever reaching `run_worker`; there is no separate conversational-only path. Correctly tracked as NAHAR-TODO Group A item A3, not silently dropped. |
| 6 | Experiment engine | Only the append-only `observer.experiments` table + a typed `record_experiment`-style helper exist (part of P4-A). The actual experiment-engine CLI verbs (`experiment start/measure/decide`) and the Company-PG-side `experiments.experiments/metrics/results` evidence tables described in Phase 4 **do not exist** — `grep -n "add_parser" company_ops/cli.py` shows no `experiment` subparser. **Not started**, despite Phase 4's Observer-provenance half being done — these are two different halves of Phase 4 and only one landed. |
| 7 | Economic system | Finance expenses/revenue (P3-A) + resource pools (P3-B, in flight) done/in-flight. Monthly spend-review ritual (Phase 8) not started — no cron/scheduler code found referencing it. |
| 8 | Product control | Running for Homely via the existing opencode-manager loop (unchanged, this is the root `PLAN.md`, not `company-ops/PLAN.md`) — not yet Hermees-initiated (Hermees has no live decision-loop wired to actually call it yet, only the MCP path exists). |
| 9 | Website control | Not started — `site-homely` has no Hermees-driven control loop, only a manually-dispatched P9-F ticket (see Phase 9 below). |
| 10 | Marketing framework | Not started — zero `marketing.*` tables, no channel/content/campaign schema. |
| 11 | Organizational learning | Resource-pool pacing (part of Phase 5's spirit) landed this run. Engineering-container self-modification safety (canary/candidate/previous convention) landed this session — see Phase 9 below. Broader autonomy-dimension views/evals still not started. |
| 12 | Observer from Day 1 | Still true, unchanged since Phase 1. |

## Engineering-container self-test fix-up verified + isolated baseline confirmed (2026-09-07, same session, post-interruption)

Fix-up commit `e2908ed` (timeout 90s→240s + `/proc`-based process-check fallback before declaring FAIL, dispatched to `opencode/mimo-v2.5-free`, same free tier) landed. Read the diff directly: exactly the described change (22 lines), crash-detection path (`ENTRIES=2`) untouched, matches the fix-up ticket precisely.

Full narrative moved verbatim to [docs/archive/PLAN-ticket-notes-2026-10-13.md](docs/archive/PLAN-ticket-notes-2026-10-13.md#section-narrative-engineering-container-self-test-fix-up-verified--isolated-baseline-confirmed-2026-09-07-same-session-post-interruption) on 2026-10-13.

| P-MGR6 | Fix supergateway crash on second/reconnecting SSE client against `engineering` MCP endpoint | P-MGR5 | company-ops/scripts/engineering-entrypoint.sh (final `exec` line), company-ops/hermes/config.yaml… | opencode | done | Manager-verified 2026-09-07: chose option (b), swapped `supergateway` for `mcp-proxy` in `engineering-entrypoint.sh`'s final exec line… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr6)) |
| P-MGR7 | Diagnose inconsistent repo-clone SSH timeouts (`app`/`company-os` intermittently time out while… | P-MGR6 (done) | company-ops/scripts/engineering-entrypoint.sh (`sync_repo()` function and its WARN messages only -- do NOT… | opencode | done | Coordinator-verified 2026-09-07 (board row was stale at "dispatching"; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr7)) |
| P-MGR8 | Switch engineering container repo access to GitHub App auth (retire SSH deploy key) | P-MGR7 | company-ops/scripts/github-app-token.js (new), company-ops/scripts/engineering-entrypoint.sh,… | antigravity | review | Landed commit `8275f92`. Implemented RS256 JWT installation token minting via `github-app-token.js` (zero external deps, Node 22 built-in… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr8)) |
| P-MGR9 | Register Axiom MCP server (mcp-server-axiom) for Claude, Codex, OpenCode, and Hermes | P-MGR8 | company-ops/Dockerfile.engineering, company-ops/docker-compose.yml, company-ops/hermes/config.yaml,… | antigravity | claimed | Register mcp-server-axiom as axiom MCP server across engineering container (Claude, Codex, OpenCode), OpenCode configs, and Hermes gateway; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr9)) |
| P-MGR10 | Pin all Hermes auxiliary task slots (except vision) to the funded TokenRouter free model +… | P-MGR9 | company-ops/hermes/config.yaml (new top-level `auxiliary:` block only) | opencode (zai-coding-plan/glm-5.3-flash) | done | Root cause: aux slots default `provider: auto`, probing unconfigured/unfunded nous/openrouter providers — churn in gateway logs plus real… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr10)) |
| P-MGR11 | Fix Axiom llm_call events showing 0 tokens (hermes axiom_usage plugin mapping bug) + explicit `role` tagging… | — | hermes-plugins/axiom_usage/__init__.py, scripts/opencode-plugins/axiom-usage.js,… | opencode (zai-coding-plan/glm-5.3-flash) | review | Two already-root-caused fixes landed. (1) 0-tokens bug: the plugin's hand-rolled `_USAGE_ATTRS` read src keys "input"/"output", but the… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#p-mgr11)) |

## Phase: buildmyhouse-dev cluster pipeline — on-demand dev-branch build/deploy gate (2026-10-04)

> **Status correction (2026-10-06, manager):** the CO-DEV1–CO-DEV5 rows below still read > `todo`, but all five were completed and live-verified 2026-10-04 — the authoritative > evidence (real PASS verdict, digest-pinned images live, browser-adversary re-targeted at > commit f3b64c5, gate doc commands re-run) lives in…

Full narrative moved verbatim to [docs/archive/PLAN-ticket-notes-2026-10-13.md](docs/archive/PLAN-ticket-notes-2026-10-13.md#section-narrative-phase-buildmyhouse-dev-cluster-pipeline--on-demand-dev-branch-builddeploy-gate-2026-10-04) on 2026-10-13.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| CO-DEV1 | `buildmyhouse-dev` namespace + Infisical dev-secrets sync | — | company-os/k8s/buildmyhouse-dev-namespace.yaml (new), company-os/k8s/buildmyhouse-dev-infisical-sync.yaml (new) |  | done | Namespace manifest (buildmyhouse-dev) + InfisicalConnection/InfisicalAuth/InfisicalStaticSecret targeting project… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#co-dev1)) |
| CO-DEV2 | `buildmyhouse-dev` app/mcp/luxcore Deployment+Service manifests | CO-DEV1 | company-os/k8s/buildmyhouse-dev-app.yaml (new) |  | done | One manifest file, 3 Deployments + 3 Services (ClusterIP, no ingress), mirroring prod's topology/env vars from… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#co-dev2)) |
| CO-DEV3 | Auto-deploy `app`'s `dev` branch to `buildmyhouse-dev` on push (GitHub-polling CronJob) + on-demand… | CO-DEV1, CO-DEV2 | company-os/scripts/dev-deploy-poller.js (new, reuses `buildJobManifest`/`buildAndPush` from… |  | done | **Revised scope 2026-10-04** (supersedes the original one-shot-script framing): Nahar wants pushes to `app`'s `dev` branch to auto-deploy… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#co-dev3)) |
| CO-DEV4 | Point `browser-adversary` at `buildmyhouse-dev` alongside prod | — | company-os/k8s/browser-adversary.yaml |  | done | **2026-10-04, done directly by the manager (small mechanical fix, not dispatched):** research (Explore subagent) found the original… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#co-dev4)) |
| CO-DEV5 | Doc: how to invoke the dev-cluster gate flow and read its verdict | CO-DEV3, CO-DEV4 | company-os/docs/DEV-CLUSTER-GATE.md (new) |  | done | **DONE 2026-10-04:** wrote `docs/DEV-CLUSTER-GATE.md` covering all 5 required sections (what `buildmyhouse-dev` is/isn't; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#co-dev5)) |

## Hive worker tiering (2026-10-10)

Board directive: use Claude, OpenCode and mixed configs for different cost/power
options. Scheme, rollout and the handling of already-queued items are documented
in `hive/README.md` ("Worker tiers"); submitter convention is in
`hermes-engineering/container-workspace/HIVE_WORKER_MEMORY.md`. Items queued
before this change have no tier and behave as `standard`.

## Hive lifecycle reliability (2026-10-05)

Two worker dispatches failed before making progress, so the manager completed
the verified fallback. The code commit was pushed to remote `main` before the
Hive items were claimed and completed by this manager; the close-out records
the commit SHA and test evidence.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| HIVE-LIFECYCLE-1 | Resolve Hive work by readable slug and recover expired leases durably | — | hive/lib/hive/work.ex, hive/lib/hive/router.ex, hive/lib/hive/tasks.ex, hive/test/hive/router_test.exs,… | manager fallback | done | Verified by manager fallback after two worker dispatch failures. (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#hive-lifecycle-1)) |

| HIVE-MANUAL-UPDATE-1 | Tell local and Hive agents that company-os container updates require the controlled manual MCP rollout | — | AGENTS.md (workspace root), docs/DEPLOY-ENGINEERING.md, hive/README.md, hermes-engineering/AGENTS.md,… | buildmy-house-workspace | done | Re-verified 2026-10-06: all six target files independently confirmed to carry substantive (not stub) "manual release control" guidance —… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#hive-manual-update-1)) |

## Hive and MCP connection stabilization (2026-10-05)

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| HIVE-CONN-1 | Stabilize Hive subscriptions, diagnostics, and completion | — | scripts/engineering-manager-mcp.js, scripts/hive-member-mcp.js, scripts/engineering-manager-mcp.test.mjs,… | 2_Bob (manager fallback) | in_progress | Steward task: `hive-conn-1-stabilize-hive-subscriptions-diagnostics-and-com`. (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#hive-conn-1)) |
| MCP-HOST-1 | Isolate the engineering builder MCP from worker rollouts | HIVE-CONN-1 | k8s/engineering-build-dispatcher.yaml, k8s/kustomization.yaml, docs/DEPLOY-ENGINEERING.md; | 2_Bob (manager fallback) | in_progress | Steward task: `mcp-host-1-isolate-the-engineering-builder-mcp-from-worker-r`. (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#mcp-host-1)) |

## Phase: PM-agent (in-app product-feedback chat) + Hive feedback pipeline (2026-10-05)

Dispatched by `agent-manager` (Steward manager task `manage-company-os-pm-agent-hive-feedback-board-wave-1`). Builds the company-os half of a new feature: an isolated "pm-agent" that chats with app users (via the `app/` chat widget, tracked on that repo's own `PLAN.md`), learns from product context via read-only…

Full narrative moved verbatim to [docs/archive/PLAN-ticket-notes-2026-10-13.md](docs/archive/PLAN-ticket-notes-2026-10-13.md#section-narrative-phase-pm-agent-in-app-product-feedback-chat--hive-feedback-pipeline-2026-10-05) on 2026-10-13.

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| PM-A | Postgres schema + typed store for pm-agent conversations/feedback signals | — | `sql/pm_schema.sql` (new), `company_ops/pm_store.py` (new), `tests/test_pm_store.py` (new) | agent-manager | done | Shipped — commit `7fd3c5e` on `origin/main` (company-os), dispatched to `oc-opencode/mimo-v2.6-flash-free` via isolated git worktree… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#pm-a)) |
| PM-B | Hive A2A client — `message/send` only, no bid/claim/register | — | `company_ops/hive_client.py` (new), `tests/test_hive_client.py` (new) | agent-manager | done | Shipped — commit `d279668` on `origin/main` (company-os), dispatched to `oc-opencode/mimo-v2.6-flash-free` via isolated git worktree… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#pm-b)) |
| PM-C | pm-agent persona, minimal credential/tool scope, container + k8s manifest | PM-A, PM-B | `pm-agent/` (new dir: `SOUL.md`, `agent.py` or equivalent entrypoint, `Dockerfile`), `k8s/pm-agent.yaml` (new) | agent-manager | done | Mirror `hermes/SOUL.md`'s structure but scope strictly down: grounded support/product-manager persona, able to say "not now" / "already… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#pm-c)) |
| PM-D | Hermes-side bounded agent-query receiver (extends Hermes, careful/high-risk — do not dispatch to a free-tier… | — | `hermes/SOUL.md` (extend, do not rewrite), `company_ops/agent_query_interface.py` (new),… | agent-manager | done | Hermes's `SOUL.md` currently only models "the Board" (human via Discord) as an asker; (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#pm-d)) |
| PM-E | In-cluster chat surface wiring confirmation (no new code — verification ticket) | PM-C | — | — | todo | Once PM-C's Service exists, confirm in-cluster reachability end to end (`kubectl run --rm -it --image=curlimages/curl -n company-ops --… (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#pm-e)) |
| PM-F | Cloudflare Tunnel exposure for pm-agent (`pm-agent.buildmy.house`) | PM-E | `k8s/pm-agent-tunnel.yaml` (new) | — | todo | `cloudflared` Deployment in the `company-ops` namespace routing a new hostname (e.g. (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#pm-f)) |

## Hive terminal-signal recovery (2026-10-06)

Incident: Hive task task_c0a338c7e93ac4fa attempt 1 lost its terminal signal — bid/allocated
09:08:26Z, upstream get_result polls cease ~09:22Z, zero terminal events (no a2a_task failed, no
hive completion); the item was reallocated and attempt 4 completed healthy at 10:46:30Z (commit
97c2159 pushed to main by engineering-agent; independently verified: on origin/main, mix test
13/13 pass locally, diff matches self-report). Manager diagnosis (source + Axiom verified):
(1) engineering-manager-mcp.js trackProcess() get_result poll chain has no deadline — a hung
upstream call stalls forever with no terminal marker; (2) upstreamCall started telemetry emit omits
...context so start rows have task_id:null (live-confirmed in Axiom), and hive_subscription
work_received omits task_id; (3) hive router GET /work/:id/bids maps resolve_id :not_found to 500
"unable to rank bids" (live-confirmed; valid task id resolves bids fine).

| Ticket | Title | Deps | Owner paths | Claimed-by | Status | Notes |
|--------|-------|------|--------------|------------|--------|-------|
| HIVE-TERM-1 | Bounded worker poll chain + execution deadline, telemetry correlation ids, bids not-found→404 | HIVE-CONN-1 | scripts/engineering-manager-mcp.js, scripts/engineering-manager-mcp.test.mjs, hive/lib/hive/router.ex,… | 2_Paul (Hive dispatch: engineering-agent) | done | Shipped — commit `ab0e451` on `origin/main` (company-os), merged via `528411d`. (full: [archive](docs/archive/PLAN-ticket-notes-2026-10-13.md#hive-term-1)) |
