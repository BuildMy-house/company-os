# Engineering — Steward Coordination

Extends `shared/AGENTS.base.md` Steward section. Read that first.

## Repo Identity

- **Repository**: `house_designer`
- **Branch**: `main` (shared checkout)
- **Project**: Rebuilding Sweet Home 3D as "Homely" (Tauri + Three.js)
  with Python equivalence harness

## Layout & Ownership (STRICT)

| Directory | Owner | Notes |
|---|---|---|
| `sweethome3d-7.5-wayland-patch/` | — | READ-ONLY reference. Never modify. |
| `equivalence/driver-java/` | driver-dev | Java driver wrapper |
| `equivalence/eq/`, `equivalence/scenarios/` | harness-dev | Python test harness |
| `homely/` | clone-dev | Tauri + Three.js clone |
| `docs/schema/`, `docs/specs/` | integrator | Frozen contracts |
| `docs/behaviours/` | any agent | Append-only |
| `PLAN.md` | integrator | Live claim board |
| `company-ops/` | Hermes/CEO | Company operations |

**Rule**: Never edit outside your owner directories. Your PLAN.md row is
the one exception.

## Frozen Contracts

These files are locked by the integrator. Change requests go through
a `blocked` board note:

- `docs/schema/home-project.schema.json` — NormalizedHomeState schema v1
- `docs/specs/ws-protocol.md` — Automation WebSocket protocol v1

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

- **Only the integrator/manager sets `done`**
- Workers cannot self-approve
- `blocked`: set status + write reason in Notes column

### 3. Steward lifecycle per ticket

```
claim_work(task_id)
  → lock_file every file before editing
    → implement
      → verify (run DoD test commands, capture output)
        → save_memory / skill_save (record learnings)
          → unlock files
            → commit (stage only your files)
              → update PLAN.md row
                → release_work
                  → submit_task_feedback (LAST)
```

### 4. File locking protocol

First lock establishes repo scope. Subsequent locks within the same
task reuse that scope. If a file is already locked by another agent,
wait or mark blocked.

### 5. Commit protocol

- Stage only your owned files: `git add <explicit paths>`
- Never `git add .`
- Commit message: `<ticket-id>: <description>`
- Verify `git status --short` after staging to prove separation

## Key Facts (from architecture research)

- SH3D 7.5 source at `sweethome3d-7.5-wayland-patch/`; pre-built jar in
  `build/SweetHome3D.jar`; GPL v2 — Homely must never import its code.
- Drive SH3D via controllers in centimeter model coordinates:
  `PlanController.pressMouse/moveMouse/releaseMouse/setMode(Mode)`.
- Units: cm lengths, radians angles internally; normalized state
  uses cm + degrees.
- Camera defaults: FOV 63deg, top camera z=1010 pitch 45deg,
  observer eye 170cm yaw 315deg pitch 11.25deg.
- Default wall height 250cm.

## Steward Scope

Domain scope path: `house_designer/engineering`
