# Hermes Engineering Plan

Multi-agent engineering plan for Homely (Tauri + Three.js) with Python
equivalence harness.

**Frozen contracts**: `docs/schema/home-project.schema.json` (NormalizedHomeState v1),
`docs/specs/ws-protocol.md` (automation WebSocket protocol v1).

## Claim Board

Agents claim by editing ONLY their row, then commit `board: claim <TICKET-ID>`.
Status flow: `todo → claimed → in_progress → review → done`.
Only the integrator sets `done`. Blocked: set status + reason.

| Ticket | Title | Deps | Owner dir | Track | Claimed-by | Status | Notes |
|--------|-------|------|-----------|-------|------------|--------|-------|
| T1 | *example: scaffold feature X* | — | homely/ | core | — | todo | — |

## Track Legend

| Track | Scope | Owner |
|-------|-------|-------|
| core | Core model, plan view, 3D viewport | clone-dev |
| driver | Java driver wrapper, state capture | driver-dev |
| harness | Python equivalence harness, scenarios | harness-dev |
| ui | UI shell, toolbar, properties panel | clone-dev |
| integration | Contracts, docs, golden files | integrator |
| infra | CI, tooling, build, lint | manager |

## Completed

<!-- Move done tickets here with date and summary -->

## Blocked

<!-- Tickets that are blocked, with reason -->
