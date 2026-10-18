# Reviewer-merger agent

Status: built, not deployed (no image, no GitHub App, no live test). Decided by Nahar on 2026-10-16.

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

- Its own GitHub App, separate from the engineering App (4857525), so workers cannot merge.
  `reviewer/lib/token.mjs` mints installation tokens from its credentials and has no defaults.
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

## Build and deploy (human steps)

Code: `reviewer/` (policy, GitHub client, token minter, CLI, tests), `Dockerfile.reviewer`,
`scripts/reviewer-entrypoint.sh`, `k8s/base/reviewer/`, `k8s/base/reviewer-secrets-sync/`. The pod is a
Hive member declaring the tool `github-merge`, only when `GITHUB_APP_ID`,
`GITHUB_APP_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY` are all present. Like `publisher`, it is
not in the `buildmy-prod` overlay.

1. Create a GitHub App (org BuildMy-house): Pull requests read/write, Contents read/write, Checks
   read, Metadata read. Install it on website, hermees-diary, observer-website, app, company-os only.
2. Put in Infisical `/reviewer` (exactly these keys): `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`,
   `GITHUB_APP_PRIVATE_KEY`, `REVIEWER_LOGIN` (the bot login, `<app-slug>[bot]`).
3. Build the image host-side on pop-os-1 (`Dockerfile.reviewer`, BASE_IMAGE pinned by digest), put
   the digest in `k8s/base/reviewer/reviewer.yaml`.
4. `kubectl apply -k k8s/base/reviewer-secrets-sync`, then `kubectl apply -k k8s/base/reviewer`.
5. Verify the Hive registration lists `github-merge`, then test on a throwaway PR in a site repo.

`app` merges deploy to production (see Verified facts). To keep the first live run safe, test on a
site repo before the `app` entry is exercised.

## Open questions

- Whether the five-repo scope should include `app` before blue-green exists (Nahar accepted the risk).
- Steward was unreachable when this was drafted, so no ticket exists yet.
