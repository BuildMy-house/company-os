# Engineering Container — Workspace Instructions

`/workspace` inside this container is the runtime equivalent of the
`buildmy.house` workspace root used for local dev: a parent directory
holding several independent repo checkouts as siblings
(`app-checkout/`, `website-checkout/`, `company-os-checkout/`,
`hermees-checkout/`, `observer-website-checkout/`), each cloned on demand by
`/opt/company-ops/scripts/sync-repo.sh <name>` (see `/workspace/README.md`),
each its own git repo with its own `CLAUDE.md`/`AGENTS.md` covering its
specifics. This file only covers concerns that span every checkout — read
the checkout's own `CLAUDE.md`/`AGENTS.md` before working on code in it.

Unlike the local `buildmy.house` root, `/workspace` itself is not a git
repo — it's materialized fresh by the entrypoint on every container start,
so there is nothing here to commit. Never `git init`/commit at this level.

## Steward ACS Coordination

`/workspace/AGENTS_STEWARD.md` explains why this level has no `Repo:`
identity of its own. Once you know which checkout you're editing, use
*that* checkout's own `AGENTS_STEWARD.md` (e.g.
`company-os-checkout/AGENTS_STEWARD.md`, `Repo: company-os`) for Steward
task/lock/memory scoping.

## Credentials — injected by Infisical, never hardcoded

Unlike local dev (which sources a git-ignored `.env`), this container has no
`.env` step: `engineering-entrypoint.sh` fetches every secret from Infisical
at startup (`INFISICAL_UNIVERSAL_AUTH_CLIENT_ID`/`SECRET`) and injects them
into the environment before any agent starts, and `sync-repo.sh` mints a
fresh short-lived GitHub App installation token per call rather than reusing
one from boot. Never write a literal secret into a committed file in any
checkout — same rule as local dev, different delivery mechanism.

## Multiple concurrent agents work this container too — assume it, don't fight it

A single container instance (or several replicas) can have more than one
manager/worker session active in the same checkout at once. This is normal:

1. **Never delete, revert, overwrite, or "clean up" a change you did not
   make**, unless positively confirmed abandoned or superseded. An
   unrecognized diff in a checkout is very likely another live session's
   in-progress or already-verified work, not garbage.
2. **Land your own verified work as a real git commit as soon as it's
   confirmed correct** — don't leave it sitting as an uncommitted diff. A
   commit is the only thing that survives a concurrent write to the same
   file, and only workers push (managers verify and flip the board, they
   don't `git add .` for a worker's files).

## Manager pattern

The engineering-manager agent definition lives at
`/opt/company-ops/.claude/agents/agent-manager.md` inside this image
(sourced from `company-os`'s own
`hermes-engineering/.opencode/agents/engineering-manager.md`), and is copied
to `$HOME/.claude/CLAUDE.md` at container startup — that's Claude Code's
live instruction set here. It carries the Steward `claim_work` concurrency
gate, the Steward-memory dispatch ledger, the ticket-writing checklist, wave
planning, and gatekeeper verification steps — see that file for the full
operating loop.

Every backgrounded worker dispatch should be paired with
`/opt/company-ops/scripts/stall-watch.sh <logfile> <pid> [stall_secs]
[hard_cap_secs] [label]` — it watches the dispatch's own log file for
actual growth (not just process liveness) and fires once on normal exit,
silent stall, or a hard time cap, so a manager doesn't have to repeatedly
re-check a dispatch by hand and burn tokens doing it.

## Model selection

Dispatch through the `ai-cli` aliases already configured at
`~/.config/ai-cli/config.toml` (`free`, `cheap`, `balanced`, `quick`,
`flash`, `hard`) rather than raw provider/model strings — see the
engineering-manager definition's "Choosing a worker CLI/model per ticket"
section for the rung ladder and the GLM-5.3-Flash campaign-window check.
Default to `free`/`balanced` for mechanical edits and standard features;
only escalate to `hard` once a cheaper rung has demonstrably failed on the
ticket.
