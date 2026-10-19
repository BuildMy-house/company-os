"""Unit tests for the weekly SEO learning loop (no network, no DB, no real push)."""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from company_ops import seo_report
from company_ops.seo_report import (
    aggregate_by,
    append_learning,
    build_analysis,
    expected_ctr,
    file_tickets,
    followup_tickets,
    format_learnings_entry,
    format_report_markdown,
    git_content_changes,
    learnings_entry_from_report,
    parse_git_log,
    pr_commands,
    title_rewrite_ticket,
    new_page_ticket,
    untargeted_queries,
    validate_ticket_standard,
    EXPERIMENT_LOG_HEADING,
)


def grain(query, page, clicks, impressions, position=None):
    return {"query": query, "page": page, "clicks": clicks, "impressions": impressions, "position": position}


# --- winners/losers math -------------------------------------------------------


def test_winners_losers_math():
    current = aggregate_by([grain("smithfield waterproofing query", "https://site.com/services/waterproofing", 20, 100, 3.0)], "page")
    prior = aggregate_by([grain("smithfield waterproofing query", "https://site.com/services/waterproofing", 5, 60, 4.0)], "page")
    result = aggregate_by([grain("smithfield waterproofing query", "https://site.com/services/waterproofing", 1, 50, 6.0)], "page")
    # sanity of aggregate: weighted position = (20*100)/(100) etc handled elsewhere; check sums
    analysis = build_analysis(
        [grain("q1", "https://site.com/a", 20, 100, 3.0), grain("q2", "https://site.com/b", 2, 10, 30.0)],
        [grain("q1", "https://site.com/a", 5, 60, 4.0), grain("q2", "https://site.com/b", 9, 200, 12.0)],
    )
    pages = analysis["pages"]
    a_win = [f for f in pages["winners"] if f["key"] == "https://site.com/a"]
    b_lose = [f for f in pages["losers"] if f["key"] == "https://site.com/b"]
    assert a_win and a_win[0]["clicks_delta"] == 15 and a_win[0]["impressions_delta"] == 40
    assert b_lose and b_lose[0]["clicks_delta"] == -7 and b_lose[0]["impressions_delta"] == -190
    noise = [f for f in pages["winners"] + pages["losers"] if f["key"] == "https://site.com/noise"]
    below = build_analysis(
        [grain("q", "https://site.com/noise", 3, 5, 8.0)],
        [grain("q", "https://site.com/noise", 1, 4, 9.0)],
    )
    keys = [f["key"] for f in below["pages"]["winners"] + below["pages"]["losers"]]
    assert not noise and "https://site.com/noise" not in keys


def test_winners_losers_min_impressions_threshold():
    analysis = build_analysis(
        [grain("q", "https://site.com/tiny", 2, 5, 3.0)],
        [grain("q", "https://site.com/tiny", 1, 3, 3.0)],
    )
    assert analysis["pages"]["winners"] == analysis["pages"]["losers"] == []
    assert analysis["has_data"] is False or analysis["totals"]["impressions"] == 5


# --- CTR-vs-position gap detection -----------------------------------------------


def test_expected_ctr_curve():
    assert expected_ctr(1) == pytest.approx(0.28)
    assert expected_ctr(2) == pytest.approx(0.15)
    assert expected_ctr(3) == pytest.approx(0.10)
    assert expected_ctr(4.5) == pytest.approx(0.06)
    assert expected_ctr(7) == pytest.approx(0.03)
    assert expected_ctr(50) == pytest.approx(0.01)


def test_ctr_gap_detected_when_actual_below_curve():
    # position ~1 => expected 28%; 0.8% actual is far below.
    metrics = {"https://site.com/page": {"clicks": 4, "impressions": 500, "position": 1.2}}
    gaps = seo_report.ctr_gaps(metrics)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["key"] == "https://site.com/page"
    assert gap["ctr"] == pytest.approx(0.008)
    assert gap["expected_ctr"] == pytest.approx(0.28)


def test_ctr_gap_not_flagged_at_or_above_curve():
    metrics = {"https://site.com/page": {"clicks": 150, "impressions": 500, "position": 2.0}}
    assert seo_report.ctr_gaps(metrics) == []
    # below the impression floor
    assert seo_report.ctr_gaps({"https://site.com/small": {"clicks": 0, "impressions": 10, "position": 1.0}}) == []


