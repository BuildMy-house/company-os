"""Weekly SEO learning loop (service: seo_report).

Reads Google Search Console rows ingested into `marketing.gsc_search_analytics`
by `company_ops.seo_gsc_ingest` (READ-only here), compares the last 7 days
against the prior 7 days, and emits:

- a dated markdown report under ``--out``
- a learnings entry appended to the marketing seo-playbook Experiment log
- optional follow-up tickets (``--file-tickets``) filed through hive_client;
  tickets satisfy docs/TICKET-STANDARD.md (validated before posting)
- optional learnings PR (``--open-pr``) — branches/commits/pushes and opens a
  PR via gh; never merges, only runs when explicitly passed

Tables are populated by the `seo_gsc_ingest` service; when empty this module
reports "no data yet" and exits 0. This side never writes SEO tables.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from company_ops.hive_client import file_feedback_work
from company_ops.spend_review import _ensure_env, _get_connection, resolve_dsn

DEFAULT_PLAYBOOK = Path("hermes-operations/domains/marketing/knowledge/seo-playbook.md")
EXPERIMENT_LOG_HEADING = "## Experiment log"
DB_NOT_CONFIGURED_EXIT = 3
FILE_TICKETS_EXIT = 4
SITE_URL_ENV = "SEO_GSC_SITE_URL"
HIVE_URL_ENV = "COMPANY_HIVE_URL"
DEFAULT_HIVE_URL = "http://hive:4100"

NOISE_IMPRESSIONS = 30
CTR_GAP_MIN_IMPRESSIONS = 50
UNTARGETED_MIN_IMPRESSIONS = 25
UNTARGETED_MAX_POSITION = 20.0
GENERIC_PAGE_PATHS = frozenset({"/", "/index.html", "/blog", "/blog/", "/blog/index.html"})

EXPECTED_CTR_CURVE: tuple[tuple[float | None, float], ...] = (
    (1, 0.28),
    (2, 0.15),
    (3, 0.10),
    (5, 0.06),
    (10, 0.03),
    (None, 0.01),
)

PAGE_QUERY_GRAIN_SQL = """
SELECT query, page, sum(clicks) AS clicks, sum(impressions) AS impressions
FROM marketing.gsc_search_analytics
WHERE date >= %s AND date <= %s
  AND (%s::text IS NULL OR site_url = %s)
