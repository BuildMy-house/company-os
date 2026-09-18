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
`/opt/company-ops/.claude/agents/agent-manager.md` inside this image, and is
copied to `$HOME/.claude/CLAUDE.md` at container startup — that's Claude
Code's live instruction set here. It's built at image-build time by
concatenating the **canonical** `.agents/agent-manager.md` from
`buildmy.house/workspace` (pulled live via a Docker additional build
context, not a hand-maintained copy — see `Dockerfile.engineering`) with
this repo's own `manager-supplement.md` (container-only concerns:
self-upgrade/rollback, reporting to Hermes). It carries the Steward
`claim_work` concurrency gate, the Steward-memory dispatch ledger, the
ticket-writing checklist, wave planning, and gatekeeper verification steps
— see that file for the full operating loop.

Every backgrounded worker dispatch should be paired with
`/opt/company-ops/scripts/stall-watch.sh <logfile> <pid> [stall_secs]
[hard_cap_secs] [label]` — it watches the dispatch's own log file for
actual growth (not just process liveness) and fires once on normal exit,
silent stall, or a hard time cap, so a manager doesn't have to repeatedly
re-check a dispatch by hand and burn tokens doing it.

## Model selection

There are no named worker-tier aliases (`free`/`cheap`/`balanced`/etc.) —
`~/.config/ai-cli/config.toml` no longer defines any; dispatch every
worker with an explicit `ai-cli run --model <provider/model>`, the same
raw-string resolution local dev already uses. A preset tier table was a
second source of truth that drifted from what workers actually needed
(model ids renamed, new free models never added to it) and added nothing
a direct model choice couldn't do — you (Claude, the manager) are the
only thing Hermes talks to and the only one deciding where a ticket goes,
so there was never a safety reason to route through a fixed tier instead
of choosing a model directly.

Pick the model per ticket from: the workspace-wide `agent-manager/
model-routing` Steward memory (known good/bad fits by task type) and a
fresh `ai-cli models` listing (catches models added since the memory was
last updated) — see the canonical file's "Environment-neutral model
selection" and "Model-routing memory" sections for the full mechanism,
including how to try and record a model with no routing memory yet.
