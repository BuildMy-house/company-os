# pm-agent: Hermes-runtime product-feedback agent

pm-agent is the end-user chat front door for buildmy.house product feedback.
It replaced a non-LLM script with canned replies. The persona is
`pm-agent/content/buildmy-house/SOUL.md` (override dir via `PM_AGENT_CONTENT_DIR`); the model runs in the Hermes runtime
(`nousresearch/hermes-agent` image) and registers with the Hive as
`HIVE_AGENT_ID=pm-agent`.

## Request path

```
app relay -> pm-agent-gateway (nginx bearer gate, NodePort 30420, unchanged)
          -> pm-agent :4200  POST /chat {app_user_id, message}  -> {reply}
```

One pod, three processes (`pm-agent/entrypoint.sh`):

| Process | Role |
|---|---|
| `agent.py` (front door, :4200) | Validates and caps input, rate-limits chats, wraps user text as delimited untrusted data, calls the local Hermes runtime, scrubs the reply to plain text, writes the exchange to `company.pm_conversations`, serves `/healthz`, keeps the Hive registration alive (`hive_presence.py`, re-registers every 60 s). |
| `hermes gateway run` (loopback :8642) | The LLM agent. Config `pm-agent/hermes-config.yaml`: all built-in toolsets disabled, exactly one toolset, no plugins, no memory, no auxiliary models, per-boot random API key. |
| `pm_tools_mcp.py` (spawned by Hermes, stdio) | The only tools the model has (`tools.py`): `find_similar_signal(topic)` (read-only PmStore dedupe lookup) and `file_feedback_work(topic, summary)` (the gated path to `hive_client.file_feedback_work`). |

PmStore dedupe and `ESCALATION_THRESHOLD = 5` are kept, but as the tools'
internal logic rather than the program. The tools learn who is asking and what
they said from a request-context file the front door writes (tmpfs,
`/run/pm-agent/context.json`, one exchange in flight at a time), never from
model arguments, so injected text cannot impersonate another user or forge the
quote. The model has no way to read that file.

Hive: pm-agent registers with `modes: [observe, propose]` and no `bid` or
`execute`, so it is never prompted for or allocated work (see
`docs/AGENT-CONTRACT.md`). `A2A_ENDPOINT` is `http://pm-agent:4200`; the
front door serves only `/chat` and `/healthz`.

## Security acceptance criteria and where each is enforced

