import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { ALLOWED_REPOS, checkPushArgs, checkRefLines, checkRemoteUrl, parseGlobalArgs, checkConfigArgs } from "../lib/guard.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

// ---- pure rules --------------------------------------------------------------------------
const refused = (fn) => assert.throws(fn, (e) => e.guard === true);
const head = () => "blog/current";

test("push: only blog/ and seo/ branches allowed, everything else (incl. reports/) refused", () => {
  assert.deepEqual(checkPushArgs(["-u", "origin", "blog/my-post"], head).branches, ["blog/my-post"]);
  assert.deepEqual(checkPushArgs(["origin", "blog/a:refs/heads/blog/b"], head).branches, ["blog/b"]);
  assert.deepEqual(checkPushArgs(["origin", "HEAD"], head).branches, ["blog/current"]);
  assert.deepEqual(checkPushArgs(["-u", "origin", "seo/title-ctr-fix"], head).branches, ["seo/title-ctr-fix"]);
  assert.deepEqual(checkPushArgs(["origin", "HEAD"], () => "seo/current").branches, ["seo/current"]);
  for (const bad of ["reports/2026-10-weekly", "refs/heads/reports/x", "main", "master", "prod", "staging", "refs/heads/main", "blog/a:main", "HEAD:main", "blog/../main", "blog/", "blog/UPPER", "blog", "refs/tags/blog/x", "blogger/x", "x/blog/y", "seo/", "seo", "seo/UPPER", "seo/../main", "seo/a:main", "seos/x", "x/seo/y", "refs/tags/seo/x"]) {
    refused(() => checkPushArgs(["origin", bad], head));
  }
});

test("push: force, delete, mirror, all, tags, hook bypass and abbreviations are refused", () => {
  for (const flag of ["-f", "--force", "--force-with-lease", "--force-with-l", "--no-verify", "--no-ver", "--all", "--mirror", "--tags", "--follow-tags", "--delete", "-d", "--del", "--prune", "--repo=x", "--receive-pack=sh", "--exec=sh", "-o", "--push-option=x"]) {
    refused(() => checkPushArgs([flag, "origin", "blog/x"], head));
  }
  refused(() => checkPushArgs(["origin", "+blog/x"], head));
  refused(() => checkPushArgs(["origin", ":blog/x"], head));
  refused(() => checkPushArgs(["origin", "blog/*:blog/*"], head));
  refused(() => checkPushArgs(["origin"], head), "needs an explicit branch");
  refused(() => checkPushArgs([], head));
});

test("global args: dangerous -c overrides and options are refused", () => {
  for (const kv of ["core.hooksPath=/dev/null", "core.hookspath=/x", "alias.push=!sh", "url.x.insteadOf=y", "remote.origin.url=z", "push.default=matching", "credential.helper=x", "include.path=/x", "core.sshCommand=sh"]) {
    refused(() => parseGlobalArgs(["-c", kv, "push"]));
  }
  refused(() => parseGlobalArgs(["--git-dir=/x", "push"]));
  refused(() => parseGlobalArgs(["--exec-path=/x", "push"]));
  assert.deepEqual(parseGlobalArgs(["-c", "user.name=x", "-C", "/w", "status"]).rest, ["status"]);
  refused(() => checkConfigArgs(["core.hooksPath", "/dev/null"]));
  refused(() => checkConfigArgs(["--unset", "core.hooksPath"]));
  checkConfigArgs(["--get", "remote.origin.url"]);
});

test("the compiled-in repo allowlist is exactly the three site repos", () => {
  assert.deepEqual([...ALLOWED_REPOS], ["BuildMy-house/website", "BuildMy-house/hermees-diary", "BuildMy-house/observer-website"]);
  const cfg = JSON.parse(readFileSync(join(root, "config", "marketing-lead.json"), "utf8"));
  assert.deepEqual(cfg.repos.map((r) => r.repo).sort(), [...ALLOWED_REPOS].sort(), "marketing-lead.json repos must equal ALLOWED_REPOS");
  assert.ok(ALLOWED_REPOS.includes(cfg.blog.repo));
  assert.equal(cfg.reports, undefined, "no reports-to-git config");
});

