# Publisher agent

OpenCode-flavor Hive member `publisher`. Hermes (strategist and approver) sends it tickets over
A2A/Hive; it writes social/blog content, analyses posts, opens blog PRs and reports back.

## Architecture

- Pod `publisher` (company-ops): own ServiceAccount, no k8s token, no RBAC.
  - `publisher` container: the node agent. Env from `publisher-secrets` only. No ingest DSN, no
    Substack cookie. Tool CLI: `publisher` (`publisher/bin/publisher`).
  - `ingest-sidecar`: same image, `company_ops.marketing_sidecar` on 127.0.0.1:8099. Env from
    `marketing-ingest-secrets`. Serves `/ingest` and `/substack/call`; writes ready-marker files into a
    shared emptyDir once its DB check and env are OK. Those markers gate the `marketing-ingest` and
    `substack-publish` tools in Hive registration (`TOOL_CREDENTIAL_ENV` `file:` entries).
  - The main entrypoint waits up to 60s for the marker, because Hive registration declares tools
    once at startup. If the sidecar becomes ready later, restart the pod.
- `marketing-ingest` CronJob (`k8s/base/marketing-ingest`): same ingester code, daily.
- Postgres: schema `marketing` in `homely_company` (aggregates and `ingest_runs` only; no emails,
  no subscriber-level rows). Roles: `marketing_ingest` (writes), `publisher_reader` and
  `hermes_analytics` (SELECT). The agent never writes SQL; rows are validated before upsert.
- Memory: Hindsight bank `publisher` on `http://hindsight:8888`. One shared `HINDSIGHT_API_KEY`:
  separation is by bank name only, not access control.
- Hand-back: ~10 line Hive result carrying the full report, plus Steward `save_memory`. Reports are
  NEVER pushed to git (all three site repos are public). Reports state source, date range, row counts
  and mark unchecked items as untested.

## Approval gate

No `approval_ref` means draft only; the agent says why. `approval_ref` travels in ticket metadata and
is injected into the worker prompt. Hive `GET /tasks/:id` hides the payload, so the pod can check only
presence and format (`^[A-Za-z0-9][A-Za-z0-9._:/#-]{5,119}$`), not authenticity. Follow-up: expose
the payload (or a signed ref) from Hive.

## Push guard (defense in depth only)

`/usr/local/bin/git` wrapper plus `pre-push` hook allow pushes only to `blog/<slug>` branches, and
only to an explicit three-repo allowlist compiled into `publisher/lib/guard.mjs` (`ALLOWED_REPOS`):
`BuildMy-house/website`, `BuildMy-house/hermees-diary`, `BuildMy-house/observer-website`.
`PUBLISHER_ALLOWED_REMOTES` (env) can narrow that set but never widen it, and `publisher/config/publisher.json`
`repos` must equal it (a test enforces both). `reports/*` branches are refused. An agent with a shell can bypass
this. The real control: a fine-grained token scoped to exactly those three repos (contents + pull requests
write) AND branch protection on `main` in each. All three repos are public and `main` is currently
unprotected on all three (checked 2026-10-15 via the GitHub API).

## Website findings

The blog is in `hermees-diary` (`src/content/blog/YYYY-MM-DD-slug.md`). The `website` repo has no
blog, and its CI has no deploy job although `docs/DEPLOY.md` claims Cloudflare Workers. The repo,
content dir and front matter are config-driven (`publisher/config/publisher.json`, ConfigMap `publisher-config`).

## Data sources

- GA4 and Search Console: official read APIs (OAuth refresh token). Not verified live.
- Reddit: official API (script-app password grant); allowlist + daily cap. Access and the approved-app
  and self-promotion policy could not be read (403) - unverified.
- Substack: no official API. Uses the unofficial cookie-based `substack-mcp` pinned at npm 1.3.2
  (gitHead c148a3b, tarball integrity checked in `Dockerfile.publisher`). The cookie is a password,
  expires, and use may breach Substack ToS. Cookie expiry is recorded as `auth_failed`. Only
  `get_publication_stats`, `get_post_stats`, `list_posts`, `create_draft_post`, `publish_draft`
  (send forced false) are reachable; subscriber export is refused. Transitive dependencies of the SDK
  are not locked by the tarball.

## Human steps, in order

1. Create the Infisical keys (project 8806c2b0-73d2-4bea-8537-5b874c5ff592): `/publisher`,
   `/marketing-ingest`, and `MARKETING_INGEST_PASSWORD`, `PUBLISHER_READER_PASSWORD` in `/hermes`
   (key lists: `docs/SECRETS-INFISICAL.md`).
2. Apply `sql/marketing_schema.sql` to `homely_company`.
3. Run `scripts/provision-db-roles.sh`.
4. Build the engineering base (opencode flavor), then the publisher image
   (`PUBLISHER_BASE_IMAGE=... scripts/build-push-images.sh --only publisher`); bump the digests in
   `k8s/base/publisher/publisher.yaml` and `k8s/base/marketing-ingest/marketing-ingest.yaml`.
5. Apply by hand: `k8s/base/publisher-secrets-sync`, `k8s/base/marketing-ingest`, `k8s/base/publisher`.
   They are deliberately NOT in the buildmy-prod overlay.
6. Set `reddit.allowedSubreddits` in the ConfigMap.
7. Create the three-repo fine-grained GitHub token and enable branch protection on `main` in each repo.

## Known limits

- Ledger (daily caps, approval_ref reuse) is an emptyDir and resets on restart; use a PVC.
- Substack drafts are plain paragraphs; Markdown to ProseMirror conversion is a follow-up.
- `CREATE TABLE IF NOT EXISTS` does not alter an existing `ingest_runs` CHECK (schema not applied anywhere yet).
- NetworkPolicy egress is public 443 minus private ranges (no FQDN matching).