def test_untargeted_query_detection():
    rows = [
        grain("culvert repair cost", "https://site.com/", 0, 40, 25.0),
        grain("culvert repair cost", "https://site.com/blog", 0, 20, 30.0),
        grain("culvert repair permit", "https://site.com/blog/", 0, 30, None),
        grain("culvert repair permit", "https://site.com/", 0, 5, 35.0),
    ]
    candidates = untargeted_queries(rows, min_impressions=10)
    queries = {c["query"]: c for c in candidates}
    assert set(queries) == {"culvert repair cost", "culvert repair permit"}
    assert all(c["avg_position"] > 20 or c["landing_paths"] == [] or set(c["landing_paths"]) <= seo_report.GENERIC_PAGE_PATHS for c in candidates)


def test_targeted_query_not_flagged():
    rows = [grain("culvert repair cost", "https://site.com/services/culvert-repair", 12, 50, 3.0)]
    assert untargeted_queries(rows, min_impressions=10) == []


# --- empty data path ------------------------------------------------------------


def test_empty_data_reports_no_data():
    analysis = build_analysis([], [])
    assert analysis["has_data"] is False
    report = {
        "week_ending": "2026-10-18",
        "site_url": "(all sites)",
        "has_data": False,
        "totals": {"clicks": 0, "prior_clicks": 0, "impressions": 0, "prior_impressions": 0},
        "pages": {"winners": [], "losers": []},
        "queries": {"winners": [], "losers": []},
        "ctr_gaps_pages": [],
        "ctr_gaps_queries": [],
        "untargeted_queries": [],
        "content_changes": [],
        "ingest_health": {"available": False},
    }
    markdown = format_report_markdown(report)
    assert "No data yet" in markdown
    assert markdown.startswith("# SEO weekly report — week ending 2026-10-18")


# --- learnings entry + idempotent append ------------------------------------------


def test_format_learnings_entry_matches_prescribed_shape():
    entry = {
        "date": "2026-10-18",
        "title": "Something changed",
        "hypothesis": "h",
        "change": "c",
        "metric": "m",
        "result": "r",
        "learning": "l",
        "status": "open",
    }
    block = format_learnings_entry(entry)
    assert block == (
        "### 2026-10-18 Something changed\n"
        "- Hypothesis: h\n"
        "- Change: c\n"
        "- Metric: m\n"
        "- Result: r\n"
        "- Learning: l\n"
        "- Status: open\n"
    )


def test_append_learning_creates_file_and_heading(tmp_path):
    playbook = tmp_path / "knowledge" / "seo-playbook.md"
    appended = append_learning(playbook, learnings_entry_from_report({"week_ending": "2026-10-18", "has_data": False}))
    assert appended is True
    text = playbook.read_text()
    assert EXPERIMENT_LOG_HEADING in text
    assert "### 2026-10-18 No GSC data yet for week ending 2026-10-18" in text


def test_append_learning_inserts_under_existing_heading(tmp_path):
    playbook = tmp_path / "seo-playbook.md"
    playbook.write_text("# Playbook\n\nSome intro.\n\n## Other section\n\n- keep me\n\n" + EXPERIMENT_LOG_HEADING + "\n\n### 2026-01-01 Older entry\n- Result: r\n")
    appended = append_learning(playbook, learnings_entry_from_report({"week_ending": "2026-10-18", "has_data": False}))
    assert appended is True
    text = playbook.read_text()
    idx_new = text.index("### 2026-10-18")
    idx_old = text.index("### 2026-01-01 Older entry")
    heading = text.index(EXPERIMENT_LOG_HEADING)
    assert heading < idx_new < idx_old
    assert "- keep me" in text


def test_append_learning_is_idempotent(tmp_path):
    playbook = tmp_path / "seo-playbook.md"
    entry = learnings_entry_from_report({"week_ending": "2026-10-18", "has_data": False})
    assert append_learning(playbook, entry) is True
    before = playbook.read_text()
    assert append_learning(playbook, entry) is False
    assert playbook.read_text() == before


def test_learnings_entry_has_all_required_fields():
    entry = learnings_entry_from_report({"week_ending": "2026-10-18", "has_data": False})
    for field in ("hypothesis", "change", "metric", "result", "learning", "status"):
        assert field in entry
    assert entry["status"] in ("open", "confirmed", "refuted", "inconclusive")


# --- ticket standard validator (mirror of hive-member-mcp.js) --------------------


