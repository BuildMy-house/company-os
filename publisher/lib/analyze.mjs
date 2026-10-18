// Deterministic blog-post checks. Returns a short report; does not judge quality, only measures.
// existing: [{slug,title,body}] for duplicate detection.
const words = (t) => (t.toLowerCase().match(/[a-z0-9']+/g) || []);
const shingles = (t, n = 5) => { const w = words(t), s = new Set(); for (let i = 0; i + n <= w.length; i++) s.add(w.slice(i, i + n).join(" ")); return s; };

export function parseFrontMatter(md) {
  const m = md.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!m) return { fm: {}, body: md };
  const fm = {};
  for (const line of m[1].split("\n")) { const k = line.match(/^(\w+):\s*(.*)$/); if (k) fm[k[1]] = k[2].replace(/^"(.*)"$/, "$1"); }
  return { fm, body: m[2] };
}

export function fleschReadingEase(text) {
  const sentences = Math.max(1, (text.match(/[.!?]+(\s|$)/g) || []).length);
  const w = words(text);
  if (!w.length) return 0;
  const syl = w.reduce((n, x) => n + Math.max(1, (x.replace(/e$/, "").match(/[aeiouy]+/g) || []).length), 0);
  return Math.round((206.835 - 1.015 * (w.length / sentences) - 84.6 * (syl / w.length)) * 10) / 10;
}

export function analyzePost(md, { existing = [], titleMax = 70, excerptMin = 70, excerptMax = 170 } = {}) {
  const { fm, body } = parseFrontMatter(md);
  const findings = [];
  const add = (level, msg) => findings.push({ level, msg });
  const title = fm.title || "";
  if (!title) add("fail", "no title in front matter"); else if (title.length > titleMax) add("warn", `title is ${title.length} chars (max ${titleMax}); search results truncate it`);
  const ex = fm.excerpt || "";
  if (!ex) add("fail", "no excerpt/meta description"); else if (ex.length < excerptMin || ex.length > excerptMax) add("warn", `excerpt is ${ex.length} chars (target ${excerptMin}-${excerptMax})`);
  const headings = [...body.matchAll(/^(#{1,6})\s+(.+)$/gm)].map((h) => ({ level: h[1].length, text: h[2] }));
  if (headings.some((h) => h.level === 1)) add("warn", "body has an H1; the layout already renders the title");
  if (!headings.some((h) => h.level === 2)) add("warn", "no H2 headings; add structure");
  headings.forEach((h, i) => { if (i && h.level - headings[i - 1].level > 1) add("warn", `heading jump to "${h.text}" (H${headings[i - 1].level} to H${h.level})`); });
  const links = [...body.matchAll(/\[([^\]]*)\]\(([^)\s]+)\)/g)];
  if (!links.length) add("warn", "no links");
  for (const [, text, href] of links) { if (!text.trim()) add("warn", `link with empty text: ${href}`); if (/^(javascript:|http:\/\/)/i.test(href)) add("warn", `insecure or unsafe link: ${href}`); }
  const plain = body.replace(/```[\s\S]*?```/g, " ").replace(/[#>*_`]|\[([^\]]*)\]\([^)]*\)/g, "$1");
  const wc = words(plain).length;
  if (wc < 300) add("warn", `only ${wc} words`);
  const fre = fleschReadingEase(plain);
  if (fre < 50) add("warn", `Flesch reading ease ${fre} (hard to read; aim for 50+)`);
  const mine = shingles(plain);
  for (const o of existing) {
    const theirs = shingles(o.body || "");
    if (!mine.size || !theirs.size) continue;
    let shared = 0; for (const s of mine) if (theirs.has(s)) shared++;
    const overlap = shared / mine.size;
    if (overlap > 0.2) add("fail", `${Math.round(overlap * 100)}% of 5-word phrases already appear in "${o.title || o.slug}"`);
    if (o.title && title && o.title.toLowerCase() === title.toLowerCase()) add("fail", `same title as existing post ${o.slug}`);
  }
  return { words: wc, readingEase: fre, headings: headings.length, links: links.length, findings, ok: !findings.some((f) => f.level === "fail") };
}

export const formatReport = (r) => [`words ${r.words}, reading ease ${r.readingEase}, headings ${r.headings}, links ${r.links}`, ...r.findings.map((f) => `${f.level.toUpperCase()}: ${f.msg}`), r.ok ? "RESULT: no blocking findings" : "RESULT: blocking findings"].join("\n");
