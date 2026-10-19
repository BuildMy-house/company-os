# buildmy.house SEO Playbook

This playbook is the CEO agent's single source of truth for SEO work on buildmy.house. It defines the title standard, content rules, structured-data requirements, keyword/intent buckets, programmatic-page gates, and the experiment process. Every non-obvious claim cites a numbered source from the `## Sources` section, and is labelled either **Google says** (from Google's own documentation), **practitioner heuristic** (industry convention, not Google-documented), or **our hypothesis** (unverified — treat as an assumption to test, not a fact).

## 1. Purpose

- buildmy.house is a browser-based home, floor-plan, and 3D house-design tool with an "Own the sandbox" positioning: the free lane to design a house in the browser — the "Google Docs of house design".
- The website (Astro static site, deployed to Cloudflare Pages) is new. The GSC property is `sc-domain:buildmy.house` and has no data yet. Expect small numbers and treat them accordingly (Section 9).
- The CEO agent owns SEO: this playbook defines what is true; the `seo` skill (`hermes/skills/seo/SKILL.md`) defines the weekly procedure; the `## Experiment log` at the bottom of this file records what was tried and what happened.

## 2. Titles and snippets

**Google says** every page needs a `<title>`, and "descriptive and concise" wins — source [1]. There is **no hard length rule**: Google truncates titles "typically to fit the device width" [1]. Snippets are generated automatically from page content, sometimes from the meta description; there is no length limit there either — the same device-width truncation applies [2]. Meta descriptions should be unique per page; for database-driven (programmatic) pages Google explicitly says programmatic generation is fine and encouraged [2]. Boilerplate or "micro-boilerplate" titles (the same short string on many pages) are a listed common problem [1]; site name may appear with a delimiter (hyphen/colon/pipe) [1].

**Our standard** (our decision):

- Every markdown page title follows `<Page> · buildmy.house` (e.g. `Floor Plan Tool · buildmy.house`).
- Meta descriptions are written per page, roughly 1–2 sentences, naming what the page lets the visitor do.
- **Practitioner heuristic**: keeping the branded suffix out of the truncation-critical first ~55–60 characters is the safest layout — front-load the page-specific words. This is a layout rule, not a length rule: Google still says there is no cutoff [1].
- Title changes recrawl in days to weeks [1] — do not judge a title experiment for at least two weekly cycles.

## 3. Helpful, people-first content

**Google says** content is evaluated on being helpful, reliable, people-first — not search-engine-first. Explicitly: word count is **not** a factor ("No, we don't" have a preferred word count) [3]. E-E-A-T is not a direct ranking factor, but trust is the most important member of it [3]. The main-content qualities Google names: effort, originality, talent/skill, accuracy [3]. The "who / how / why" self-check: who wrote it, how it was produced (tools/sources), and why it exists (to help people, not to rank) [3]. Fabricated authorship is deception [3]. Bulk-generating AI content without oversight reads as low effort [3]; automation used to manipulate rankings is a spam-policy violation [3] [8]. Word count, hidden text, keyword stuffing, and keywords-in-domain are all named "don't obsess" items in the starter guide [6].

**Our rules**:

- Write for a visitor who wants to design a house. If a page would not help that visitor, it does not ship.
- No padded word counts. Dense, complete, and done beats long.
- If genAI assists drafting, a human fact-checks numbers, product claims, names, and dates **before publishing** [9], and the page discloses how it was made where relevant (a short "How this was made" note) [9].
- Author bylines are real people or the company. Never invent a person. [3]

## 4. Core Web Vitals

