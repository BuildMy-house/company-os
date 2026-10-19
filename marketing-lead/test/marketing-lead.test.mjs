import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { Ledger, Refused, requireApproval } from "../lib/approval.mjs";
import { analyzePost } from "../lib/analyze.mjs";
import { openBlogPr, renderPost, slugify } from "../lib/blog.mjs";
import { buildSql } from "../lib/dbq.mjs";
import { assertNoSecrets } from "../lib/memory.mjs";
import { checkRedditPost, publishReddit } from "../lib/reddit.mjs";

const cfg = JSON.parse(readFileSync(new URL("../config/marketing-lead.json", import.meta.url)));
const REF = "HERMES-APPROVAL-2026-10-15-001";
const tmpLedger = () => new Ledger(join(mkdtempSync(join(tmpdir(), "led-")), "l.jsonl"));
const rcfg = (over = {}) => ({ ...cfg, reddit: { ...cfg.reddit, allowedSubreddits: ["homedesign"], ...over } });
const post = (o = {}) => ({ subreddit: "homedesign", title: "t", text: "b", approvalRef: REF, ...o });

test("approval gate: refuses missing or malformed refs, accepts a well-formed one", () => {
  for (const bad of [undefined, null, "", "short", "has space here!!", 42]) assert.throws(() => requireApproval(bad, "x"), Refused);
  assert.equal(requireApproval(REF, "x"), REF);
});

test("reddit: no approval_ref means nothing is sent", async () => {
  let called = 0;
  await assert.rejects(publishReddit(post({ approvalRef: undefined }), rcfg(), tmpLedger(), {}, async () => { called++; }), /approval_ref/);
  assert.equal(called, 0);
});

test("reddit: allowlist, empty allowlist, and daily cap", () => {
  const l = tmpLedger();
  assert.throws(() => checkRedditPost(post({ subreddit: "other" }), rcfg(), l), /allowlist/);
  assert.throws(() => checkRedditPost(post(), rcfg({ allowedSubreddits: [] }), l), /empty/);
  assert.equal(checkRedditPost(post({ subreddit: "r/HomeDesign" }), rcfg(), l), "HomeDesign");
  l.append({ ts: new Date().toISOString(), channel: "reddit", approval_ref: "OTHER-REF-0001" });
  assert.throws(() => checkRedditPost(post(), rcfg(), l), /cap/);
  assert.throws(() => checkRedditPost(post({ approvalRef: "OTHER-REF-0001" }), rcfg({ dailyPostCap: 9 }), l), /already used/);
});

test("reddit: approved post is submitted via the API and ledgered", async () => {
  const l = tmpLedger(); const seen = [];
  const f = async (url, init) => { seen.push(url); return { ok: true, status: 200, json: async () => (url.includes("access_token") ? { access_token: "t" } : { json: { errors: [], data: { url: "https://reddit/x" } } }) }; };
  const r = await publishReddit(post(), rcfg(), l, { REDDIT_USERNAME: "u", REDDIT_CLIENT_ID: "a", REDDIT_CLIENT_SECRET: "b", REDDIT_PASSWORD: "p" }, f);
  assert.equal(r.url, "https://reddit/x");
  assert.equal(l.entries().length, 1);
  assert.ok(seen[1].endsWith("/api/submit"));
});

test("blog: renders the site's front matter and refuses bad input", () => {
  const p = { title: "Why we built it", category: "decisions", tags: ["a", "b"], excerpt: "x".repeat(100), body: "## Hi\n\ntext" };
  const r = renderPost(p, cfg, new Date("2026-10-15"));
  assert.equal(r.branch, "blog/why-we-built-it");
  assert.equal(r.path, "src/content/blog/2026-10-15-why-we-built-it.md");
  assert.match(r.content, /^---\ntitle: "Why we built it"\ndate: 2026-10-15\ncategory: "decisions"\ntags: \["a", "b"\]\nexcerpt: "x+"\n---\n\n## Hi/);
  assert.throws(() => renderPost({ ...p, category: "nope" }, cfg), /category/);
  assert.throws(() => renderPost({ ...p, excerpt: "short" }, cfg), /excerpt/);
  assert.equal(slugify("  Hello, World!! "), "hello-world");
});

test("blog pr: branch from base, push only blog/<slug>, never main", async () => {
  const cmds = [];
  const rendered = renderPost({ title: "T one", category: "decisions", excerpt: "x".repeat(100), body: "b" }, cfg, new Date("2026-10-15"));
  const r = await openBlogPr({ rendered, repoDir: "/r", cfg, token: "tok", run: (a) => cmds.push(a), write: () => {}, fetchImpl: async () => ({ ok: true, json: async () => ({ html_url: "https://gh/pr/1" }) }) });
  const push = cmds.find((c) => c[0] === "push");
  assert.deepEqual(push, ["push", "-u", "origin", "blog/t-one"]);
  assert.ok(!cmds.some((c) => c.includes("main") && c[0] === "push"));
  assert.equal(r.pr, "https://gh/pr/1");
});

test("analyze: flags long title, missing links, duplicate content", () => {
  const body = "## A\n\n" + "The quick brown fox jumps over the lazy dog near the river bank today. ".repeat(40);
  const md = `---\ntitle: "${"T".repeat(80)}"\ndate: 2026-10-15\nexcerpt: "short"\n---\n\n${body}`;
  const r = analyzePost(md, { existing: [{ slug: "old", title: "Old", body }] });
  const msgs = r.findings.map((f) => f.msg).join("|");
  assert.match(msgs, /title is 80/); assert.match(msgs, /excerpt is 5/); assert.match(msgs, /no links/); assert.match(msgs, /already appear in "Old"/);
  assert.equal(r.ok, false);
  const clean = analyzePost(`---\ntitle: "Fine"\nexcerpt: "${"y".repeat(100)}"\n---\n\n## A\n\nSee [docs](https://x.io). ${"Short plain words help people read. ".repeat(60)}`);
  assert.equal(clean.ok, true);
});

test("db: named queries only, bounded integer parameters", () => {
  assert.match(buildSql("top-pages", { days: 7 }), /current_date - 7/);
  assert.throws(() => buildSql("drop table x"), /unknown query/);
  assert.throws(() => buildSql("top-pages", { days: "7; drop" }), /integer/);
  assert.throws(() => buildSql("top-pages", { evil: 1 }), /unknown parameter/);
  assert.throws(() => buildSql("top-pages", { days: 9999 }), /1\.\.365/);
});

test("memory: refuses credentials and bloated entries", () => {
  assert.throws(() => assertNoSecrets("token: abcdef123456"), /credential/);
  assert.throws(() => assertNoSecrets("postgresql://u:pw@h/db"), /credential/);
  assert.throws(() => assertNoSecrets("x\n".repeat(80)), /too long/);
  assertNoSecrets("Decision: lead with the 3D editor in post titles.");
});
