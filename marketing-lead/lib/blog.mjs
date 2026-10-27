// Blog post formatting and the branch + PR flow. The blog lives in a config-driven repo (today
// BuildMy-house/hermees-diary, src/content/blog/*.md, front matter title/date/category/tags/excerpt).
// Never pushes main: the push goes through the guarded git wrapper and only to blog/<slug>.
import { Refused } from "./approval.mjs";

export const slugify = (t) => String(t).toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60).replace(/-+$/, "");
const q = (s) => JSON.stringify(String(s));

export function renderPost(post, cfg, today = new Date()) {
  const b = cfg.blog;
  if (!post.title || post.title.length > b.titleMax) throw new Refused(`title missing or over ${b.titleMax} chars`);
  if (!b.categories.includes(post.category)) throw new Refused(`category must be one of ${b.categories.join(", ")}`);
  const ex = post.excerpt || "";
  if (ex.length < b.excerptMin || ex.length > b.excerptMax) throw new Refused(`excerpt must be ${b.excerptMin}-${b.excerptMax} chars (is ${ex.length})`);
  if (!post.body || !post.body.trim()) throw new Refused("body is empty");
  const tags = Array.isArray(post.tags) ? post.tags : [];
  const date = post.date || today.toISOString().slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Refused("date must be YYYY-MM-DD");
  const slug = slugify(post.slug || post.title);
  if (!slug) throw new Refused("could not derive a slug");
  const fm = `---\ntitle: ${q(post.title)}\ndate: ${date}\ncategory: ${q(post.category)}\ntags: [${tags.map(q).join(", ")}]\nexcerpt: ${q(ex)}\n---\n\n`;
  return { slug, path: `${b.contentDir}/${date}-${slug}.md`, branch: `blog/${slug}`, content: fm + post.body.trim() + "\n" };
}

// run(cmdArgs, {cwd}) executes the guarded git; fetchImpl talks to the GitHub API.
export async function openBlogPr({ rendered, repoDir, cfg, token, run, fetchImpl = fetch, write, title, body }) {
  const b = cfg.blog;
  run(["checkout", "-B", rendered.branch, `origin/${b.baseBranch}`], { cwd: repoDir });
  write(`${repoDir}/${rendered.path}`, rendered.content);
  run(["add", rendered.path], { cwd: repoDir });
  run(["commit", "-m", `blog: ${title || rendered.slug}`], { cwd: repoDir });
  run(["push", "-u", "origin", rendered.branch], { cwd: repoDir });
  const res = await fetchImpl(`https://api.github.com/repos/${b.repo}/pulls`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "buildmy-house-marketing-lead" },
    body: JSON.stringify({ title: title || `Blog: ${rendered.slug}`, head: rendered.branch, base: b.baseBranch, body: body || "Draft from the marketing-lead agent. Merging publishes the post: review, then a human or Board-approved merge." }),
  });
  const j = await res.json();
  if (!res.ok) throw new Error(`GitHub PR failed: HTTP ${res.status} ${String(j.message || "").slice(0, 200)}`);
  return { pr: j.html_url, branch: rendered.branch };
}
