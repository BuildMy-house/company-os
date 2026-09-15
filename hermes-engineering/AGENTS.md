# Hermes Engineering Team — Universal Agent Rules

These rules apply to every agent working in this workspace: manager, workers,
and any delegated subagent. No exceptions.

## Hierarchy

```
Hermes (CEO) → sets direction, approves releases
  └─ Engineering Manager → plans, dispatches, verifies, flips board
       └─ Workers → implement, test, commit own code
```

Hermes owns product direction. The Manager turns direction into execution.
Workers execute and self-report. The Manager independently verifies before
marking anything done.

## Cardinal Rules

1. **Read before write.** Open every file you plan to edit. Never assume
   structure, imports, or naming.
2. **Claim before edit.** Lock every file via Steward before modifying it.
   First lock establishes repo scope.
3. **Own your lanes.** Never edit files outside your assigned directory.
   Disjoint ownership prevents merge conflicts.
4. **Verify before report.** Run the test suite. Do not claim "all green"
   without evidence. The Manager will re-verify and will catch false claims.
5. **Commit your own work.** Each agent commits only files they own.
   Staging `git add .` is forbidden.
6. **No batch mutations.** One file at a time. Review each change before
   proceeding to the next.
7. **Never delete without reason.** Renames and moves preserve history.
   Deletion requires explicit approval.
8. **Save learnings.** After completing work, save patterns, warnings, and
   decisions to Steward. These become institutional knowledge.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community
structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions
before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when
  graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for
  relationships and `graphify explain "<concept>"` for focused concepts.
- Dirty graphify-out/ files are expected after hooks or incremental updates;
  dirty graph files are not a reason to skip graphify.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead
  of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when
  query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current
  (AST-only, no API cost).

## E2E Tests (Playwright) — MANDATORY for UI work

UI and 3D viewport changes MUST be verified with Playwright E2E tests.

### Commands

```bash
cd homely
npm run e2e              # run all E2E tests headless
npm run e2e:open         # open Playwright inspector for debugging
npx playwright test --ui # interactive test runner
```

### When to run

- **Any UI change** (toolbar, menus, status bar, properties panel, layout, CSS)
- **Any 3D viewport change** (camera, scene building, rendering, controls)
- **Any plan-view change** (canvas rendering, input handling, tools)
- Before committing changes to `homely/src/main.ts`, `homely/src/view3d/`,
  `homely/src/plan/`, `homely/src/ui/`, or `homely/src/style.css`

### Test files

| File | Covers |
|------|--------|
| `e2e/layout.spec.ts` | DOM shell, toolbar, menus, status bar, camera toggles |
| `e2e/viewport3d.spec.ts` | WebGL canvas, 3D rendering, panel visibility, screenshot baseline |
| `e2e/plan-3d-sync.spec.ts` | Cross-view sync, undo/redo, wall drawing flow |

### Writing new E2E tests

- Use `page.waitForSelector('#view3d canvas')` in `beforeEach` to ensure
  Three.js has booted.
- Interact with the plan via `page.mouse.click()` on `#plan-canvas` coordinates.
- Tool switching: `page.locator('button[data-tool="wall"]').click()`
- Camera presets: `page.locator('button[data-preset="3d"]').click()`
- Check WebGL content via `page.evaluate()` reading pixels from the canvas.
- Screenshot comparisons: `await expect(locator).toHaveScreenshot('name.png')`

### Debugging failures

- `npx playwright show-trace results/.../trace.zip` to replay a failed run.
- Screenshots saved to `test-results/` on failure.
- HTML report at `playwright-report/` after any run.

### Steward notes for E2E

- E2E tests auto-start the Vite dev server (port 1420) via `webServer` config
- Screenshots fail on first run (no baseline); use `--update-snapshots` to set
- Chromium only (WebGL required; no Firefox/Safari)
- Trace files saved on failure for debugging

## Verification Protocol

Every ticket has an explicit Definition of Done (DoD) with test commands.
Before reporting done:

1. Run the exact commands listed in the ticket's DoD.
2. Capture the full output (not just exit code).
3. If any check fails, fix it or mark the ticket blocked with the error.
4. Never report a check as passing if you didn't run it.

Local verification script (mirrors CI):
```bash
./scripts/verify-all.sh
```

If the script doesn't exist yet, run each check individually:
```bash
# Adapt to your project's toolchain:
npm run lint
npm run typecheck
npm run test
npm run e2e          # if UI changes
pytest               # if Python changes
```

## Model Selection

Prefer free-tier models for standard work. Use paid models only with a
concrete quality or risk reason. Report model, quota, estimated cost,
and outcome in every delegation result.

If a paid model was used for work a free model could handle, record a
routing lesson so it doesn't repeat.

## Steward Integration

Every agent interaction follows the Steward lifecycle:

```
claim_work → lock_file → implement → verify → save_memory/skill_save
→ unlock → commit → release_work → submit_task_feedback
```

- `claim_work`: Claim ownership of the task
- `lock_file`: Lock every file before editing (first lock = repo scope)
- `implement`: Write code, following project conventions
- `verify`: Run DoD test commands, capture evidence
- `save_memory`: Record eternal truths (patterns, learnings, warnings)
- `unlock`: Release file locks
- `commit`: Stage ONLY your files, write descriptive commit message
- `release_work`: Release the task
- `submit_task_feedback`: Report what worked, what didn't, what's missing

## Failure Recovery

- Worker fails once: retry with a different model or approach.
- Worker fails twice: the Manager does it directly or marks blocked.
- Manager is unavailable: workers continue independently but cannot
  mark tickets done — they leave review notes.
- Steward is unavailable: work without locks, but document the gap.

## Communication

- **To Hermes**: Manager reports decisions needed, risks, progress.
  One message, no fluff.
- **Manager → Worker**: Specific task, files to touch, DoD, constraints.
- **Worker → Manager**: Files changed, test output, model used, issues.
- **Blocked**: Set status `blocked` in PLAN.md + write the reason.
  Don't silently stall.

## CI

Automated verification runs on every push/PR via `.github/workflows/ci.yml`
(two jobs: `homely` = lint + tsc + vitest + Playwright e2e; `equivalence` =
pytest). The workflow uses whatever lint/test config exists at merge time —
it does not own the config itself.

The **authoritative** verification gate is the local script:

```bash
./scripts/verify-all.sh             # all checks (lint+tsc+vitest+e2e+pytest)
./scripts/verify-all.sh --skip-e2e  # fast path, skips slow Playwright suite
```

The script runs every check CI would run, prints a PASS/FAIL summary per
step, and exits non-zero if any step fails. Run it before committing — it is
the automated check that prevents a "done" ticket from shipping with broken
stubs. Once a GitHub remote is added, `ci.yml` runs the identical checks
automatically on push/PR.
