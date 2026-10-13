# Company memory (git-tracked)

Categories: `decisions/`, `learnings/`, `playbooks/`, `research/`, `experiments/`, `hypotheses/` (see each README).

## Capturing `close_work` learnings

`close_work(learned_for_agents=...)` creates a Steward memory but nothing lands in this tree. When a closed ticket's
learning is durable and company-wide (a decision, a repeatable playbook, a lesson), the manager also runs, before
committing the ticket's work:

    scripts/capture-learning.sh learnings <kebab-slug> <steward-task-id> "<title>" <<'EOF'
    <same text passed to learned_for_agents>
    EOF

It writes `memory/<category>/<date>-<slug>.md` (refuses credentials and duplicates) and the manager commits it with
the ticket. Skip it for ticket-local trivia: Steward already has those; this tree is for what a fresh clone should know.
