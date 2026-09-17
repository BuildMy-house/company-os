# Steward ACS — Agent Instructions (container workspace root)

## No `Repo:` identity at this level

`/workspace` is not itself a git repo — it's a container-runtime directory
holding on-demand checkouts of several independent repos as siblings
(`app-checkout/`, `website-checkout/`, `company-os-checkout/`,
`hermees-checkout/`, `observer-website-checkout/`). It has no Steward
`Repo:` declaration of its own and never will — don't invent one.

Before the first file lock, identify the checkout you are actually editing:

1. `git -C <checkout-path> rev-parse --show-toplevel` (or just note which
   `*-checkout/` directory you're in).
2. Read `<checkout>/AGENTS_STEWARD.md` and use its `Repo: <name>` value
   (e.g. `company-os-checkout/AGENTS_STEWARD.md` → `Repo: company-os`).
3. Pass that value as `repo` with `repo_confirmed: true` on the first
   `lock_file` call.

The first successful lock establishes the task and session repository so
ACS knows where the agent is working. Later locks from a different repo
fail with `repo_mismatch` — that's expected if you switch checkouts
mid-session without starting a new task.

## Everything else is generic — see the checkout's own AGENTS_STEWARD.md

Task lifecycle (`create_work`/`claim_work` before work,
`save_memory`/`release_work`/`submit_task_feedback` after), scopes, human-
readable task-ID slugs, and the two Steward server environments (local
`acs` vs. production `acs_prod`) are documented in full in each checkout's
own `AGENTS_STEWARD.md` — read that once you know which repo you're in
rather than duplicating it here. This file exists only to explain why
`/workspace` itself has no `Repo:` line, so an agent doesn't go looking for
one at this level.
