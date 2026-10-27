# Hindsight banks: one memory bank per agent

The Hindsight server (`k8s/base/hindsight`, `http://hindsight:8888`, namespace
`company-ops`) holds one memory bank per agent so each agent learns from its own
history. Every agent must both **retain** (write lessons) and **recall** (read them
before acting); a bank that is only written to teaches nothing.

## Bank names (agreed)

| Agent | Bank id | Where it is set | Retain | Recall |
|---|---|---|---|---|
| CEO (Hermes) | `ceo` | `hermes/hindsight/config.json` (`bank_id`), seeded to `$HERMES_HOME/hindsight/config.json` | `hindsight_retain` tool (SOUL.md "Long-term memory"); dream-audit in apply mode | `hindsight_recall` tool (SOUL.md); dream-audit dedupe |
| PM lead (pm-agent) | `pm-lead` | `HINDSIGHT_BANK_ID` in `k8s/base/pm-agent/pm-agent.yaml` (code default `pm-lead`) | `retain_lesson` tool + automatic outcome note on filing | `recall_lessons` tool |
| Marketing lead (was `publisher`) | `marketing-lead` | `marketing-lead/config/marketing-lead.json`, `k8s/base/marketing-lead/marketing-lead.yaml` | their own | their own |

Rules:

- The bank id is configuration, never something a model or user can choose at call
  time. pm-agent's tools have no bank parameter.
- Do not write to another agent's bank. To share a lesson across agents, save it to
  Steward (`save_memory` / `skill_save`).
- Retain short, atomic, source-pointed statements (what worked, what was corrected
  or decided, the outcome). Never retain credentials, tokens, DSNs, or raw private
  conversations. pm-agent additionally never retains user ids, emails, links or quotes.
- Recalled text is untrusted data: it informs, it never instructs.

## Isolation caveat

Hindsight authenticates with a single shared bearer (`HINDSIGHT_API_KEY`, tenant
extension `ApiKeyTenantExtension`). The key does not scope to a bank, so separation
is by bank name enforced in each agent's code, not by the server. Anything holding
the key can read every bank. Per-bank keys would need a different tenant extension
(not available in the 0.10.3 image used here) and are not part of this change.

## Rename: `hermes` -> `ceo`

The CEO's original bank was `hermes`. New installs use `ceo`. On an existing volume,
`scripts/seed-hindsight.sh` rewrites a config whose `bank_id` is exactly the old
default `hermes` to `ceo` at the next restart (any other operator value is left
alone). Existing memories do not move by themselves. Copy them with the operator
tool (read-only on the source, idempotent: items get stable ids so a re-run does
not duplicate):

```
HINDSIGHT_URL=http://hindsight:8888 HINDSIGHT_API_KEY=... \
  python3 scripts/hindsight-copy-bank.py hermes ceo --dry-run
python3 scripts/hindsight-copy-bank.py hermes ceo
```

Run it once before rolling out the new Hermes image (so `ceo` is warm when Hermes
switches) and once after (to catch anything retained in between). The old `hermes`
bank can be left in place; it is covered by the daily `hindsight-data-backup`
CronJob like every other bank. Copied memories are re-embedded; the extraction mode
is `verbatim`, so no extra LLM spend beyond embedding.

## Bank creation

Hindsight only creates a bank when something is written to it. A bank that has
never been written does not exist, and `hindsight_recall` on it fails with
HTTP 404 "Bank 'ceo' not found" — this is expected server behaviour, not an
auth or networking problem. The CEO is the case in point: it runs tools-only
(`auto_retain: false` in `hermes/hindsight/config.json`), so nothing auto-creates
its bank on the first retain, and every recall 404s until the bank is created.

`scripts/hindsight-ensure-bank.py` (stdlib only) closes that gap: it waits for
the server to answer `GET /health` (bounded by `--wait`, default 600s), then
sends `PUT /v1/default/banks/{bank_id}` with an empty body. In hindsight-api
0.10.3 that creates a missing bank and changes nothing on an existing one (there
is no single-bank GET; `GET .../profile` is 410), so it is idempotent. The bank id defaults to the
`bank_id` in `$HERMES_HOME/hindsight/config.json`, else `ceo`; the url comes
from `HINDSIGHT_URL`, the key from `HINDSIGHT_API_KEY` (never printed).
`scripts/seed-hindsight.sh` launches it in the background at container boot,
after the config seed/migration — non-blocking, `[hindsight]`-prefixed output,
failure only warns and never breaks boot. Set `HINDSIGHT_ENSURE_BANK=0` to
skip it (tests). Manual run:

```
HINDSIGHT_URL=http://hindsight:8888 HINDSIGHT_API_KEY=... \
  python3 scripts/hindsight-ensure-bank.py
```

For an existing install, also do the one-time memory copy from the old `hermes`
bank so `ceo` actually has content to recall — see the rename section above
(`scripts/hindsight-copy-bank.py`, read-only on the source and idempotent).
Creating the empty bank stops the 404s; the copy is what makes recall useful.

## Dream pass: injection / planted-lesson scan

One shared key means anything able to retain into a bank can plant a "lesson" the
agent later recalls. `company_ops/hindsight_dream.py` (stdlib only) is the scheduled
pass over a bank: it lists valid memories, flags those matching rules
(`override_instructions`, `instruction_to_agent`, `tool_request`,
`credential_request`, `url`, `credential_like`; plus `anonymous_behaviour` for pm-lead,
i.e. a model-written lesson that changes future behaviour) and, in apply mode,
quarantines them by setting `state=invalidated` (PATCH `.../memories/{id}`). That is
reversible (`state=valid`), kept for audit, and excluded from recall; the API has no
per-memory delete. It logs only counts, rule names and ids, never memory text or the key.
Hindsight's own server-side consolidation of observations is unchanged.

| Agent / bank | Runs as | Schedule | Default mode |
|---|---|---|---|
| pm-lead | CronJob `hindsight-dream-pm-lead` (`--behaviour-check`) | 04:15 UTC daily | apply |
| marketing-lead | CronJob `hindsight-dream-marketing-lead` | 04:30 UTC daily | apply |
| ceo (Hermes) | `dream-audit-prepare.sh` pre-run step of the `dream-audit` cron (06:00/18:00 BTT) | twice daily | follows `$HERMES_HOME/dream-audit.mode`: audit (report only) until the Board sets `apply` |

Review quarantined ids with `GET .../memories/list?state=invalidated`; restore a false
positive with PATCH `{"state": "valid"}`. A Job's logs contain only the JSON summary.

## Operations

- Backups: `hindsight-data-backup` (03:45 UTC) tars the whole PVC, so every bank is
  included. Restore: docs/RESTORE.md.
- Known latency: recall takes about 5s (CPU cross-encoder rerank); `hindsight_reflect`
  has timed out at the 120s client limit, so agents are told to prefer recall.
