---
name: memory-practice
description: What to keep in the marketing-lead memory bank and topic files
---
# Memory practice

Two places, both small. Hindsight bank marketing-lead: marketing-lead memory retain <text> and
marketing-lead memory recall <query>. Topic files under /workspace/memory (decisions, learnings,
playbooks), each under 40 lines, one topic per file.

Keep: decisions with the reason, what worked or failed and why, repeatable playbooks.
Never keep: credentials, tokens, emails, raw analytics rows, full post drafts.
The Hindsight bank is separated by name only (one shared API key): do not read other banks.
Before writing a post, recall past decisions on the topic; after a post performs, record why.

Cycle rhythm (every 2-3 days): at the START of a cycle run `marketing-lead memory recall "<topic or
last experiment>"` to find the previous hypothesis and what was still open. At the END run
`marketing-lead memory retain-cycle --date YYYY-MM-DD --hypothesis "..." --change "..." --result "..."
--lesson "..."` (result "pending" if not yet measurable; retain the measured result next cycle).
Also save the hand-back to Steward save_memory. This bank is the agent's own learning loop;
the playbook experiment log in git stays the shared record.