GROUP BY query, page
"""

INGEST_HEALTH_SQL = """
SELECT status, error, rows_upserted, finished_at
FROM marketing.ingest_runs
WHERE source = 'gsc'
ORDER BY started_at DESC
LIMIT 1
"""

# --- ticket standard (mirror of scripts/hive-member-mcp.js applyTicketStandard) ---

TICKET_TYPES = ("feature", "bug", "chore")
FIBONACCI_POINTS = (1, 2, 3, 5, 8, 13)
BUG_BUMPS = {"needs_diagnosis": 2, "touches_production": 1}
MAX_POINTS = 13
KNOWN_TOOLS = (
    "browser",
    "hindsight",
    "kubectl-deploy",
    "image-build",
    "hermes-data",
    "postgres-admin",
    "github-pr",
    "reddit-publish",
    "substack-publish",
    "marketing-db-read",
    "marketing-ingest",
)

ANTI_FABRICATION = (
    "Anti-fabrication: every number, path, and command in this ticket comes from the "
    "weekly analysis report; do not invent extra metrics, files, or evidence, and do "
    "not ask clarifying questions back - decide, act, and document."
)


def validate_ticket_standard(args: dict[str, Any]) -> dict[str, Any]:
    """Mirror scripts/hive-member-mcp.js::applyTicketStandard.

    Raises ValueError with the same rejection wording shapes; returns
    {"message": composed message, "metadata": finalized metadata}.
    """
    metadata = dict(args.get("metadata") or {})
    problems: list[str] = []
    if metadata.get("type") not in TICKET_TYPES:
        problems.append(f"metadata.type must be one of {'|'.join(TICKET_TYPES)}")
    if metadata.get("points") not in FIBONACCI_POINTS:
        problems.append(f"metadata.points must be one of {', '.join(map(str, FIBONACCI_POINTS))} (split anything bigger than {MAX_POINTS})")

    def text(value: Any) -> bool:
        return isinstance(value, str) and value.strip() != ""

    if not text(metadata.get("goal")):
        problems.append("metadata.goal must state the outcome in one or two sentences")
    criteria = [c for c in metadata.get("acceptance_criteria") or [] if isinstance(c, str) and c.strip()]
    if not criteria:
        problems.append("metadata.acceptance_criteria must list at least one verifiable check")
    if not text(metadata.get("scope")):
        problems.append("metadata.scope must name the repo/paths in scope and what is out of scope")
    bug_modifiers = metadata.get("bug_modifiers")
    if bug_modifiers is not None and (not isinstance(bug_modifiers, list) or any(m not in BUG_BUMPS for m in bug_modifiers)):
        problems.append(f"metadata.bug_modifiers may only contain {', '.join(BUG_BUMPS)}")
    requires_tools = metadata.get("requires_tools")
    if requires_tools is not None:
        unknown = None
        if isinstance(requires_tools, list):
            unknown = [t for t in requires_tools if t not in KNOWN_TOOLS]
        if unknown is None:
            problems.append("metadata.requires_tools must be an array of tool names")
        elif unknown:
            problems.append(f"metadata.requires_tools has unknown tool(s) {', '.join(map(repr, unknown))}; known tools: {', '.join(KNOWN_TOOLS)}")
    if problems:
        raise ValueError(f"hive_submit rejected, ticket does not meet docs/TICKET-STANDARD.md: {'; '.join(problems)}")
    if not text(args.get("message")):
        raise ValueError("hive_submit requires a non-empty message")

    bumps = 0
    if metadata.get("type") == "bug":
        bumps = sum(BUG_BUMPS[m] for m in dict.fromkeys(bug_modifiers or []))
    points = int(metadata["points"]) + bumps
    if points > MAX_POINTS:
        raise ValueError(f"hive_submit rejected: {metadata['points']} points + {bumps} bug bump = {points}, above {MAX_POINTS}; split the ticket into smaller tickets")

    message = "\n\n".join(
        [
            args["message"].strip(),
            f"Type: {metadata['type']} | Points: {points}" + (f" ({metadata['points']} + {bumps} bug bump)" if bumps else ""),
            f"Goal: {metadata['goal'].strip()}",
            "Acceptance criteria:\n" + "\n".join(f"- {c.strip()}" for c in criteria),
            f"Scope: {metadata['scope'].strip()}",
        ]
    )
    tier = metadata["tier"] if metadata.get("tier") in ("cheap", "standard", "power") else ("cheap" if points <= 3 else "standard" if points <= 5 else "power")
    tools = {"requires_tools": sorted(set(requires_tools))} if requires_tools else {}
    return {
        "message": message,
        "metadata": {**{k: v for k, v in metadata.items() if k not in ("bug_modifiers", "requires_tools")}, **tools, "points": points, "base_points": metadata["points"], "tier": tier},
    }


def render_ticket_checklist(sections: dict[str, str]) -> str:
    """Agent-manager ticket-writing checklist, embedded in the message text."""
    lines = []
    for header in (
        "Owner files",
        "Dependencies",
        "Root cause / context",
        "Pattern to mirror",
        "Runnable DoD",
        "Authority",
        "Scope",
        "No legacy cruft",
    ):
        lines.append(f"## {header}")
        body = sections.get(header, "n/a")
        lines.append(body if body.endswith("\n") else body + "\n")
        if header == "Authority":
            lines.append(ANTI_FABRICATION + "\n")
    return "\n".join(lines).rstrip() + "\n"


# --- position/CTR heuristics -------------------------------------------------


def expected_ctr(position: float) -> float:
    """Documented heuristic curve: pos1 ~28%, pos2 ~15%, pos3 ~10%, 4-5 ~6%, 6-10 ~3%, beyond ~1%.

    GSC positions are averages, so bucket by the rounded rank (1.4 behaves like 1)."""
    rank = max(1, round(position))
    for max_pos, ctr in EXPECTED_CTR_CURVE:
        if max_pos is None or rank <= max_pos:
            return ctr
    return EXPECTED_CTR_CURVE[-1][1]


def url_path(page: str | None) -> str:
    if not page:
        return "/"
    return urlsplit(str(page)).path or "/"


def _empty_metric() -> dict[str, Any]:
    return {"clicks": 0, "impressions": 0, "position": None}


def aggregate_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Weighted aggregate of grain rows (one per query+page) by `key`."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        clicks = int(row.get("clicks") or 0)
        impr = int(row.get("impressions") or 0)
        position = row.get("position")
        agg = out.setdefault(str(row[key]), {"clicks": 0, "impressions": 0, "_weighted": 0.0})
        agg["clicks"] += clicks
        agg["impressions"] += impr
        if position is not None:
            agg["_weighted"] += float(position) * impr
    for agg in out.values():
        weighted = agg.pop("_weighted")
        agg["position"] = weighted / agg["impressions"] if agg["impressions"] else None
    return out


def winners_losers(
    metrics_current: dict[str, dict[str, Any]],
    metrics_prior: dict[str, dict[str, Any]],
    min_impressions: int = NOISE_IMPRESSIONS,
) -> dict[str, list[dict[str, Any]]]:
    """Winners/losers by clicks and impressions deltas; below-threshold keys are noise."""
    winners: list[dict[str, Any]] = []
    losers: list[dict[str, Any]] = []
    for name in sorted(set(metrics_current) | set(metrics_prior)):
        cur = metrics_current.get(name) or _empty_metric()
        pri = metrics_prior.get(name) or _empty_metric()
        if cur["impressions"] < min_impressions and pri["impressions"] < min_impressions:
            continue
        clicks_delta = cur["clicks"] - pri["clicks"]
        impr_delta = cur["impressions"] - pri["impressions"]
        if clicks_delta == 0 and impr_delta == 0:
            continue
        finding = {
            "key": name,
            "clicks": cur["clicks"],
            "impressions": cur["impressions"],
            "prior_clicks": pri["clicks"],
            "prior_impressions": pri["impressions"],
            "clicks_delta": clicks_delta,
            "impressions_delta": impr_delta,
        }
        if impr_delta > 0 or clicks_delta > 0:
            winners.append(finding)
        if impr_delta < 0 or clicks_delta < 0:
            losers.append(finding)
    winners.sort(key=lambda f: (-f["impressions_delta"], -f["clicks_delta"]))
    losers.sort(key=lambda f: (f["impressions_delta"], f["clicks_delta"]))
    return {"winners": winners, "losers": losers}


def ctr_gaps(
    metrics_current: dict[str, dict[str, Any]],
    min_impressions: int = CTR_GAP_MIN_IMPRESSIONS,
) -> list[dict[str, Any]]:
    """Pages/queries whose actual CTR sits under the heuristic curve for their position."""
    gaps = []
    for name, m in metrics_current.items():
        if m["impressions"] < min_impressions or m["position"] is None:
            continue
        actual = m["clicks"] / m["impressions"]
        expected = expected_ctr(m["position"])
        if actual < expected:
            gaps.append(
                {
                    "key": name,
                    "clicks": m["clicks"],
                    "impressions": m["impressions"],
                    "position": m["position"],
                    "ctr": actual,
                    "expected_ctr": expected,
                }
            )
    gaps.sort(key=lambda g: g["expected_ctr"] - g["ctr"], reverse=True)
    return gaps


def untargeted_queries(
    rows: list[dict[str, Any]],
    min_impressions: int = UNTARGETED_MIN_IMPRESSIONS,
    max_position: float = UNTARGETED_MAX_POSITION,
    generic_paths: frozenset[str] = GENERIC_PAGE_PATHS,
) -> list[dict[str, Any]]:
    """Queries with demand but no targeted page: avg position > max_position, or
    all landings land on generic pages (home / blog index)."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        impr = int(row.get("impressions") or 0)
        if impr <= 0:
            continue
        g = groups.setdefault(row["query"], {"clicks": 0, "impressions": 0, "pages": set(), "_weighted": 0.0})
        g["clicks"] += int(row.get("clicks") or 0)
        g["impressions"] += impr
        g["pages"].add(url_path(row.get("page")))
        g["_weighted"] += float(row["position"] or 0) * impr
    out = []
    for query, g in sorted(groups.items()):
        if g["impressions"] < min_impressions:
            continue
        avg_position = g.pop("_weighted") / g["impressions"]
        all_generic = g["pages"] <= generic_paths
        if (avg_position > max_position or avg_position == 0) or all_generic:
            out.append(
                {
                    "query": query,
                    "impressions": g["impressions"],
                    "clicks": g["clicks"],
                    "avg_position": avg_position,
                    "landing_paths": sorted(g["pages"]),
                }
            )
    out.sort(key=lambda f: -f["impressions"])
    return out