test("remote URL: env list can narrow but never widen the hard-coded three repos", () => {
  const all = ALLOWED_REPOS.map((r) => `https://github.com/${r}`);
  for (const u of all) checkRemoteUrl(u, all);
  checkRemoteUrl(all[0], [all[0]]);
  refused(() => checkRemoteUrl(all[1], [all[0]]), "env narrows");
  for (const bad of ["https://github.com/BuildMy-house/company-os", "https://github.com/BuildMy-house/app", "https://github.com/evil/website", "git@github.com:BuildMy-house/website.git", "https://gitlab.com/BuildMy-house/website"]) {
    refused(() => checkRemoteUrl(bad, [...all, bad]), `${bad} is refused even if the env var lists it`);
  }
});

test("remote URL must be the configured repo and carry no credentials", () => {
  const allowed = ["https://github.com/BuildMy-house/website.git"];
  checkRemoteUrl("https://github.com/BuildMy-house/website", allowed);
  checkRemoteUrl("https://github.com/BuildMy-house/website.git/", allowed);
  refused(() => checkRemoteUrl("https://github.com/evil/website.git", allowed));
  refused(() => checkRemoteUrl("https://x:tok@github.com/BuildMy-house/website.git", allowed));
  refused(() => checkRemoteUrl("https://github.com/BuildMy-house/website.git", []));
});

test("pre-push ref lines: main, deletes and tags refused", () => {
  const sha = "a".repeat(40);
  checkRefLines(`refs/heads/x ${sha} refs/heads/blog/x ${"0".repeat(40)}\n`);
  checkRefLines(`refs/heads/x ${sha} refs/heads/seo/x ${"0".repeat(40)}\n`);
  refused(() => checkRefLines(`refs/heads/x ${sha} refs/heads/main ${sha}\n`));
  refused(() => checkRefLines(`(delete) ${"0".repeat(40)} refs/heads/blog/x ${sha}\n`));
  refused(() => checkRefLines(`refs/tags/v1 ${sha} refs/tags/v1 ${"0".repeat(40)}\n`));
  refused(() => checkRefLines(`refs/heads/x ${sha} refs/heads/blog/x ${sha}\nrefs/heads/x ${sha} refs/heads/main ${sha}\n`));
});

// ---- real git, real bare remote -----------------------------------------------------------
function sh(cmd, args, opts = {}) {
  return spawnSync(cmd, args, { encoding: "utf8", ...opts });
}

function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), "marketing-lead-guard-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const remote = join(dir, "remote.git");
  const work = join(dir, "work");
  const base = { ...process.env, GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t", GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1" };
  sh("git", ["init", "--bare", "-b", "main", remote], { env: base });
  sh("git", ["clone", remote, work], { env: base });
  writeFileSync(join(work, "a.txt"), "1\n");
  sh("git", ["-C", work, "add", "."], { env: base });
  sh("git", ["-C", work, "commit", "-m", "init"], { env: base });
  const real = sh("git", ["-C", work, "push", "origin", "HEAD:main"], { env: base });
  assert.equal(real.status, 0, "fixture seeds main using plain git before the guard exists: " + real.stderr);
  sh("git", ["-C", work, "checkout", "-b", "blog/foo"], { env: base });
  writeFileSync(join(work, "post.md"), "hi\n");
  sh("git", ["-C", work, "add", "."], { env: base });
  sh("git", ["-C", work, "commit", "-m", "post"], { env: base });
  // Real-git tests push to a local bare repo, which the compiled-in github-only allowlist rightly
  // refuses. Run a COPY of the guard whose hard list also names this fixture remote; the shipped
  // list itself is covered by the pure-rule tests above.
  const copy = join(dir, "guard");
  for (const d of ["bin", "lib", "hooks"]) cpSync(join(root, d), join(copy, d), { recursive: true });
  cpSync(join(root, "package.json"), join(copy, "package.json"));
  const gp = join(copy, "lib", "guard.mjs");
  const src = readFileSync(gp, "utf8");
  const patched = src.replace("hardAllowed = ALLOWED_REPOS.map(repoUrl)", `hardAllowed = [...ALLOWED_REPOS.map(repoUrl), ${JSON.stringify(remote)}]`);
  assert.notEqual(patched, src, "test seam present in guard.mjs");
  writeFileSync(gp, patched);
  const wrapper = join(copy, "bin", "git");
  const hooks = join(copy, "hooks");
  const guarded = { ...base, MARKETING_LEAD_ALLOWED_REMOTES: remote, MARKETING_LEAD_REAL_GIT: sh("which", ["git"]).stdout.trim() };
  const refs = () => sh("git", ["-C", remote, "for-each-ref", "--format=%(refname)"], { env: base }).stdout.split("\n").filter(Boolean);
  return { work, remote, base, guarded, refs, wrapper, hooks };
}

