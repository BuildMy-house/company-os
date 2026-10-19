import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { findRepo, openSitePr, startBranch, syncRepo, verifyCommands } from "../lib/sites.mjs";
import { ALLOWED_REPOS } from "../lib/guard.mjs";

const cfg = JSON.parse(readFileSync(new URL("../config/marketing-lead.json", import.meta.url), "utf8"));

test("every configured repo is allowlisted, has a base, verify commands and deploy notes", () => {
  assert.equal(cfg.repos.length, 3);
  for (const r of cfg.repos) {
    assert.ok(ALLOWED_REPOS.includes(r.repo));
    assert.equal(r.baseBranch, "main");
    assert.deepEqual(verifyCommands(r).map((c) => c.join(" ")), ["npm ci", "npm run lint", "npm run check", "npm run build"]);
    assert.ok(r.deploy.length > 40);
  }
});

test("the ConfigMap in marketing-lead.yaml carries exactly config/marketing-lead.json", () => {
  const y = readFileSync(new URL("../../k8s/base/marketing-lead/marketing-lead.yaml", import.meta.url), "utf8");
  const a = y.indexOf("  marketing-lead.json: |\n") + "  marketing-lead.json: |\n".length;
  const b = y.indexOf("\n---\n", a);
  const block = y.slice(a, b).split("\n").map((l) => l.slice(4)).join("\n");
  assert.deepEqual(JSON.parse(block), cfg);
  assert.match(y, /MARKETING_LEAD_ALLOWED_REMOTES\n\s+value: https:\/\/github.com\/BuildMy-house\/website,https:\/\/github.com\/BuildMy-house\/hermees-diary,https:\/\/github.com\/BuildMy-house\/observer-website\n/);
});

test("findRepo resolves by name or slug and refuses anything else", () => {
  assert.equal(findRepo(cfg, "website").repo, "BuildMy-house/website");
  assert.equal(findRepo(cfg, "BuildMy-house/observer-website").name, "observer-website");
  for (const bad of ["app", "company-os", "BuildMy-house/app", "", "../x"]) assert.throws(() => findRepo(cfg, bad), /unknown repo/);
  assert.throws(() => findRepo({ repos: [{ name: "x", repo: "BuildMy-house/company-os" }] }, "x"), /allowlist/);
});

test("sync clones when absent, fetches when present, refuses a dirty tree, always lands on origin/base", () => {
  const r = findRepo(cfg, "hermees-diary");
  const calls = [];
  const run = (a, o) => calls.push([a, o?.cwd]);
  syncRepo({ r, work: "/w", run, exists: () => false, clean: () => true });
  assert.deepEqual(calls[0], [["clone", "https://github.com/BuildMy-house/hermees-diary.git", "/w/hermees-diary"], undefined]);
  assert.deepEqual(calls.at(-1), [["checkout", "-B", "main", "origin/main"], "/w/hermees-diary"]);
  calls.length = 0;
  syncRepo({ r, work: "/w", run, exists: () => true, clean: () => true });
  assert.deepEqual(calls[0][0], ["fetch", "origin"]);
  assert.throws(() => syncRepo({ r, work: "/w", run, exists: () => true, clean: () => false }), /uncommitted/);
});

test("branch names must be blog/<slug> or seo/<slug>", () => {
  const r = findRepo(cfg, "website");
  const calls = [];
  startBranch({ r, work: "/w", run: (a) => calls.push(a), branch: "blog/fix-hero" });
  assert.deepEqual(calls[0], ["checkout", "-B", "blog/fix-hero", "origin/main"]);
  startBranch({ r, work: "/w", run: (a) => calls.push(a), branch: "seo/ctr-fix" });
  assert.deepEqual(calls.at(-1), ["checkout", "-B", "seo/ctr-fix", "origin/main"]);
  for (const bad of ["main", "reports/x", "feature/x", "blog/UP", "seo/UP", ""]) assert.throws(() => startBranch({ r, work: "/w", run: () => {}, branch: bad }), /branch must match/);
});

test("repo pr commits, pushes only blog/<slug>, opens the PR against the base, never the base itself", async () => {
  const r = findRepo(cfg, "observer-website");
  const cmds = []; let req;
  const fetchImpl = async (url, o) => { req = { url, body: JSON.parse(o.body) }; return { ok: true, json: async () => ({ html_url: "https://gh/pr/9" }) }; };
  const res = await openSitePr({ r, work: "/w", run: (a) => cmds.push(a), branch: "blog/copy-fix", message: "fix: copy", token: "t", fetchImpl, hasChanges: () => true });
  assert.deepEqual(res, { pr: "https://gh/pr/9", repo: "BuildMy-house/observer-website", branch: "blog/copy-fix" });
  assert.deepEqual(cmds.map((c) => c[0]), ["checkout", "add", "commit", "push"]);
  assert.deepEqual(cmds.at(-1), ["push", "-u", "origin", "blog/copy-fix"]);
  assert.equal(req.url, "https://api.github.com/repos/BuildMy-house/observer-website/pulls");
  assert.deepEqual([req.body.head, req.body.base], ["blog/copy-fix", "main"]);
  await assert.rejects(openSitePr({ r, work: "/w", run: () => {}, branch: "main", message: "m", token: "t", hasChanges: () => true }), /branch must match/);
  await assert.rejects(openSitePr({ r, work: "/w", run: () => {}, branch: "blog/x", message: "m", token: "t", hasChanges: () => false }), /nothing to commit/);
  await assert.rejects(openSitePr({ r, work: "/w", run: () => {}, branch: "blog/x", message: "", token: "t", hasChanges: () => true }), /message/);
});