def test_validate_ticket_standard_accepts_well_formed_ticket():
    result = validate_ticket_standard(
        {
            "message": "Do the thing",
            "metadata": {
                "type": "chore",
                "points": 2,
                "goal": "Fix the thing",
                "acceptance_criteria": ["Thing fixed"],
                "scope": "In scope: repo A. Out of scope: repo B.",
            },
        }
    )
    assert result["metadata"]["tier"] == "cheap"
    assert result["metadata"]["points"] == 2
    assert result["message"].startswith("Do the thing")
    assert "Type: chore | Points: 2" in result["message"]
    assert "Acceptance criteria:\n- Thing fixed" in result["message"]
    assert "Scope: In scope: repo A. Out of scope: repo B." in result["message"]


@pytest.mark.parametrize(
    "overrides, expected_fragment",
    [
        ({"type": "spike"}, "metadata.type must be one of"),
        ({"points": 4}, "metadata.points must be one of"),
        ({"goal": ""}, "metadata.goal must state the outcome"),
        ({"acceptance_criteria": []}, "metadata.acceptance_criteria must list"),
        ({"scope": "  "}, "metadata.scope must name the repo/paths"),
        ({"requires_tools": ["not-a-tool"]}, "unknown tool"),
        ({"bug_modifiers": ["something_else"]}, "bug_modifiers may only contain"),
    ],
)
def test_validate_ticket_standard_rejects_bad_metadata(overrides, expected_fragment):
    metadata = {
        "type": "feature",
        "points": 5,
        "goal": "g",
        "acceptance_criteria": ["a"],
        "scope": "s",
    }
    metadata.update(overrides)
    with pytest.raises(ValueError, match=expected_fragment):
        validate_ticket_standard({"message": "m", "metadata": metadata})


def test_validate_ticket_standard_bug_bumps_and_tier():
    result = validate_ticket_standard(
        {
            "message": "m",
            "metadata": {
                "type": "bug",
                "points": 5,
                "goal": "g",
                "acceptance_criteria": ["a"],
                "scope": "s",
                "bug_modifiers": ["needs_diagnosis", "needs_diagnosis", "touches_production"],
            },
        }
    )
    assert result["metadata"]["points"] == 8
    assert result["metadata"]["base_points"] == 5
    assert result["metadata"]["tier"] == "power"
    assert "(5 + 3 bug bump)" in result["message"]


def test_validate_ticket_standard_rejects_above_max_points():
    with pytest.raises(ValueError, match="above 13"):
        validate_ticket_standard(
            {
                "message": "m",
                "metadata": {
                    "type": "bug",
                    "points": 13,
                    "goal": "g",
                    "acceptance_criteria": ["a"],
                    "scope": "s",
                    "bug_modifiers": ["needs_diagnosis"],
                },
            }
        )


# --- built follow-up tickets satisfy the real standard ----------------------------


def _require_checklist_sections(body: str) -> None:
    for header in ("Owner files", "Dependencies", "Root cause / context", "Pattern to mirror", "Runnable DoD", "Authority", "Scope"):
        assert f"## {header}" in body
    assert "Anti-fabrication" in body
    assert "do not ask clarifying questions" in body


def test_title_rewrite_ticket_passes_standard_and_coverage():
    ticket = title_rewrite_ticket({"key": "https://site.com/blog/my-post", "position": 4.0, "ctr": 0.008, "impressions": 250, "expected_ctr": 0.06})
    validate_ticket_standard({"message": ticket["message"], "metadata": ticket["metadata"]})  # must not raise
    assert ticket["metadata"]["type"] == "chore"
    assert ticket["metadata"]["goal"] == ticket["title"]
    assert "Rewrite title for page https://site.com/blog/my-post: CTR 0.8% at position 4" in ticket["title"]
    assert "250 impressions" in ticket["message"]
    assert "heuristic curve expects ~6%" in ticket["message"]
    _require_checklist_sections(ticket["message"])


def test_new_page_ticket_passes_standard_and_coverage():
    ticket = new_page_ticket({"query": "culvert repair cost", "impressions": 60, "avg_position": 25.0, "landing_paths": ["/", "/blog"]})
    validate_ticket_standard({"message": ticket["message"], "metadata": ticket["metadata"]})  # must not raise
    assert ticket["metadata"]["type"] == "feature"
    assert "culvert repair cost" in ticket["title"]
    assert "60 impressions" in ticket["message"]
    _require_checklist_sections(ticket["message"])


def test_followup_tickets_are_all_valid():
    gaps = [{"key": f"https://site.com/p{i}", "position": 1.0, "ctr": 0.0, "impressions": 90, "expected_ctr": 0.28} for i in range(6)]
    untargeted = [{"query": f"q {i}", "impressions": 30, "avg_position": 30.0, "landing_paths": []} for i in range(6)]
    tickets = followup_tickets(gaps, untargeted)
    assert len(tickets) == 10
    for ticket in tickets:
        validate_ticket_standard({"message": ticket["message"], "metadata": ticket["metadata"]})


