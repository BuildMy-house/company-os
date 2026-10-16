# Reviewer-merger agent (design, not yet built)

Status: draft for review. Nothing here is deployed. Decided by Nahar on 2026-10-16.

## Purpose

Engineering workers open PRs; nobody merges them without a human. The
reviewer-merger removes that bottleneck: it reviews a PR, and if it passes the
checklist below, merges it into `main`. CI/CD then
deploys. The agent is one more Hive member with a single job.

## Scope decisions

| Agent | Repos it can write to | Why |
|-------|----------------------|-----|
| publisher | website, hermees-diary, observer-website; `blog/<slug>` branches only | Holds Reddit/Substack/GitHub credentials and reads external content; kept narrow (guard in `publisher/lib/guard.mjs`). Unchanged. |
| engineering workers | all six cloned repos; branches and PRs, no merge | Existing GitHub App. |
| reviewer-merger | website, hermees-diary, observer-website, app, company-os; merge into `main` | The only identity with merge permission. |

It never merges into `prod`. Promotion to `prod` follows the existing branch
workflow where one exists: `company-os` has a `prod` branch; `app` has none (see Verified facts).
Moving that gate to an automated one is a separate decision (see "Deploy
follow-ups").

## Identity and permissions

- A separate GitHub identity (second GitHub App preferred, machine user
  otherwise). Not the engineering App, so workers cannot merge.
- Pull requests: write, Contents: write, Checks/Statuses: read, on the five
  repos only.
- Branch protection on `main` of every repo: require the `check` status and one
  approving review. The reviewer-merger's identity is the approver and the only
  actor allowed to merge. `prod` branches: no merge rights for this identity.
- Credentials live in Infisical in their own folder and sync to their own
  k8s Secret, like `/publisher`.

## Review checklist (all must pass)

1. Required checks (`check`) are green on the PR head commit.
2. The PR was not opened by the reviewer-merger's own identity.
3. The diff matches the PR title and the linked ticket's definition of done.
4. No secrets, tokens or `.env` content in the diff.
5. Shared components (design tokens, `Layout.astro`, shared libraries) per the
   repo's `AGENTS.md`: reviewed explicitly, the review recorded in a PR comment
   before the merge.
6. Target branch is `main`, never `prod`.

## Refusals (human required)

The agent does not merge a PR that changes any of:

- its own spec, identity, or this file;
- `k8s/base/mcp-gateway` policy, RBAC, or any `*-secrets-sync` manifest;
- branch protection or CI workflow files;
- the deploy updater (see below).

These keep it from widening its own access. The list is deliberately short; add
to it when an incident shows a gap.

## Failure behaviour

- It comments its reasoning on every merge or refusal.
- A merge is revertible (`git revert`); the agent can open a revert PR and
  merge it under the same checklist if the deploy health check fails.
- If it is down, PRs wait. Nothing else depends on it.

## Deploy follow-ups (separate tickets)

- `app`: blue-green deploy triggered from `main`, smoke test before switching.
  First step is documenting how `app` deploys today (not yet verified).
- Static sites: Cloudflare Pages already deploys on merge and keeps previous
  deployments for rollback.
- `company-os`: a self-update path for the Hive that does not run inside the
  thing it updates: separate updater, health-gated rollout, automatic
  `rollout undo`, fixed order (stateless agents, then Hive, `mcp-gateway`
  last), and excluded from its own updates. Images are built host-side on
  pop-os-1 because builder pods cannot pull from the node-local HTTP registry.

## Verified facts (checked 2026-10-16 via the GitHub API)

- `app` has no `staging` or `prod` branch. Merging to `main` already deploys to
  app.buildmy.house: `deploy-app.yml` runs after the `CI` workflow succeeds on `main`. There is no
  blue-green yet, and the most recent `build-and-deploy` run on `main` failed. So for `app`, a merge
  IS a production deploy until the blue-green work lands.
- `company-os` has a `prod` branch; merging to `main` does not deploy by itself.
- `main` is unprotected on `app` and `company-os` (and the three site repos).
- Both repos report check runs (`app`: several CI jobs plus `build-and-deploy`; `company-os`:
  `kustomize-render-gate`, `test`). The policy needs every run green, which can refuse a PR whose
  head carries a failed duplicate run.

## Open questions

- Second GitHub App vs machine user (affects `enforce_admins` and audit trail).
- Where the agent runs (Hive member Deployment, like `publisher`).
- Steward was unreachable when this was drafted, so no ticket exists yet.
