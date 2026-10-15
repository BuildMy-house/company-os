# Shared Engineering Rules

These rules apply to agents working on this project. Repository-specific
instructions take precedence where they are more specific.

1. Read before writing; inspect callers and existing patterns first.
2. Claim and lock files before editing when Steward is available.
3. Keep changes inside the assigned repository and directory.
4. Verify changes before reporting them complete.
5. Commit only files owned by the current task.
6. Never put credentials or tokens in tracked configuration.

## buildmy.house MCP test account

For buildmy.house MCP testing, use `https://app.buildmy.house/mcp`. The
local account file is `certs/buildmyhouse-test-account.env` (mode `600`,
git-ignored). In company-ops runtime containers, the same values are
available through Infisical as `BUILDMYHOUSE_APP_URL`,
`BUILDMYHOUSE_MCP_URL`, `BUILDMYHOUSE_TEST_EMAIL`,
`BUILDMYHOUSE_TEST_PASSWORD`, and `BUILDMYHOUSE_MCP_BEARER_TOKEN`.
Never copy the values into instructions, commits, prompts, logs, or memory.

Hermes sets direction. A domain manager plans, delegates, and verifies.
Workers implement, test, and report their own commits.

For the long-term coordination model, read
[`docs/HIVE-ARCHITECTURE.md`](../docs/HIVE-ARCHITECTURE.md). New coordination
work should move toward Elixir/Postgres durable events, leases, and task-ID
completion signals while preserving the existing Steward and k3s boundaries.

## Ticket standard

Work submitted to Hive (`hive_submit`) must follow `docs/TICKET-STANDARD.md`:
`type`, Fibonacci `points`, `goal`, `acceptance_criteria` and `scope`. Bugs that need
diagnosis or touch production score higher. Points set the worker tier. Malformed
tickets are rejected.

## Compact Instructions

When compacting or summarizing this conversation (Claude `/compact`, OpenCode
auto-compaction at ~100k tokens), always preserve verbatim:

- Steward task ids (umbrella and sub-tickets), which files this session has
  locked, and the Steward `repo:` name in use.
- Per-ticket definition of done and current status (todo/claimed/in_progress/
  review/done/blocked), with the exact verification commands and their results.
- Running background work: PIDs, `ai-cli`/OpenCode task ids and models, stall-
  watcher state, anything still to be waited on or cleaned up.
- Open decisions, pending human approvals (e.g. company-os `prod` merge/deploy),
  and constraints the user stated this session.
- The repo, branch, and worktree path being edited, plus commit SHAs already
  made and anything uncommitted or unpushed.

Drop raw tool output, file dumps, and exploration that did not change a decision.