# --- git history parser on a real temp repo ----------------------------------------


def _git(repo: Path, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env)


def test_parse_git_log():
    text = (
        "COMMIT:aaaa1111|2026-10-12 10:00:00 +0000|feat(content): rewrite waterproofing title\n"
        "3\t1\tsrc/pages/services/waterproofing.astro\n"
        "\n"
        "COMMIT:bbbb2222|2026-10-13 09:30:00 +0000|feat(blog): new culvert post\n"
        "10\t0\tsrc/content/blog/culvert.md\n"
        "2\t2\tsrc/layouts/Blog.astro\n"
    )
    records = parse_git_log(text)
    assert len(records) == 2
    assert records[0] == {"commit": "aaaa1111", "committed_at": "2026-10-12 10:00:00 +0000", "subject": "feat(content): rewrite waterproofing title", "files": ["src/pages/services/waterproofing.astro"]}
    assert records[1]["files"] == ["src/content/blog/culvert.md", "src/layouts/Blog.astro"]


def test_git_content_changes_against_temp_repo(tmp_path):
    repo = tmp_path / "site"
    (repo / "src/pages").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "src/pages/services.astro").write_text("A\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat(content): new services page", "--date=2026-10-12T10:00:00 +0000", env_extra={"GIT_COMMITTER_DATE": "2026-10-12T10:00:00 +0000"})
    (repo / "src/content/blog").mkdir(parents=True)
    (repo / "src/content/blog/old.md").write_text("old\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat(content): old post", "--date=2026-09-01T10:00:00 +0000", env_extra={"GIT_COMMITTER_DATE": "2026-09-01T10:00:00 +0000"})
    (repo / "src/layouts").mkdir()
    (repo / "src/layouts/Layout.astro").write_text("L\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat(layout): tweak", "--date=2026-10-14T10:00:00 +0000", env_extra={"GIT_COMMITTER_DATE": "2026-10-14T10:00:00 +0000"})
    changes = git_content_changes(repo, date(2026, 10, 1))
    subjects = [c["subject"] for c in changes]
    assert subjects == ["feat(layout): tweak", "feat(content): new services page"]
    assert changes[0]["committed_date"] == "2026-10-14"
    assert "src/layouts/Layout.astro" in changes[0]["files"]
    assert all("old post" not in s for s in subjects)
    assert git_content_changes(None, date(2026, 10, 1)) == []
    assert git_content_changes(tmp_path / "missing", date(2026, 10, 1)) == []


# --- PR command construction and mocked invocation ---------------------------------


def test_pr_commands_shape():
    cmds = pr_commands(Path("/repo"), Path("kb/seo-playbook.md"), "2026-10-18")
    assert cmds[0][:4] == ["git", "-C", "/repo", "checkout"]
    assert cmds[0][-2:] == ["-b", "seo/learnings-2026-10-18"]
    flat = " ".join(" ".join(c) for c in cmds)
    assert "commit" in flat and "push" in flat and "gh" in flat and "pr" in flat
    assert all("merge" not in c for c in cmds)  # the job never self-merges


def test_open_learning_pr_runs_only_explicit_mocks():
    from company_ops.seo_report import open_learning_pr

    with patch.object(subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        open_learning_pr(Path("/repo"), Path("kb/seo-playbook.md"), "2026-10-18")
        assert run.call_count == 5
        assert not any(c == "merge" for call in run.call_args_list for c in call.args[0])


def test_file_tickets_uses_hive_client_with_validated_metadata():
    ticket = title_rewrite_ticket({"key": "https://site.com/x", "position": 1.0, "ctr": 0.001, "impressions": 100, "expected_ctr": 0.28})
    with patch.object(seo_report, "file_feedback_work") as fb:
        fb.return_value = {"id": "abc"}
        results = file_tickets([ticket], hive_url="http://hive:4100")
    assert results == [{"id": "abc"}]
    args, kwargs = fb.call_args
    assert args[0] == "http://hive:4100"
    assert args[1] == ticket["title"]
    validate_ticket_standard({"message": args[2], "metadata": args[3]})


def test_metadata_of_built_tickets_has_no_unknown_keys():
    ticket = new_page_ticket({"query": "q", "impressions": 30, "avg_position": 25.0, "landing_paths": []})
    allowed = {"type", "points", "goal", "acceptance_criteria", "scope", "bug_modifiers", "tier", "requires_tools", "approval_ref", "base_points"}
    assert set(ticket["metadata"]) <= allowed
