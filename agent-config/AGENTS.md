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
