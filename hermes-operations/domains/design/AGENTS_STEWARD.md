# Design — Steward Coordination

Extends `shared/AGENTS.base.md` Steward section. Read that first.

## Repo Identity

- **Repository**: `house_designer`
- **Branch**: `main` (shared checkout)
- **Project**: Homely design — UI/UX, design system, brand identity

## Layout & Ownership (STRICT)

| Directory | Owner | Notes |
|---|---|---|
| `site-homely/src/components/` | design-dev | UI components, design system |
| `site-homely/src/styles/` | design-dev | CSS, tokens, themes |
| `docs/design/` | design-dev | Mockups, prototypes, research |
| `PLAN.md` | design-manager | Live claim board |

**Rule**: Never edit outside your owner directories.

## Coordination Protocol

### 1. PLAN.md is the live claim board

Read it first. Claim by editing only your row and committing:
```
board: claim <TICKET-ID>
```

### 2. Status flow

```
todo → claimed → in_progress → review → done
```

- **Only the manager sets `done`**
- Workers cannot self-approve
- `blocked`: set status + write reason in Notes column

### 3. Steward lifecycle per ticket

```
claim_work(task_id)
  → lock_file every file before editing
    → implement
      → verify (run DoD commands, capture output)
        → save_memory / skill_save (record learnings)
          → unlock files
            → commit (stage only your files)
              → update PLAN.md row
                → release_work
                  → submit_task_feedback (LAST)
```

## DoD Checklist (per ticket type)

### UI component
- [ ] Component renders correctly in all breakpoints
- [ ] WCAG 2.1 AA compliant (axe scan clean)
- [ ] Keyboard navigation works
- [ ] Screen reader tested
- [ ] Storybook entry added (if applicable)
- [ ] Design tokens used (no hardcoded values)

### Brand asset
- [ ] Meets brand guide specifications
- [ ] Exported in required formats (SVG, PNG)
- [ ] Variants provided (light/dark, color/bw)

### Accessibility audit
- [ ] axe scan passes with 0 violations
- [ ] Lighthouse accessibility score ≥ 90
- [ ] Keyboard-only navigation verified
- [ ] Color contrast ratio ≥ 4.5:1

## Steward Scope

Domain scope path: `house_designer/design`
