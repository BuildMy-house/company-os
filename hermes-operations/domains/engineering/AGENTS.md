# Engineering Domain — Agent Rules

Extends `shared/AGENTS.base.md`. Read that first.

## Domain: Engineering

Software development, code quality, architecture, testing, deployment.

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

## CI

Automated verification runs on every push/PR via `.github/workflows/ci.yml`
(two jobs: `homely` = lint + tsc + vitest + Playwright e2e; `equivalence` =
pytest).

Local verification script (mirrors CI):
```bash
./scripts/verify-all.sh
```

## Steward Scoping

Engineering domain uses Steward scope path: `company-os/engineering`