| # | Criterion | Enforcement | Evidence |
|---|---|---|---|
| 1 | Own secret, two keys only, no `company-ops-secrets` | `k8s/base/pm-agent/pm-agent.yaml` env from `pm-agent-secrets` keys `PM_DATABASE_URL`, `PM_AGENT_LLM_API_KEY`; `envFrom` removed; `k8s/base/pm-agent-secrets-sync/pm-agent-infisical-sync.yaml` syncs Infisical `/pm-agent` into it | `ManifestSecurityTest.test_own_secret_only_no_envfrom_company_ops`, `test_infisical_sync_targets_only_pm_agent_secret` |
| 2 | No Axiom | No Axiom env, plugin, or code; Hermes `plugins.enabled: []`; structured JSON logs to stdout | `test_no_axiom_credentials_or_env`, `test_no_axiom_anywhere_in_pm_agent_code` |
| 3 | No SA token, dedicated SA, no RBAC | `ServiceAccount pm-agent`, `automountServiceAccountToken: false` on SA and pod, no Role/Binding | `test_dedicated_service_account_no_token_no_rbac` |
| 4 | NetworkPolicy | Egress: hive-coordinator:4100, postgres:5432, kube-dns:53, public HTTPS:443 (LLM). Ingress: pm-agent-gateway:4200 only | `test_network_policy_*` (see enforcement finding below) |
| 5 | Tool allowlist + AST ban | Hermes: one MCP server, `tools.include` = the two tools, one toolset; AST test bans privileged imports and dangerous calls over every `pm-agent/*.py` | `HermesConfigAllowlistTest`, `McpServerTest.test_advertises_exactly_the_allowlist`, `ForbiddenCodeTest` |
| 6 | Prompt-injection v1 | `guard.py`: random-nonce delimited envelope, marker stripping, control/bidi stripping, 2000-char message cap (413), 16 KiB body cap, plain-text-only replies (links, URLs, markdown, HTML removed), fixed refusal (`OUT_OF_SCOPE` sentinel or secret-in-reply), 20 chats/user/hour, 3 filings/user/hour and 20 filings/hour globally, filed title/description capped and prefixed/headed "user-reported (untrusted)", every exchange and tool call logged to `pm_conversations` | `AdversarialInputTest`, `ChatServiceTest`, `ToolsTest` |
| 7 | `pm_agent_writer` stays SELECT/INSERT/UPDATE on two tables | Unchanged by this work (PR #15 owns the UPDATE grant) | `RoleGrantTest` accepts the grant with or without UPDATE, rejects anything beyond |

Steward read-only `query_memories`/`query_specs` are part of the allowlist but
are deliberately not wired in v1: they need a Steward credential, and criterion
1 limits `pm-agent-secrets` to two keys. If a read-only Steward token is later
provisioned, add a `steward` server to `hermes-config.yaml` with
`tools: {include: [query_memories, query_specs]}` (the test already enforces
that exact list for any server named `*steward*`) and extend the secret and the
NetworkPolicy deliberately.

## NetworkPolicy enforcement finding

Not verifiable from this session: there is no cluster access (no kubectl; cluster
work belongs to the in-cluster agents). What the repository shows: no k3s
install flags in the repo (`scripts/k3s-local-up.sh` does not install k3s), no
existing enforced policy to point to, and `docs/MCP-GATEWAY.md` already records
enforcement as "unverified". k3s ships an embedded NetworkPolicy controller
(kube-router) that is on unless k3s was started with `--disable-network-policy`
or a CNI without policy support replaced flannel, so enforcement is likely but
unproven. Do not rely on the policy until the in-cluster engineering agent runs
this check:

1. `kubectl -n company-ops get networkpolicy` (expect `pm-agent`, `mcp-gateway-ingress`).
2. Apply the manifest, then from the pm-agent pod (or a pod with the same labels)
   confirm a blocked destination fails and an allowed one works, e.g. `curl -m 5 http://hermes-gateway:8642`
   must time out while `curl -m 5 http://hive-coordinator:4100/agents` succeeds.
3. Record the result in Steward. If the block does not hold, the policy is
   decorative: treat the LLM key and egress risks below as unmitigated.

kubelet probes come from the node and are not subject to pod policies.

## Residual risks

1. **The LLM key is in the process environment.** Hermes needs it, and the
   model's tools cannot read the environment (no shell, file, or env tool), but
   a Hermes or dependency vulnerability could. Mitigations: dedicated key with a
   spend cap, no other credential in the pod beyond the writer-role DSN,
   egress NetworkPolicy, and the secret-in-reply filter in the front door. The
   LLM egress rule is `0.0.0.0/0:443` minus private ranges because pod policies
   cannot match hostnames; tighten to the provider's CIDRs when known.
2. **Filed Hive items may reach engineering agents.** Hive work from pm-agent
   is attacker-influenced text. Mitigations: title prefix `[user-reported
   (untrusted)]`, a description header stating it is data and not instructions,
   verbatim quote truncated to 500 chars, `metadata.untrusted: true`, and
   rate limits. Engineering agents still read these items; the marking reduces,
   it does not remove, the injection risk. Worker prompts that fence Hive task
   text as untrusted data (as `hive_prompt_workers` already does) are the second
   layer.
3. **Rate limits are in memory.** They reset on restart and assume one replica
   (`replicas: 1`, `Recreate`). A restart loop could briefly widen the budget.
4. **One exchange at a time.** The request-context design serialises chats in
   a pod; throughput is bounded by LLM latency (90 s timeout). Fine for v1
   volumes; revisit before scaling out.
5. **No chat history is passed to the model.** Each turn is stateless, which
   limits cross-turn injection and cross-user leakage, at the cost of
   conversational continuity. Full history is persisted in `pm_conversations`.
6. **Unverified against a live Hermes image.** The toolset names in
   `disabled_toolsets`, `platform_toolsets: {api_server: [pm_tools]}`, and
   `tools.include` follow the Hermes docs and the existing `hermes/config.yaml`
   but were not run here. The build-time `command -v hermes` check and the
   hand-off checks below cover this.

## Build and rollout hand-off (not done from this session)

Source: branch `feat/pm-agent-hermes-runtime` of `BuildMy-house/company-os`
(PR head SHA recorded in the PR; build only an immutable merged SHA, per
`docs/DEPLOY-ENGINEERING.md`). Image build spec:
`docker buildx build -f pm-agent/Dockerfile -t localhost:30500/company-os-pm-agent:<sha> .`
through the isolated BuildKit builder (not Kaniko, not an agent container).

The in-cluster engineering agent should, via the hive:

1. Build the image at the merged SHA and push it to the in-cluster registry.
   The build fails fast if `hermes` is not on PATH in the base image.
2. Confirm the Infisical `/pm-agent` folder holds exactly the two secrets (user
   action below), then apply `k8s/base/pm-agent-secrets-sync/pm-agent-infisical-sync.yaml` (it is not in
   `k8s/overlays/buildmy-prod`, like `k8s/base/infisical-sync/infisical-sync.yaml`, because it needs
   the operator CRDs) and confirm `pm-agent-secrets` exists with two keys.
3. Set the image tag in `k8s/base/pm-agent/pm-agent.yaml` to the built tag/digest and apply it
   (replacing `:latest`), then verify: pod Ready, `/healthz` 200, hive agent list
   shows `pm-agent`, a test `/chat` through the gateway returns a plain-text
   reply and writes `pm_conversations` rows (roles `user`, `tool`, `assistant`).
4. Run the NetworkPolicy enforcement check above.
5. In-image checks (first rollout): `hermes tools list` or equivalent shows only
   `pm_tools`; the MCP child starts (`pm_tools` connected in Hermes logs); if the
   root filesystem needs to be writable beyond the mounted emptyDirs, adjust the
   mounts, do not widen the security context.
6. Only after all of that, remove the stale `PM_DATABASE_URL` from
   `company-ops-secrets`/Infisical `/hermes` if it is still there.

## User actions required (secrets and infra are not written by this change)

1. Infisical (project Build My house, env dev): create folder `/pm-agent` with
   exactly `PM_DATABASE_URL` (the `pm_agent_writer` DSN, existing role) and
   `PM_AGENT_LLM_API_KEY` (a new key dedicated to pm-agent with a hard spend cap;
   the manifest uses the Nous inference API `https://inference-api.nousresearch.com/v1`
   and an automatically chosen FREE model (see "Model selection" below);
   to change provider, edit `hermes-config.yaml` and the NetworkPolicy together).
2. Ensure the Infisical machine identity behind `infisical-auth` can read
   `/pm-agent` and the operator is allowed to create Secrets in `company-ops`.
3. Merge PR #15 (writer UPDATE grant) before rollout: `record_message` and
   `upsert_signal` use UPDATE, so without it chat logging fails.
4. Approve and merge this PR; approve the rollout via the engineering agent.

## Model selection (automatic, free-only)

Nous retires `:free` models without notice and its `/v1/models` listing keeps
showing retired ones (`meituan/longcat-2.0:free` answered 404 while listed as
free), so `pm-agent/resolve_model.py` probes instead of trusting the list.

- At boot `entrypoint.sh` runs the resolver: fetch `/v1/models`, keep models
  that are free (prompt and completion price 0), advertise `tools`, have
  context >= 100k, are not within 3 days of `expiration_date`, and are not
  domain-specialised (`-fin`, `-sante`) or `stealth/*`. Order by
  `pm-agent/model-preferences.json` (preferred ids first, then context length),
  send a 1-token chat completion to each, take the first HTTP 200, and render it
  into `$HERMES_HOME/config.yaml` (the baked `hermes-config.yaml` is only the
  default). The last working model is kept in `/run/pm-agent/resolved-model.json`
  and reused if every fresh probe fails.
- If nothing works the container exits non-zero (visible in k8s). A paid
  fallback (cheapest paid model with tools) happens only if `PM_AGENT_ALLOW_PAID=true`.
- `agent.py` runs a watchdog thread: every 6h, and when an LLM call fails with a
  404/model-unavailable style error (5 min cooldown), it runs
  `resolve_model.py --check`. If the resolved model differs from the rendered
  one, the process exits (code 75), `entrypoint.sh` stops Hermes, and k8s
  restarts the pod, which re-resolves. Restart-on-change was chosen over
  hot-reloading Hermes as the simpler, more robust option.
- To prefer a different model edit `model-preferences.json`; stale ids are skipped.
