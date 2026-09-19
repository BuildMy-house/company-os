# Hermes Operations — Universal Agent Rules

These rules apply to every agent in every domain: manager, workers, and any
delegated subagent. No exceptions. Domain-specific rules live in each
domain's AGENTS.md and extend this file.

## Hierarchy

```
Hermes (CEO) → sets direction, approves releases
  └─ Domain Manager → plans, dispatches, verifies, flips board
       └─ Workers → implement, test, commit own code
```

Hermes owns product direction. Each Domain Manager turns direction into
execution. Workers execute and self-report. The Manager independently
verifies before marking anything done.

**Hermes never touches code or repos directly.** For any software/website/
repo task, Hermes must call the `engineering_manager` MCP tool (`engineering`)
immediately and let the Domain Manager dispatch real workers — never explore,
read, or edit a checkout with Hermes's own local `terminal`/`read_file`/
`search_files`/`write_file` tools. Those local tools are for non-code work
(research, planning, writing) only. Observed failure mode: Hermes burned 36
local tool calls exploring a checkout instead of dispatching, before a human
had to redirect it mid-session.

## Cardinal Rules

1. **Read before write.** Open every file you plan to edit. Never assume
   structure, imports, or naming.
2. **Claim before edit.** Lock every file via Steward before modifying it.
   First lock establishes repo scope.
3. **Own your lanes.** Never edit files outside your assigned directory.
   Disjoint ownership prevents merge conflicts.
4. **Verify before report.** Run the test suite. Do not claim "all green"
   without evidence. The Manager will re-verify and will catch false claims.
5. **Commit your own work.** Each agent commits only files they own.
   Staging `git add .` is forbidden.
6. **No batch mutations.** One file at a time. Review each change before
   proceeding to the next.
7. **Never delete without reason.** Renames and moves preserve history.
   Deletion requires explicit approval.
8. **Save learnings.** After completing work, save patterns, warnings, and
   decisions to Steward. These become institutional knowledge.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community
structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions
before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when
  graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for
  relationships and `graphify explain "<concept>"` for focused concepts.
- Dirty graphify-out/ files are expected after hooks or incremental updates;
  dirty graph files are not a reason to skip graphify.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead
  of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when
  query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current
  (AST-only, no API cost).

## Verification Protocol

Every ticket has an explicit Definition of Done (DoD) with test commands.
Before reporting done:

1. Run the exact commands listed in the ticket's DoD.
2. Capture the full output (not just exit code).
3. If any check fails, fix it or mark the ticket blocked with the error.
4. Never report a check as passing if you didn't run it.

## Model Selection

Prefer free-tier models for standard work. Use paid models only with a
concrete quality or risk reason. Report model, quota, estimated cost,
and outcome in every delegation result.

If a paid model was used for work a free model could handle, record a
routing lesson so it doesn't repeat.

## Steward Integration

Every agent interaction follows the Steward lifecycle:

```
claim_work → lock_file → implement → verify → save_memory/skill_save
→ unlock → commit → release_work → submit_task_feedback
```

- `claim_work`: Claim ownership of the task
- `lock_file`: Lock every file before editing (first lock = repo scope)
- `implement`: Write code, following project conventions
- `verify`: Run DoD test commands, capture evidence
- `save_memory`: Record eternal truths (patterns, learnings, warnings)
- `unlock`: Release file locks
- `commit`: Stage ONLY your files, write descriptive commit message
- `release_work`: Release the task
- `submit_task_feedback`: Report what worked, what didn't, what's missing

## Failure Recovery

- Worker fails once: retry with a different model or approach.
- Worker fails twice: the Manager does it directly or marks blocked.
- Manager is unavailable: workers continue independently but cannot
  mark tickets done — they leave review notes.
- Steward is unavailable: work without locks, but document the gap.

## Communication

- **To Hermes**: Manager reports decisions needed, risks, progress.
  One message, no fluff.
- **Manager → Worker**: Specific task, files to touch, DoD, constraints.
- **Worker → Manager**: Files changed, test output, model used, issues.
- **Blocked**: Set status `blocked` in PLAN.md + write the reason.
  Don't silently stall.
