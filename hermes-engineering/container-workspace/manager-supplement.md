## Container operations

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

## Reporting to Hermes

When closing out a wave or a board, report back to Hermes with: tickets
completed/blocked this run, a model/cost breakdown (pulled from
`steward_query_memories` on this repo's `agent-manager/dispatch-log`
scope), any risks or contract gaps surfaced, and next priorities. This is
in addition to, not instead of, the board close-out report described in
the canonical agent-manager instructions' "Completion" section.