# --- git history of the website checkout -------------------------------------


def parse_git_log(text: str) -> list[dict[str, Any]]:
    """Parse `git log --numstat --date=iso --pretty=format:COMMIT:%H|%ad|%s` output."""
    records: list[dict[str, Any]] = []
    record: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("COMMIT:"):
            if record is not None:
                records.append(record)
            commit, committed_at, subject = line[len("COMMIT:") :].split("|", 2)
            record = {"commit": commit, "committed_at": committed_at, "subject": subject, "files": []}
        elif record is not None and "\t" in line:
            record["files"].append(line.split("\t", 2)[-1].strip())
    if record is not None:
        records.append(record)
    return records


def git_content_changes(repo_path: Path | None, since: date) -> list[dict[str, Any]]:
    """Content commits under src/pages, src/layouts, src/content since `since`.

    Fixed argv, never shell=True. Empty list means "no history available" and
    the report annotates that, without claiming causation for anything.
    """
    if repo_path is None:
        return []
    args = [
        "git",
        "-C",
        str(repo_path),
        "log",
        "--numstat",
        "--date=iso",
        "--pretty=format:COMMIT:%H|%ad|%s",
        "--",
        "src/pages",
        "src/layouts",
        "src/content",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    # Filter by committer date here instead of `git log --since`: git stops
    # traversal at the first commit older than the given date, which hides
    # non-monotonic histories (e.g. rebased/backdated commits).
    changes = []
    for record in parse_git_log(proc.stdout):
        try:
            record["committed_date"] = datetime.fromisoformat(record["committed_at"]).date().isoformat()
        except ValueError:
            record["committed_date"] = record["committed_at"]
        if record["committed_date"] >= since.isoformat():
            changes.append(record)
    return changes


def slug_for_page(page: str) -> str:
    tail = [seg for seg in page.split("/") if seg]
    return tail[-1] if tail else ""


def annotate_with_content_changes(findings: list[dict[str, Any]], changes: list[dict[str, Any]]) -> None:
    """Annotate findings whose page slug appears in a changed file's path.

    Records a correlation and only that — no causation is claimed.
    """
    for finding in findings:
        slug = slug_for_page(finding["key"])
        finding["related_content_changes"] = sorted(
            {f"{c['commit'][:12]} {c['committed_date']} {c['subject']}" for c in changes if slug and slug in " ".join(c["files"])}
        )


# --- playbook learnings -------------------------------------------------------


def format_learnings_entry(entry: dict[str, str]) -> str:
    lines = [
        f"### {entry['date']} {entry['title']}",
        f"- Hypothesis: {entry.get('hypothesis', '')}",
        f"- Change: {entry.get('change', '')}",
        f"- Metric: {entry.get('metric', '')}",
        f"- Result: {entry.get('result', '')}",
        f"- Learning: {entry.get('learning', '')}",
        f"- Status: {entry.get('status', 'open')}",
    ]
    return "\n".join(lines) + "\n"


def append_learning(playbook_path: Path, entry: dict[str, str]) -> bool:
    """Append an entry under `## Experiment log`, creating file/heading when missing.

    Returns True when appended, False when the same dated entry already exists
    (idempotent re-runs do not duplicate learnings).
    """
    text = playbook_path.read_text() if playbook_path.exists() else ""
    marker = f"### {entry['date']} {entry['title']}"
    if EXPERIMENT_LOG_HEADING in text and marker in text:
        return False
    block = format_learnings_entry(entry)
    if EXPERIMENT_LOG_HEADING not in text:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n{EXPERIMENT_LOG_HEADING}\n\n{block}"
    else:
        idx = text.index(EXPERIMENT_LOG_HEADING) + len(EXPERIMENT_LOG_HEADING)
        text = f"{text[:idx]}\n\n{block}\n{text[idx:].lstrip(chr(10))}"
    playbook_path.parent.mkdir(parents=True, exist_ok=True)
    playbook_path.write_text(text)
    return True


def learnings_entry_from_report(report: dict[str, Any]) -> dict[str, str]:
    week = report["week_ending"]
    entry: dict[str, str] = {"date": week, "status": "open"}
    if not report["has_data"]:
        entry.update(
            title=f"No GSC data yet for week ending {week}",
            hypothesis="Once seo_gsc_ingest populates search analytics, the weekly loop can surface winners/losers and CTR gaps.",
            change="Ran the weekly SEO learning loop; GSC tables were empty for the window.",
            metric="clicks / impressions deltas vs prior 7 days",
            result="No data yet (0 impressions in both windows).",
            learning="Loop is wired and safe on empty data; waiting on GSC ingestion.",
        )
        return entry
    parts = []
    if report["pages"]["winners"]:
        top = report["pages"]["winners"][0]
        parts.append(f"top winner: {top['key']} (impressions {top['impressions_delta']:+d})")
    if report["ctr_gaps_pages"]:
        worst = report["ctr_gaps_pages"][0]
        parts.append(
            f"worst CTR gap: {worst['key']} ({worst['ctr'] * 100:.1f}% actual vs ~{worst['expected_ctr'] * 100:.0f}% expected at position {worst['position']:.1f})"
        )
    entry.update(
        title=f"Week ending {week}: {'; '.join(parts) if parts else 'stable week'}",
        hypothesis="Week-over-week deltas and CTR-vs-position gaps identify the next content actions.",
        change="Ran the weekly SEO learning loop over GSC search analytics.",
        metric=f"week ending {week} vs prior 7 days (clicks/impressions/CTR)",
        result="; ".join(parts) if parts else "No significant winners or CTR gaps above thresholds.",
        learning="See the dated report in the out dir and follow-up tickets (if filed).",
    )
    return entry


# --- follow-up tickets --------------------------------------------------------


def _make_ticket(title: str, ticket_type: str, points: int, sections: dict[str, str], acceptance_criteria: list[str], scope: str) -> dict[str, Any]:
    """Build a docs/TICKET-STANDARD.md-compliant ticket (validated, never invented)."""
    args = {
        "message": render_ticket_checklist(sections),
        "metadata": {
            "type": ticket_type,
            "points": points,
            "goal": title,
            "acceptance_criteria": acceptance_criteria,
            "scope": scope,
        },
    }
    validated = validate_ticket_standard(args)
    return {"title": title, **validated}


def title_rewrite_ticket(gap: dict[str, Any]) -> dict[str, Any]:
    page = gap["key"]
    title = f"Rewrite title for page {page}: CTR {gap['ctr'] * 100:.1f}% at position {gap['position']:.0f}"
    return _make_ticket(
        title,
        ticket_type="chore",
        points=2,
        sections={
            "Owner files": f"- `src/pages{page}` title/head block (website checkout)",
            "Dependencies": "- None; GSC data is already ingested in marketing.gsc_search_analytics.",
            "Root cause / context": (
                f"Weekly analysis (company_ops.seo_report, week ending report) found CTR {gap['ctr'] * 100:.1f}% at average position "
                f"{gap['position']:.1f} over {gap['impressions']} impressions; the documented heuristic curve expects "
                f"~{gap['expected_ctr'] * 100:.0f}% at that position. The title/snippet is underselling the ranking."
            ),
            "Pattern to mirror": "- An existing page under `src/pages` with a clear `<title>` and meta description.",
            "Runnable DoD": (
                "- Build the website checkout with its normal build command; the page's rendered HTML contains the new title.\n"
                "- Re-run `python -m company_ops.seo_report --dry-run` after a week and confirm the CTR gap narrowed or note it in the Experiment log."
            ),
            "Authority": "You own this page's title and meta description only. No questions; decide, act, and document in the PR.",
            "Scope": "Only the title/meta description of the named page. No new pages, no redesign, no copy rewrites.",
            "No legacy cruft": "One current title; no old-title variants, commented-out stage drafts, or staging files left behind.",
        },
        acceptance_criteria=[f"Rendered title/meta for {page} changes; no other page content touched."],
        scope=f"In scope: website repo src/pages{page} title/head only. Out of scope: other pages, site-wide design, sitemap changes.",
    )


def new_page_ticket(candidate: dict[str, Any]) -> dict[str, Any]:
    query = candidate["query"]
    title = f"Create page for query '{query}'"
    avg = candidate["avg_position"]
    avg_text = "unknown" if avg in (None, 0) else f"{avg:.1f}"
    return _make_ticket(
        title,
        ticket_type="feature",
        points=3,
        sections={
            "Owner files": (
                "- `src/pages/<chosen-path>.astro` (new file in the website checkout)\n"
                "- exactly one existing page under `src/pages` or `src/layouts` that links it"
            ),
            "Dependencies": "- None blocking; the author picks the keyword path.",
            "Root cause / context": (
                f"Weekly analysis found {candidate['impressions']} impressions for query '{query}' with avg position {avg_text}, "
                f"landing only on generic pages ({', '.join(candidate['landing_paths']) or 'none recorded'}). "
                "Demand exists; no targeted page does."
            ),
            "Pattern to mirror": "- An existing content page under `src/pages` (frontmatter, layout import, content body).",
            "Runnable DoD": (
                f"- Build the website checkout with its normal build command; the new URL renders and targets '{query}' in its title/h1.\n"
                "- Run the repo's link check or manually confirm the linking page points at the new URL."
            ),
            "Authority": "You own creating this page and choosing its path. No questions; decide, act, and document in the PR.",
            "Scope": "One new page plus one link from an existing page. No site-wide changes.",
            "No legacy cruft": "No placeholder or staging pages left behind after merge.",
        },
        acceptance_criteria=[f"Built site renders a page targeting '{query}', linked from at least one existing page."],
        scope=f"In scope: one new page under src/pages for '{query}' plus one inbound link. Out of scope: other pages, layout rewrites, sitemap tooling.",
    )


def followup_tickets(ctr_gap_findings: list[dict[str, Any]], untargeted: list[dict[str, Any]], max_each: int = 5) -> list[dict[str, Any]]:
    tickets = [title_rewrite_ticket(g) for g in ctr_gap_findings[:max_each]]
    tickets += [new_page_ticket(c) for c in untargeted[:max_each]]
    return tickets


def file_tickets(tickets: list[dict[str, Any]], hive_url: str | None = None) -> list[Any]:
    url = hive_url or os.environ.get(HIVE_URL_ENV) or DEFAULT_HIVE_URL
    filed = []
    for ticket in tickets:
        filed.append(file_feedback_work(url, ticket["title"], ticket["message"], ticket["metadata"]))
    return filed


# --- open PR with the learnings diff ------------------------------------------

PR_BRANCH = "seo/learnings-{week}"


def pr_commands(repo_path: Path, playbook_path: Path, week_ending: str) -> list[list[str]]:
    branch = PR_BRANCH.format(week=week_ending)
    return [
        ["git", "-C", str(repo_path), "checkout", "-b", branch],
        ["git", "-C", str(repo_path), "add", str(playbook_path)],
        ["git", "-C", str(repo_path), "commit", "-m", f"docs(seo): append weekly learnings {week_ending}"],
        ["git", "-C", str(repo_path), "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--fill", "--title", f"SEO learnings week ending {week_ending}", "--body", "Learnings entry appended by company_ops.seo_report; human review required before merge."],
    ]


def open_learning_pr(repo_path: Path, playbook_path: Path, week_ending: str) -> None:
    for args in pr_commands(repo_path, playbook_path, week_ending):
        subprocess.run(args, check=True, capture_output=True, text=True)


# --- report compile/format pair ----------------------------------------------


def week_windows(week_ending: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """((current 7d), (prior 7d)) as inclusive start / exclusive end."""
    current_end = week_ending + timedelta(days=1)
    return (week_ending - timedelta(days=6), current_end), (week_ending - timedelta(days=13), week_ending - timedelta(days=6))


def totals_for(metrics: dict[str, dict[str, Any]], prior: dict[str, dict[str, Any]]) -> dict[str, int]:
    return {
        "clicks": sum(m["clicks"] for m in metrics.values()),
        "prior_clicks": sum(m["clicks"] for m in prior.values()),
        "impressions": sum(m["impressions"] for m in metrics.values()),
        "prior_impressions": sum(m["impressions"] for m in prior.values()),
    }


def build_analysis(rows_current: list[dict[str, Any]], rows_prior: list[dict[str, Any]], content_changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pages_cur = aggregate_by(rows_current, "page")
    pages_pri = aggregate_by(rows_prior, "page")
    queries_cur = aggregate_by(rows_current, "query")
    queries_pri = aggregate_by(rows_prior, "query")
    changes = content_changes if content_changes is not None else []
    gaps_pages = ctr_gaps(pages_cur)
    gaps_queries = ctr_gaps(queries_cur)
    pages = winners_losers(pages_cur, pages_pri)
    queries = winners_losers(queries_cur, queries_pri)
    for findings in [pages["winners"], pages["losers"], queries["winners"], queries["losers"], gaps_pages, gaps_queries]:
        annotate_with_content_changes(findings, changes)
    return {
        "has_data": bool(pages_cur or queries_cur or pages_pri or queries_pri),
        "totals": totals_for(pages_cur, pages_pri),
        "pages": pages,
        "queries": queries,
        "ctr_gaps_pages": gaps_pages,
        "ctr_gaps_queries": gaps_queries,
        "untargeted_queries": untargeted_queries(rows_current),
        "content_changes": changes,
    }


DSN_ENVS = ("TEST_ANALYTICS_DATABASE_URL", "ANALYTICS_DATABASE_URL", "MARKETING_INGEST_DATABASE_URL")


def _report_dsn() -> str | None:
    """Read-only DSN: prefer the hermes_analytics reader, fall back to the ingest role."""
    for name in DSN_ENVS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def fetch_grain_rows(week_ending: date, site_url: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    (cur_start, cur_end), (pri_start, pri_end) = week_windows(week_ending)
    ingest_health: dict[str, Any] = {"available": False}
    rows_current: list[dict[str, Any]] = []
    rows_prior: list[dict[str, Any]] = []
    try:
        with _get_connection(resolve_dsn(_report_dsn() or '')) as conn, conn.cursor() as cur:
            cur.execute(PAGE_QUERY_GRAIN_SQL, (cur_start, cur_end, site_url, site_url))
            rows_current = cur.fetchall()
            cur.execute(PAGE_QUERY_GRAIN_SQL, (pri_start, pri_end, site_url, site_url))
            rows_prior = cur.fetchall()
            try:
                cur.execute(INGEST_HEALTH_SQL)
                ingest_health = {**(cur.fetchone() or {}), "available": True}
            except psycopg.errors.UndefinedTable:
                pass
    except psycopg.errors.UndefinedTable:
        return rows_current, rows_prior, ingest_health
    return rows_current, rows_prior, ingest_health


def compile_seo_report(
    week_ending: date,
    site_url: str | None,
    repo_path: Path | None,
    rows_current: list[dict[str, Any]] | None = None,
    rows_prior: list[dict[str, Any]] | None = None,
    ingest_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile the report payload (pure given rows; fetches from DB otherwise)."""
    if rows_current is None:
        rows_current, rows_prior, ingest_health = fetch_grain_rows(week_ending, site_url)
    else:
        rows_prior = rows_prior or []
    analysis = build_analysis(rows_current, rows_prior, git_content_changes(repo_path, week_ending - timedelta(days=13)))
    return {
        "week_ending": week_ending.isoformat(),
        "site_url": site_url or "(all sites)",
        "has_data": analysis["has_data"],
        "totals": analysis["totals"],
        "pages": analysis["pages"],
        "queries": analysis["queries"],
        "ctr_gaps_pages": analysis["ctr_gaps_pages"],
        "ctr_gaps_queries": analysis["ctr_gaps_queries"],
        "untargeted_queries": analysis["untargeted_queries"],
        "content_changes": analysis["content_changes"],
        "ingest_health": ingest_health or {"available": False},
    }


def format_report_markdown(report: dict[str, Any], entries_count: int | None = None) -> str:
    week = report["week_ending"]
    totals = report["totals"]
    lines = [
        f"# SEO weekly report — week ending {week}",
        "",
        f"Site: {report['site_url']}. Comparison: last 7 days vs prior 7 days.",
        "",
        "| Metric | Last 7d | Prior 7d | Delta |",
        "|---|---|---|---|",
        f"| Clicks | {totals['clicks']} | {totals['prior_clicks']} | {totals['clicks'] - totals['prior_clicks']:+d} |",
        f"| Impressions | {totals['impressions']} | {totals['prior_impressions']} | {totals['impressions'] - totals['prior_impressions']:+d} |",
        "",
    ]
    ingest = report["ingest_health"]
    if ingest.get("available"):
        lines.append(f"Latest ingest run: status {ingest.get('status')!r}, rows {ingest.get('rows_upserted')}, finished {ingest.get('finished_at')}.")
        lines.append("")
    if not report["has_data"]:
        lines += [
            "**No data yet.** No GSC impressions in the window. Once `company_ops.seo_gsc_ingest` populates the tables, re-run this report.",
        ]
        return "\n".join(lines) + "\n"

    def findings_table(section_title: str, rows: list[dict[str, Any]]) -> list[str]:
        out = [f"## {section_title}", ""]
        if not rows:
            out.append("(none above the noise threshold)")
            out.append("")
            return out
        out += ["| Key | Clicks | ΔClicks | Impr | ΔImpr |", "|---|---|---|---|---|"]
        for f in rows[:10]:
            out.append(f"| {f['key']} | {f['clicks']} | {f['clicks_delta']:+d} | {f['impressions']} | {f['impressions_delta']:+d} |")
        out.append("")
        for f in rows[:10]:
            if f.get("related_content_changes"):
                out.append(f"- Correlated content change for {f['key']} (annotation only, not a causation claim): {'; '.join(f['related_content_changes'])}")
        out.append("")
        return out

    lines += findings_table("Page winners", report["pages"]["winners"])
    lines += findings_table("Page losers", report["pages"]["losers"])
    lines += findings_table("Query winners", report["queries"]["winners"])
    lines += findings_table("Query losers", report["queries"]["losers"])

    lines += ["## CTR below heuristic curve (pages)", ""]
    if report["ctr_gaps_pages"]:
        lines += ["| Page | Impr | Pos | CTR | Expected |", "|---|---|---|---|---|"]
        for g in report["ctr_gaps_pages"][:10]:
            lines.append(f"| {g['key']} | {g['impressions']} | {g['position']:.1f} | {g['ctr'] * 100:.1f}% | ~{g['expected_ctr'] * 100:.0f}% |")
    else:
        lines.append("(none)")
    lines.append("")

    lines += ["## Untargeted queries (new-page candidates)", ""]
    if report["untargeted_queries"]:
        lines += ["| Query | Impr | Avg pos | Landings |", "|---|---|---|---|"]
        for c in report["untargeted_queries"][:10]:
            avg = c["avg_position"]
            avg_text = "" if avg in (None, 0) else f"{avg:.1f}"
            lines.append(f"| {c['query']} | {c['impressions']} | {avg_text} | {', '.join(c['landing_paths']) or '(none)'} |")
    else:
        lines.append("(none)")
    lines.append("")

    lines += ["## Content changes (git history, annotation only)", ""]
    if report["content_changes"]:
        for change in report["content_changes"][:20]:
            files = ", ".join(change["files"][:5]) + ("…" if len(change["files"]) > 5 else "")
            lines.append(f"- {change['committed_date']} {change['subject']} — {files}")
    else:
        lines.append("(no repo-path given, or no matching commits since the window — annotation only, no causation claimed)")
    lines += [
        "",
        "## Heuristic curve",
        "",
        "pos 1 ~28%, 2 ~15%, 3 ~10%, 4–5 ~6%, 6–10 ~3%, beyond ~1%. Actual CTR materially below the curve at a page's position → title/snippet follow-up.",
        "",
    ]
    if entries_count is not None:
        lines += [f"Learnings entry appended ({entries_count}). Follow-ups filed via hive, if any.", ""]
    return "\n".join(lines) + "\n"


# --- CLI -----------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="company_ops.seo_report",
        description="Weekly SEO learning loop over ingested GSC search analytics (READ-only).",
        epilog=(
            "Exit codes: 0 success (including 'no data yet'); "
            f"{DB_NOT_CONFIGURED_EXIT} DB not configured; "
            f"{FILE_TICKETS_EXIT} one or more tickets failed to file. "
            "--dry-run prints the report (and tickets with --file-tickets) to stdout and never writes/appends/files/PRs."
        ),
    )
    parser.add_argument("--week-ending", type=str, default=None, help="YYYY-MM-DD week ending date (default: last complete 7 days ending today-3d)")
    parser.add_argument("--repo-path", type=Path, default=None, help="Website git checkout used to correlate content history")
    parser.add_argument("--out", type=Path, default=Path("seo-reports"), help="Directory for the dated markdown report")
    parser.add_argument("--playbook", type=Path, default=DEFAULT_PLAYBOOK, help="Marketing seo-playbook path holding the Experiment log")
    parser.add_argument("--dry-run", action="store_true", help="Print report and tickets; write/append/file/PR nothing")
    parser.add_argument("--file-tickets", action="store_true", help="File follow-up tickets via hive_client (off by default)")
    parser.add_argument("--open-pr", action="store_true", help="Branch/commit/push the playbook change and open a PR via gh (never merges)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _ensure_env()
    args = parse_args(argv)
    week_ending = date.fromisoformat(args.week_ending) if args.week_ending else date.today() - timedelta(days=3)
    site_url = os.environ.get(SITE_URL_ENV, "").strip() or None
    dsn = _report_dsn()
    if args.dry_run:
        if not dsn:
            print(f"DB not configured: set ANALYTICS_DATABASE_URL (or MARKETING_INGEST_DATABASE_URL) to run the SEO learning loop (exit {DB_NOT_CONFIGURED_EXIT}).")
            return DB_NOT_CONFIGURED_EXIT
        report = compile_seo_report(week_ending, site_url, args.repo_path)
        print(format_report_markdown(report))
        if args.file_tickets:
            for ticket in followup_tickets(report["ctr_gaps_pages"], report["untargeted_queries"]):
                print(f"TICKET: {ticket['title']}")
                print(ticket["message"])
        if args.open_pr:
            print(f"PR commands that would run with --open-pr (branch seo/learnings-{week_ending}):")
            for cmd in pr_commands(args.repo_path or Path("."), args.playbook, week_ending.isoformat()):
                print("  ", " ".join(cmd))
        return 0
    if not dsn:
        print(f"DB not configured: set ANALYTICS_DATABASE_URL (or MARKETING_INGEST_DATABASE_URL) to run the SEO learning loop (exit {DB_NOT_CONFIGURED_EXIT}).")
        return DB_NOT_CONFIGURED_EXIT
    report = compile_seo_report(week_ending, site_url, args.repo_path)
    if not report["has_data"]:
        print("No data yet in marketing.gsc_search_analytics for the window; nothing to learn this week.")
    args.out.mkdir(parents=True, exist_ok=True)
    appended = append_learning(args.playbook, learnings_entry_from_report(report))
    report_path = args.out / f"seo-report-{week_ending.isoformat()}.md"
    report_path.write_text(format_report_markdown(report, entries_count=1 if appended else 0))
    print(f"Wrote {report_path}")
    print(f"Appended learnings entry to {args.playbook}" if appended else f"Learnings entry already present in {args.playbook} (idempotent).")
    if args.file_tickets:
        try:
            for result in file_tickets(followup_tickets(report["ctr_gaps_pages"], report["untargeted_queries"])):
                print(f"Filed ticket: {result}")
        except Exception as exc:  # noqa: BLE001 - a hive failure must not lose the written report
            print(f"Ticket filing failed: {exc}")
            return FILE_TICKETS_EXIT
    if args.open_pr:
        open_learning_pr(args.repo_path or Path("."), args.playbook, week_ending.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