test("wrapper: push to main is refused, push to blog/foo succeeds", (t) => {
  const f = fixture(t);
  for (const args of [["push", "origin", "main"], ["push", "origin", "HEAD:main"], ["push", "origin", "blog/foo:main"], ["push", "--force", "origin", "blog/foo"], ["push", "origin", ":blog/foo"], ["push", "--no-verify", "origin", "blog/foo"], ["push", "origin", "--all"]]) {
    const r = sh(f.wrapper, ["-C", f.work, ...args], { env: f.guarded });
    assert.equal(r.status, 77, `${args.join(" ")} must be refused by the wrapper: ${r.stderr}`);
    assert.match(r.stderr, /refused/);
  }
  assert.deepEqual(f.refs(), ["refs/heads/main"], "nothing but the seeded main exists on the remote after every refused push");

  const ok = sh(f.wrapper, ["-C", f.work, "push", "-u", "origin", "blog/foo"], { env: f.guarded });
  assert.equal(ok.status, 0, ok.stderr);
  assert.deepEqual(f.refs(), ["refs/heads/blog/foo", "refs/heads/main"]);
  const head = sh(f.wrapper, ["-C", f.work, "push", "origin", "HEAD"], { env: f.guarded });
  assert.equal(head.status, 0, "HEAD resolves to the current blog/ branch: " + head.stderr);
});

test("wrapper: cannot disable the hook with -c or config writes", (t) => {
  const f = fixture(t);
  assert.equal(sh(f.wrapper, ["-C", f.work, "-c", "core.hooksPath=/dev/null", "push", "origin", "blog/foo"], { env: f.guarded }).status, 77);
  assert.equal(sh(f.wrapper, ["-C", f.work, "config", "core.hooksPath", "/dev/null"], { env: f.guarded }).status, 77);
  assert.deepEqual(f.refs(), ["refs/heads/main"]);
});

test("hook layer alone (plain git, no wrapper): main refused, blog/foo allowed, delete refused", (t) => {
  const f = fixture(t);
  const gitHooked = (...args) => sh("git", ["-C", f.work, "-c", `core.hooksPath=${f.hooks}`, ...args], { env: f.guarded });
  const toMain = gitHooked("push", "origin", "HEAD:main");
  assert.notEqual(toMain.status, 0);
  assert.match(toMain.stderr, /\[marketing-lead-pre-push\].*not an allowed branch/);
  assert.notEqual(gitHooked("push", "--force", "origin", "HEAD:main").status, 0);
  assert.notEqual(gitHooked("push", "origin", "HEAD:refs/tags/v1").status, 0);
  assert.deepEqual(f.refs(), ["refs/heads/main"]);

  const ok = gitHooked("push", "origin", "HEAD:blog/foo");
  assert.equal(ok.status, 0, ok.stderr);
  assert.deepEqual(f.refs(), ["refs/heads/blog/foo", "refs/heads/main"]);
  const del = gitHooked("push", "origin", ":blog/foo");
  assert.notEqual(del.status, 0, "deleting a remote branch is refused");
  assert.deepEqual(f.refs(), ["refs/heads/blog/foo", "refs/heads/main"]);

  const wrongRemote = sh("git", ["-C", f.work, "-c", `core.hooksPath=${f.hooks}`, "push", "origin", "HEAD:blog/other"], { env: { ...f.guarded, MARKETING_LEAD_ALLOWED_REMOTES: "/some/other/repo" } });
  assert.notEqual(wrongRemote.status, 0, "pushes to a repo that is not the configured one are refused");
  assert.match(wrongRemote.stderr, /not the configured repo/);
});
