# Ticket standard

Every piece of work submitted to Hive (`hive_submit`) is a ticket with the same
shape, so agents can read each other's work, price it, and route it. `hive_submit`
rejects anything that does not meet this standard, before it reaches Hive.

## Required fields

`message` is the task text. `metadata` must carry:

| Field | Meaning |
|---|---|
| `type` | `feature`, `bug` or `chore` |
| `points` | Effort and risk on the scale below: 1, 2, 3, 5, 8 or 13 |
| `goal` | The outcome, in one or two sentences |
| `acceptance_criteria` | Array of at least one check someone else can verify |
| `scope` | Repo and paths in scope, and what is explicitly out of scope |

The tool appends the type, points, goal, criteria and scope to the message text,
so the worker that picks the ticket up sees them in the prompt.

## Points

Points measure how much work and how much judgment the ticket needs, not hours.

| Points | Looks like | Tier |
|---|---|---|
| 1 | One-line or config change, typo, doc fix | cheap |
| 2 | Small change in one file with an obvious test | cheap |
| 3 | A contained change across a few files | cheap |
| 5 | A standard feature or a bug with a known cause | standard |
| 8 | Cross-module work, unclear cause, security or data sensitive | power |
| 13 | Large or cross-repo; consider splitting first | power |

Anything above 13 must be split into smaller tickets. A ticket that cannot be
described with acceptance criteria is not ready to submit.

## Bugs score higher

A bug that needs more work gets more points. Set `bug_modifiers` on a bug:

- `needs_diagnosis`: the cause is not known yet (+2)
- `touches_production`: production traffic or data is involved (+1)

The coordinator receives the total (`points`) and the original (`base_points`).
A bug that lands above 13 after the bump is rejected: split it into a diagnosis
ticket and a fix ticket.

## Points set the tier

Tier follows the final points: up to 3 is `cheap`, 5 is `standard`, 6 and above is
`power`. Pass `metadata.tier` to override when the points understate the need (for
example a 2-point change in an auth path that should go to the power pool). Cheap
tickets go to the cheap OpenCode pools first; power tickets go to Claude. See the
worker tiers section of `hive/README.md`.

## Calibration

Workers report `estimated_cost` and `actual_cost` in `hive_complete` feedback and
the cost ledger records the outcome per allocation. Compare actual cost by points
band from time to time: if 3-point tickets routinely cost like 8-point ones, move
the guide above, not individual tickets.

## When nobody bids

Work with no interested bid for 30 minutes is re-offered by the coordinator: earlier
declines are cleared, the tier drops one step and `incentive` rises by 0.5, which
workers use to discount their bid. After three re-offers it escalates for a human or
manager to decide. Work with `requires_tools` and no capable agent skips the re-offer
tier drop and escalates with the missing tool names. A re-offered ticket that keeps failing usually needs splitting or
clearer acceptance criteria, not another retry.

## Requiring tools

A ticket that cannot be done without a specific tool lists it in the optional
`metadata.requires_tools` array. Known names: `browser`, `hindsight`, `kubectl-deploy`,
`image-build`, `hermes-data`, `postgres-admin`, `github-pr`, `reddit-publish`,
`substack-publish`, `marketing-db-read`, `marketing-ingest`; `hive_submit` rejects any other name. The last five
belong to the marketing-lead agent and are declared only when their credential env (or, for substack-publish and marketing-ingest, the sidecar ready marker) is present
(see `docs/MARKETING-LEAD.md`).

Agents declare `capabilities.tools` at registration (set per pod from `AGENT_TOOLS`, only
for tools the pod verifiably has). The coordinator marks an agent missing any required
tool ineligible with reason `missing_tools` before scoring and records it in the bids, so
a cheaper agent without the tool can never win and the score fallback never picks it. If
no registered execute-capable agent has every required tool, the work is not allocated;
it escalates for a human with the missing tool names, and the tier is held rather than
dropped. Tickets without `requires_tools` route exactly as before.

## Approval for publishing (`approval_ref`)

A ticket asking the marketing-lead agent to publish to Reddit or Substack carries
`metadata.approval_ref`, a reference to the Hermes/Board approval (decision id, ticket id or
URL). Hive passes unknown metadata through untouched. The marketing-lead refuses to publish
without it and says why. Drafting never needs it. Hive does not yet expose payload metadata
on `GET /tasks/:id`, so the agent can check that the ref is present and well formed but not
that it is genuine (see `docs/MARKETING-LEAD.md`, limitations).

## Example

```json
{
  "message": "Login page returns 500 when the email contains a plus sign.",
  "metadata": {
    "type": "bug",
    "points": 3,
    "bug_modifiers": ["touches_production"],
    "goal": "Emails with a plus sign log in successfully.",
    "acceptance_criteria": [
      "Regression test covers a plus-sign email",
      "Existing auth tests pass"
    ],
    "scope": "repo app, src/auth only; no session or token changes"
  }
}
```

That is 3 + 1 = 4 points, which routes as `standard`.

A QA sweep that needs a real browser:

```json
{
  "message": "Run the pre-launch QA sweep against staging.",
  "metadata": {
    "type": "chore",
    "points": 5,
    "goal": "Every launch-checklist page is exercised in a real browser and defects are filed.",
    "acceptance_criteria": ["Report lists each page checked with pass/fail"],
    "scope": "staging site only; no code changes",
    "requires_tools": ["browser"]
  }
}
```
