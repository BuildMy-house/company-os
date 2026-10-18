import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ALLOWED_REPOS, evaluate, globToRe } from "../lib/policy.mjs";
import { Refused, client, merge } from "../lib/github.mjs";

const cfg = JSON.parse(readFileSync(new URL("../config/reviewer.json", import.meta.url), "utf8"));
const ME = "reviewer-bot";
const good = (over = {}) => ({
  cfg, repo: "BuildMy-house/website", selfLogin: ME, reviewNote: "",
  pr: { state: "open", draft: false, mergeable: true, user: { login: "eng-bot" }, base: { ref: "main" }, head: { sha: "a".repeat(40), repo: { full_name: "BuildMy-house/website" } }, title: "t" },
  files: [{ filename: "src/pages/x.astro", status: "modified", changes: 2, patch: "+<p>hi</p>" }],
  checks: { check_runs: [{ name: "check", status: "completed", conclusion: "success" }] },
  ...over,
});
const refused = (r, re) => { assert.equal(r.ok, false); assert.match(r.refusals.join("\n"), re); };

test("config repos equal the compiled-in allowlist", () => assert.deepEqual(cfg.repos.map((r) => r.repo).sort(), [...ALLOWED_REPOS].sort()));
test("a clean PR passes", () => assert.equal(evaluate(good()).ok, true));
test("prod is never a base, even if config lists it", () => {
  const bad = { ...cfg, repos: cfg.repos.map((r) => ({ ...r, baseBranches: [...r.baseBranches, "prod"] })) };
  refused(evaluate(good({ cfg: bad, pr: { ...good().pr, base: { ref: "prod" } } })), /never prod/);
});
test("staging is not a base for any repo (it does not exist for app)", () => {
  refused(evaluate(good({ pr: { ...good().pr, base: { ref: "staging" } } })), /base branch/);
  refused(evaluate(good({ repo: "BuildMy-house/app", pr: { ...good().pr, base: { ref: "staging" }, head: { sha: "b".repeat(40) } }, checks: { check_runs: [{ name: "ci", status: "completed", conclusion: "success" }] } })), /base branch/);
});
test("unknown repo, draft, own PR, fork, conflicts, closed", () => {
  refused(evaluate(good({ repo: "BuildMy-house/other" })), /not one the reviewer may merge/);
  refused(evaluate(good({ pr: { ...good().pr, draft: true } })), /draft/);
  refused(evaluate(good({ cfg: { ...good().cfg, sharedIdentity: false }, pr: { ...good().pr, user: { login: ME } } })), /own identity/);
  refused(evaluate(good({ pr: { ...good().pr, head: { sha: "c", repo: { full_name: "evil/website" } } } })), /fork/);
  refused(evaluate(good({ pr: { ...good().pr, mergeable: false } })), /conflicts/);
  refused(evaluate(good({ pr: { ...good().pr, state: "closed" } })), /not open/);
});
test("sharedIdentity skips the own-PR check; strict config still refuses", () => {
  const own = { ...good().pr, user: { login: ME } };
  const strict = { ...good().cfg, sharedIdentity: false };
  assert.equal(evaluate(good({ cfg: { ...good().cfg, sharedIdentity: true }, pr: own, selfLogin: "" })).ok, true);
  refused(evaluate(good({ cfg: strict, pr: own })), /own identity/);
  refused(evaluate(good({ cfg: strict, selfLogin: "" })), /REVIEWER_LOGIN/);
});
test("guarded paths need a human, including renames away from them", () => {
  for (const p of ["reviewer/lib/policy.mjs", "k8s/base/mcp-gateway/mcp-gateway.yaml", "k8s/base/publisher-secrets-sync/x.yaml", ".github/workflows/ci.yml", "docs/REVIEWER-MERGER.md"]) {
    refused(evaluate(good({ files: [{ filename: p, status: "modified", changes: 1, patch: "+x" }] })), /guarded paths/);
  }
  refused(evaluate(good({ files: [{ filename: "src/ok.md", previous_filename: ".github/workflows/ci.yml", status: "renamed", changes: 1, patch: "+x" }] })), /guarded paths/);
});
test("checks must exist, be green, and include required ones", () => {
  refused(evaluate(good({ checks: { check_runs: [] } })), /no check runs/);
  refused(evaluate(good({ checks: { check_runs: [{ name: "check", status: "completed", conclusion: "failure" }] } })), /not green/);
  refused(evaluate(good({ checks: { check_runs: [{ name: "check", status: "in_progress", conclusion: null }] } })), /not green/);
  refused(evaluate(good({ checks: { check_runs: [{ name: "Cloudflare Pages", status: "completed", conclusion: "success" }] } })), /required check "check"/);
});
test("secrets, env files and unreviewable diffs are refused", () => {
  refused(evaluate(good({ files: [{ filename: "a.js", status: "added", changes: 1, patch: "+const k='github_pat_" + "A".repeat(40) + "'" }] })), /secret/);
  refused(evaluate(good({ files: [{ filename: "a.js", status: "added", changes: 1, patch: "+-----BEGIN RSA PRIVATE KEY-----" }] })), /secret/);
  refused(evaluate(good({ files: [{ filename: ".env.local", status: "added", changes: 1, patch: "+A=1" }] })), /env file/);
  refused(evaluate(good({ files: [{ filename: "big.js", status: "modified", changes: 5000 }] })), /not available/);
  refused(evaluate(good({ files: Array.from({ length: 301 }, (_, i) => ({ filename: `f${i}.md`, status: "added", changes: 1, patch: "+x" })) })), /limit/);
  refused(evaluate(good({ files: [] })), /no changed files/);
});
test("shared components need a recorded review note", () => {
  const files = [{ filename: "src/layouts/Layout.astro", status: "modified", changes: 2, patch: "+<slot/>" }];
  const r = evaluate(good({ files }));
  refused(r, /shared components/);
  assert.equal(r.needsSharedReview, true);
  refused(evaluate(good({ files, reviewNote: "ok" })), /shared components/);
  assert.equal(evaluate(good({ files, reviewNote: "Added a named head slot; no existing output changes." })).ok, true);
});
test("globs: ** crosses directories, * does not", () => {
  assert.ok(globToRe("src/**").test("src/a/b.js"));
  assert.ok(!globToRe("src/*").test("src/a/b.js"));
});
test("blog/* PRs may only change blog content paths", () => {
  const blogHead = { ...good().pr, head: { ...good().pr.head, ref: "blog/hello-world" } };
  assert.equal(evaluate(good({ pr: blogHead, files: [{ filename: "src/content/blog/hello-world.md", status: "added", changes: 1, patch: "+hi" }] })).ok, true);
  refused(evaluate(good({ pr: blogHead, files: [
    { filename: "src/content/blog/hello-world.md", status: "added", changes: 1, patch: "+hi" },
    { filename: "package.json", status: "modified", changes: 1, patch: "+{}" },
  ] })), /blog\/hello-world.*may only change.*also touches: package\.json/);
  refused(evaluate(good({ pr: blogHead, files: [{ filename: "src/layouts/Layout.astro", status: "modified", changes: 1, patch: "+<slot/>" }] })), /may only change/);
  refused(evaluate(good({ pr: blogHead, files: [{ filename: "src/content/blog/moved.md", previous_filename: "src/pages/moved.md", status: "renamed", changes: 1, patch: "+x" }] })), /src\/pages\/moved\.md/);
});
test("branch path rules do not apply to non-blog head branches", () => {
  const head = { ...good().pr, head: { ...good().pr.head, ref: "feature/foo" } };
  assert.equal(evaluate(good({ pr: head, files: [{ filename: "src/layouts/Layout.astro", status: "modified", changes: 1, patch: "+<slot/>" }, { filename: "package.json", status: "modified", changes: 1, patch: "+{}" }], reviewNote: "read the layout change, it is safe" })).ok, true);
});

