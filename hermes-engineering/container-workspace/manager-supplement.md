## Container operations

## Worker model constraint

For engineering worker dispatches, use only `oc-opencode/mimo-v2.6-flash-free`
or `oc-zai-coding-plan/glm-5.3-flash`. Do not fall back to older MiMo
versions or the tokenrouter GLM free model.

You also have direct control over this container's own deployment via the
`container-manager` MCP tools — use these when the board/ticket is about
the engineering container itself (self-upgrade, rollback, health):

- `container_status` / `container_health` / `container_logs` — check
  current state before acting.
- `container_restart` — restart the running container.
- `container_upgrade` — pull and deploy a new image build.
- `container_rollback` — revert to the previous known-good image if an
  upgrade regresses.

Treat these the same as any other risky, hard-to-reverse action: verify
current state first, and prefer `container_rollback` over guesswork if an
upgrade misbehaves.

## Steward `repo:` names — by checkout, not by convention

`sync-repo.sh` materializes checkouts as `/workspace/<name>-checkout/`.
Pass the matching Steward `repo:` value (with `repo_confirmed: true`) on
your first `lock_file`/`create_work` call for that checkout — don't guess
from the checkout's directory name, since it doesn't always match:

| Checkout                          | Steward `repo:` name  |
|------------------------------------|------------------------|
| `app-checkout/`                    | `buildmy-house-app`   |
| `company-os-checkout/`             | `company-os`          |

`website-checkout/`, `hermees-checkout/`, and `observer-website-checkout/`
have no `AGENTS_STEWARD.md`/`Repo:` identity yet on their source repos —
don't invent a `repo:` name for them; check that checkout's own
`AGENTS_STEWARD.md` first (it may have been added since this was written),
and ask Hermes/the human before locking files there under Steward if it's
still missing.

## Reporting to Hermes

When closing out a wave or a board, report back to Hermes with: tickets
completed/blocked this run, a model/cost breakdown (pulled from
`steward_query_memories` on this repo's `agent-manager/dispatch-log`
scope), any risks or contract gaps surfaced, and next priorities. This is
in addition to, not instead of, the board close-out report described in
the canonical agent-manager instructions' "Completion" section.