**Google says** the thresholds are: LCP ≤ 2.5s, INP < 200ms, CLS < 0.1 — each assessed at the 75th percentile of page loads [4]. The Core Web Vitals report in Search Console shows field data per page group [4]. (Fetched 2026-10-19 from Google's CWV documentation [4]; web.dev p75 framing not separately verified — same thresholds, same source.)

**Our standard**: every shipped page type (tool, template, article) is checked in the CWV report after enough field data accrues, and lab-checked before launch. The Astro site is static and lightweight; if a page type shows a red CWV group, that page type gets a fix ticket before further content expansion. One change per page per experiment (Section 9) so causes stay readable.

## 5. Structured data

**Google says** JSON-LD is the recommended technique; markup must be truthful and represent visible page content — marking up invisible or untruthful content violates the general structured-data guidelines and risks manual actions or losing rich-result eligibility [11]. Validate with the Rich Results Test; use URL Inspection on a few deployed pages before rolling out site-wide, and submit a sitemap [5] [11].

- **Article / BlogPosting**: there are **no required properties** — all properties are recommended [5]. **Our standard** (our decision, within Google's recommended set): `headline`, `image`, `datePublished` and `dateModified` (ISO 8601 with timezone), and `author` as a real array of `Person` or `Organization` objects — `author.name` contains only the name, `author.url` / `sameAs` links to a real page. Authors are real (Section 3). Recommended by Google: long headlines may be truncated, images must be crawlable/indexable with standard aspect ratios [5].
- **SoftwareApplication**: **required** by Google: `name`, `offers` (an `Offer` with `price` — 0 is acceptable for a free app; `priceCurrency` recommended if price > 0), **and** at least one of `aggregateRating` or `review` — no rating/review means the markup does not qualify [10]. Recommended: `applicationCategory` (`DesignApplication` is in the supported list and fits us), `operatingSystem` (e.g. `Web`); `WebApplication` / `MobileApplication` subtypes are supported; `VideoGame` alone is not eligible for a rich result [10]. **Our standard**: the tool pages carry `SoftwareApplication` + `WebApplication` with `applicationCategory: DesignApplication`, `price: 0`, `aggregateRating` added only once real ratings exist — never invented.
- **Organization**: on the home page; helps Google show logo/name/contacts in the knowledge panel [12]. Logo image at least 112×112px with a square-ish aspect ratio (≤3:1); one Organization per page [12].
- **BreadcrumbList**: every breadcrumb item must be a valid URL; list order reflects page hierarchy — which need not strictly match the URL path [13].

**Our hypothesis / discipline**: markup is added per page type once, validated on a few deployed URLs, then rolled out. Invented ratings or fake authorship = never (also covered by [3] and [11]).

## 6. Internal linking and crawlability

**Google says** only `<a href>` links are crawlable — `javascript:` URLs, router-link components, `<span href>`, and onclick-only links are not [7]. Anchor text should be descriptive and concise; keyword-stuffed or empty anchors are bad [7]. Every page that matters should receive at least one internal link from another page on the site [7]. There is no magical ideal link count — don't chase one [6] [7]. External citations to trustworthy sources are fine and help trust [7]. Use `rel="sponsored"`, `rel="nofollow"`, `rel="ugc"` for paid, untrusted, and user-generated links respectively [7]. Descriptive URLs and topic-grouped directories help [6]. Duplicate content is not a spam violation — canonicalize it (redirect or `rel="canonical"`) [6].

**Our rules**:

- Astro site: links are plain `<a href>`. No crawlability games. Client-side-routed content must still render reachable `<a href>` markup.
- Every template page and article gets ≥1 internal link from a related page (article → tool page, template → similar templates).
- Anchor text names the destination ("Floor plan tool" not "click here"), no stuffing.
- Cite sources when stating facts — on the site as in this playbook.

## 7. Keyword and intent buckets

**Our hypothesis** — the buckets below are unvalidated territory: we have no keyword-volume tool access and no GSC data yet, so every term is an **unverified hypothesis** to be validated against real GSC queries once data exists (Section 9). Never quote volumes; there are none to quote.

| Bucket | Example queries | Page type | CTA posture |
|---|---|---|---|
| Launch / primary CTA | design my house | Tool landing | "Start designing — free, in your browser" |
| Product / comparison | floor plan software, 3D house design tool online | Tool pages, comparison pages | Direct tool CTA |
| Education / how-to | how to draw a floor plan | Articles | Tool CTA after real instruction |
| Inspiration / leads | modern minimalist house plans | Template galleries | Template → open in tool |
| Competitor / alternative | free alternative to <X> | Comparison pages | Honest comparison, tool CTA |

**Google says** anticipate the words people use, but don't obsess over variants — language matching handles much of it [6]. **Our hypothesis**: the competitor bucket is capped — honest comparisons only; never fabricate a competitor's flaws. Validation loop: pick queries from GSC data bucket-by-bucket, then re-prioritize. A bucket with no impressions after enough exposure is dropped or reworded.

## 8. Programmatic and template pages

Template-style pages will be generated from user-created house designs, aimed at floor-plan search intent with a tool-first CTA. **Google says** the relevant spam policy is **scaled content abuse**: "many pages generated primarily to manipulate rankings" that don't help users — violating "no matter how it's created", with AI tooling generating many pages without adding value explicitly listed as an example [8]. **Doorway abuse** is also on point: substantially similar pages built closer to search results than to a browseable hierarchy [8]. The documented exclusion mechanism for low-value pages is `noindex` [8].

**Our gates** (our decision — programmatic pages ship only when every gate passes):

1. **Unique value per page**: the page teaches or shows something its sibling pages don't (specific layout, real dimensions, real use-case description) — not a synonym-swapped clone [8].
2. **Human quality review** before indexing: a person approves each template batch.
3. **Indexation caps**: template count grows in bounded waves, reviewed against GSC indexing/crawl data before the next wave.
4. **Noindex until reviewed**: pages ship `noindex` and flip to indexable only after review passes.

There is no third way: each page either adds real value or it does not get built.

## 9. Measurement

Data source: Google Search Console (property `sc-domain:buildmy.house`), ingested daily (3-day lag) into the `marketing` schema of the ops Postgres — table `marketing.gsc_search_analytics` (date/query/page/country/device rows with clicks/impressions/CTR/position) and `marketing.ingest_runs` where `source = 'gsc'` (ingest health). The weekly report command is `python -m company_ops.seo_report`.

**Google says** the four metrics are clicks, impressions, CTR, and average position; data has roughly a ~3-day lag; Search Console impression counts are GOOGLE SEARCH impressions, not actual users (Search Console performance-report definitions; not separately fetched — treat as practitioner knowledge). **Practitioner heuristic / our standard**:

- CTR is only meaningful **relative to position** on queries with adequate impressions. A 2% CTR at position 1 is a problem; 2% at position 12 is fine.
- Minimum thresholds before declaring a result (our decision, scaled for a new low-traffic site): a comparison window needs ≥2 weeks of post-change data and both windows ≥ a few hundred impressions on the affected pages/queries; below that the result is logged as `inconclusive`, not `confirmed`.
- Weeks or days of data on a brand-new site are noise. Slow decisions.
- One experiment = one page (or one page group) changed. Never two overlapping changes in the same window — causality becomes unreadable.
- Weekly cadence (skill): ingest health → report → ≤1–3 experiments chosen → logged in `## Experiment log`.

## 10. Sources

All fetched 2026-10-19. Last-updated dates on Google pages noted where displayed.

1. Title links — Google Search Central. https://developers.google.com/search/docs/appearance/title-link (updated 2025-12-10). No title length cap; device-width truncation; avoid boilerplate; recrawl in days–weeks.
2. Snippets — Google Search Central. https://developers.google.com/search/docs/appearance/snippet (updated 2026-04-20). Snippets auto-generated; meta description sometimes used; programmatic meta-descriptions fine and encouraged.
3. Creating helpful content — Google Search Central. https://developers.google.com/search/docs/fundamentals/creating-helpful-content (updated 2026-10-05). People-first criteria; word count not a factor; who-how-why; fabricated authorship is deception.
4. Core Web Vitals — Google Search Central. https://developers.google.com/search/docs/fundamentals/core-web-vitals (updated 2025-12-10). LCP ≤2.5s, INP <200ms, CLS <0.1; CWV report in Search Console.
5. Article structured data — Google Search Central. https://developers.google.com/search/docs/appearance/structured-data/article (updated 2026-09-08). No required properties; author/image/date guidance; validate on a few pages, then sitemap.
6. SEO starter guide — Google Search Central. https://developers.google.com/search/docs/fundamentals/seo-starter-guide. Descriptive URLs; canonicalize duplicates; don't obsess over keyword stuffing, word counts, or domain keywords.
7. Make links crawlable — Google Search Central. https://developers.google.com/search/docs/crawling-indexing/links-crawlable (updated 2025-12-10). Only `<a href>` is crawlable; anchor guidelines; every important page needs ≥1 internal link; no magic link count.
8. Spam policies — Google Search Central. https://developers.google.com/search/docs/essentials/spam-policies. Scaled content abuse ("no matter how it's created", AI example listed); doorway abuse; noindex as documented exclusion mechanism.
9. Using generative AI content — Google Search Central. https://developers.google.com/search/docs/fundamentals/using-gen-ai-content (updated 2026-10-01). AI fine for research/structure; fact-check before publish; disclose how content was made.
10. SoftwareApplication (WebApplication) structured data — Google Search Central. https://developers.google.com/search/docs/appearance/structured-data/software-app (updated 2026-09-08). Required: name, offers with price, and aggregateRating or review; DesignApplication supported.
11. General structured data guidelines — Google Search Central. https://developers.google.com/search/docs/appearance/structured-data/sd-policies. JSON-LD recommended; markup must be truthful and represent visible content; Rich Results Test.
12. Organization structured data — Google Search Central. https://developers.google.com/search/docs/appearance/structured-data/organization. Home-page placement; logo ≥112×112px, ratio ≤3:1; one Organization per page.
13. Breadcrumb structured data — Google Search Central. https://developers.google.com/search/docs/appearance/structured-data/breadcrumb. Every breadcrumb item a valid URL; order = page hierarchy.

## Experiment log

Entries below follow the exact parseable format. The weekly job appends entries and opens a PR; entries never bypass review.

### 2026-10-19 title-standardization

- Hypothesis: consistent `<Page> · buildmy.house` branded titles improve brand recognition and CTR on our target queries.
- Change: apply the title standard to all markdown page titles site-wide.
- Metric: CTR on affected queries in GSC, compared per position band, once enough impressions accrue (Section 9 thresholds).
- Result: pending — no GSC data yet (new property).
- Learning: none yet.
- Status: open