function fakeGithub({ pr, files, checks }) {
  const calls = [];
  const fetchImpl = async (url, opts) => {
    const path = new URL(url).pathname;
    calls.push({ method: opts.method, path, body: opts.body ? JSON.parse(opts.body) : null });
    const ok = (j) => ({ ok: true, status: 200, text: async () => JSON.stringify(j) });
    if (opts.method === "GET" && /\/pulls\/\d+$/.test(path)) return ok(pr);
    if (opts.method === "GET" && path.endsWith("/files")) return ok(files);
    if (opts.method === "GET" && path.endsWith("/check-runs")) return ok(checks);
    return ok({});
  };
  return { gh: client({ token: "t", fetchImpl }), calls };
}

test("merge pins the reviewed head sha and comments first", async () => {
  const g = good();
  const { gh, calls } = fakeGithub(g);
  const r = await merge({ gh, cfg, repo: g.repo, n: 7, selfLogin: ME, reviewNote: "" });
  assert.equal(r.merged, true);
  const put = calls.find((c) => c.method === "PUT");
  assert.equal(put.body.sha, "a".repeat(40));
  assert.equal(put.body.merge_method, "squash");
  assert.ok(calls.findIndex((c) => c.path.endsWith("/comments")) < calls.indexOf(put));
});
test("a refused merge never calls the merge endpoint and says why", async () => {
  const g = good({ checks: { check_runs: [{ name: "check", status: "completed", conclusion: "failure" }] } });
  const { gh, calls } = fakeGithub(g);
  await assert.rejects(() => merge({ gh, cfg, repo: g.repo, n: 7, selfLogin: ME, reviewNote: "" }), Refused);
  assert.equal(calls.some((c) => c.method === "PUT"), false);
  assert.match(calls.find((c) => c.path.endsWith("/comments")).body.body, /not merging/);
});
