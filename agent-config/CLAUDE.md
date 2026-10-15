# Shared Engineering Context

You are working in the BuildMy-house engineering project, coordinated by
Hermes and Steward ACS.

- Read the repository's local `AGENTS.md` and `CLAUDE.md` before changing code.
- Prefer the smallest correct change and reuse existing project conventions.
- Verify before reporting completion; never claim tests passed without running.
- Keep secrets in environment variables, never in committed files.
- Preserve unrelated work from other agents.

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
