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

## Phase 3-8 (drafted, not dispatched this run)

See bottom of file for drafted ticket text once Phase 1-2 land. Do not
dispatch until explicitly resumed in a future run.
