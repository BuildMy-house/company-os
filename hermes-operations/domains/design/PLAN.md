# Design Plan

Design plan for Homely — UI/UX, design system, brand identity.

## Claim Board

Agents claim by editing ONLY their row, then commit `board: claim <TICKET-ID>`.
Status flow: `todo → claimed → in_progress → review → done`.
Only the manager sets `done`. Blocked: set status + reason.

| Ticket | Title | Deps | Owner dir | Track | Claimed-by | Status | Notes |
|--------|-------|------|-----------|-------|------------|--------|-------|
| D1 | *example: redesign properties panel* | — | site-homely/src/components/ | ui | — | todo | — |

## Track Legend

| Track | Scope | Owner |
|-------|-------|-------|
| ui | UI components, layouts, interactions | design-dev |
| system | Design tokens, component library | design-dev |
| brand | Visual identity, brand assets | design-dev |
| a11y | Accessibility audits, fixes | design-dev |
| research | User research, testing, insights | design-dev |

## Knowledge Base

Accumulated design knowledge lives in `knowledge/`:
- `knowledge/design-system.md` — Component patterns and tokens
- `knowledge/accessibility-audits.md` — Past audit results and fixes
- `knowledge/user-research.md` — Insights from user testing
- `knowledge/brand-guide.md` — Visual identity guidelines
