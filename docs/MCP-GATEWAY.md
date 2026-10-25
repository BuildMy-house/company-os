# MCP gateway

`scripts/mcp-gateway.js` hosts the repo's stdio MCP servers (hive member, container-manager,
builder, kubernetes-mcp-server) as supervised children and exposes each as stateless Streamable
HTTP at `POST /<server>`, plus key management at `POST /gateway-admin`, handled in-process rather
than by a child. Plan: Steward `documents/plans/mcp-gateway` + `mcp-gateway-decisions`
(the addendum wins).

## Behaviour
- **Stateless**: no `Mcp-Session-Id`; `GET` returns 405. A gateway restart loses nothing.
- **Auth**: `Authorization: Bearer <token>` per request, presented as one of three kinds:
  - an issued API key (`gwk_...`, see "API keys" below), authenticating the policy identity it
    was issued for;
  - a legacy file token, one per client identity (Secret keys `LOCAL_CLAUDE_TOKEN`,
    `LOCAL_OPENCODE_TOKEN`, `LOCAL_CODEX_TOKEN`, synced from Infisical `/mcp-gateway`), read
    from `/etc/mcp-gateway/tokens/<KEY>` per request, so Infisical rotation needs no restart.
    During migration a file token works only until that identity has any issued key; from then
    on it is retired permanently (audit reason `file_token_retired`), even if every key is later
    revoked;
  - the `gateway-admin` bootstrap file token `GATEWAY_ADMIN_TOKEN`, for the admin identity only.
  Failures are audited with an 8-hex-char SHA-256 fingerprint of the presented token — never the
  plaintext and never the full hash.
- **Fail closed**: with `MCP_GATEWAY_DATABASE_URL` unset the gateway runs in file-token-only
  mode and the admin tools report that the store is not configured. If the store is unreachable,
  key authentication is denied (503) and a file-token identity whose migration state cannot be
  determined is denied with 503 rather than allowed through.
- **Policy** (`k8s/base/mcp-gateway/mcp-gateway.yaml` ConfigMap — the identity source of truth):
  default-deny. A tool is reachable only when listed in `clients.<id>.servers.<name>.tools`;
  enforced on `tools/list` *and* `tools/call`. Hive identities get read tools (`hive_agents`,
  `hive_available_work`, `hive_diagnostics`, `hive_events`, `hive_status`, `hive_wait`,
  `hive_work_bids`) plus `hive_submit` only — no `hive_bid`/`hive_complete`/`hive_heartbeat`/
  `hive_next_work`. Optional `argLimits` per tool;
  `agentId` gives each identity its own hive child (`HIVE_AGENT_ID`), with `HIVE_REGISTER=false`
  so gateway children never appear as agents.
- **Supervision**: a crashed child is respawned with 1s→30s backoff; in-flight calls get
  JSON-RPC `-32002` and clients retry.
- **Long polls** (`hive_wait`) stream as SSE with 15s keepalives; at most
  4 concurrent per identity.
- **Audit**: one JSON line per call to stdout and, if `AXIOM_TOKEN` is set, Axiom. Argument
  *hashes* only. Tool output is redacted for tools in `redactTools`. Key lifecycle and auth
  events — `key_issued`, `key_rotated`, `key_revoked`, `keys_revoked_all`, and `auth_failure`
  with reason `unknown`, `revoked`, `expired`, `malformed` or `file_token_retired` — always
  carry only the same 8-hex-char fingerprint.

## Identities

- `local-claude`, `local-opencode`, `local-codex`: hive read tools plus `hive_submit`, and the
  kubernetes read tools. Unchanged. During migration they authenticate with their existing file
  tokens until the identity has an issued key (read-only identity key TTL: 90 days).
- `local-claude-deploy` (new): only the container-manager tools `container_upgrade`,
  `container_rollback`, `container_status`, `container_health`, `container_logs` and the builder
  tool `builder_build_and_push`, each constrained by `argLimits`:
  - `container_upgrade` requires a digest-pinned image from `localhost:30500` or
    `registry.company-ops.svc.cluster.local:5000` and a deployment other than `mcp-gateway`; rollback, health and logs exclude `mcp-gateway` as well;
  - `builder_build_and_push` requires `context_ref` (and every `contexts` value) of the form
    `https://github.com/BuildMy-house/<repo>.git#<40-hex-sha>`, an `image_repo` from an explicit
    allowlist (`company-os-mcp-gateway` deliberately excluded, so the gateway cannot rebuild
    itself), and only whitelisted argument names (`_onlyArgs`).

  It was born in the final mechanism: it has no `tokenFile` and authenticates only with an issued
  key. Key TTL 30 days (`keyTtlDays` in the policy).
- `gateway-admin`: the only identity that can issue, rotate or revoke keys, at
  `POST /gateway-admin` (in-process, not a child). Tools: `issue_key(identity, label, ttl_days?)`,
  `rotate_key(key_id, overlap_seconds?)` (default 3600, max 86400), `revoke_key(key_id)`,
  `revoke_all_for_identity(identity)`, `list_keys(identity?)`. It authenticates with a bootstrap
  file token `GATEWAY_ADMIN_TOKEN` held by Nahar — stored in Infisical and synced into Secret
  `mcp-gateway-tokens` by `k8s/base/infisical-sync/infisical-sync.yaml`, currently from path
  `/mcp-gateway` (if the
  admin token is ever kept under a different Infisical path such as `/infra/mcp-gateway`, the
  `secretPath` in `infisical-sync.yaml` must match). It is the single bootstrap root of trust:
  it can never receive an issued key, and it has no child-server access. Hard rule: the
  key-issuing capability is never held by the local Claude session. The local session may only
  request a key or rotation (from Nahar); Nahar, or an agent he explicitly directs holding the
  admin token, performs it.

## API keys

- Format `gwk_<43 chars base64url>`, 256-bit random. Only the SHA-256 hash is stored, in Postgres
  schema `mcp_gateway`, table `api_keys` (columns `id`, `key_sha256`, `identity`, `label`,
  `created_by`, `created_at`, `expires_at`, `last_used_at`, `revoked_at`). The plaintext is shown
  exactly once, in the issue/rotate response.
- A key is bound to exactly one policy identity and is always evaluated against that identity's
  current allowlist and `argLimits` at request time — a key can never exceed or outlive the
  policy. A `ttl_days` on issue may shorten but never extend the identity's default TTL.
  Expired or revoked keys fail immediately (no caching).
- Rotation issues a new key and shortens the old key's expiry to the overlap window.
  `revoke_all_for_identity` locks an identity out entirely; it does not fall back to a file token.
- Storage role: `mcp_gateway_keys` (SELECT/INSERT/UPDATE on `mcp_gateway.api_keys` only, never
  DELETE), declared in `sql/roles.d/mcp_gateway_keys.yaml`, provisioned by
  `scripts/provision-db-roles.sh`; schema in `sql/mcp_gateway_schema.sql`. The DSN reaches the
  gateway as env `MCP_GATEWAY_DATABASE_URL` (a Secret key; Infisical key `MCP_GATEWAY_DATABASE_URL`,
  with the password in Infisical key `MCP_GATEWAY_KEYS_PASSWORD` at `/hermes` for the
  provisioning script).

## Children and credentials in the gateway pod

- The gateway pod now hosts container-manager, using its own scoped ServiceAccount
  `mcp-gateway-deployer` with its token mounted at `/var/run/secrets/container-manager` and
  selected by env `CONTAINER_MANAGER_TOKEN_PATH`/`CONTAINER_MANAGER_CA_PATH` (the gateway's own
  SA stays read-only).
- It also hosts builder: the `builder-manager` SA token Secret `builder-manager-token` mounted at
  `/var/run/secrets/builder-manager`, plus `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and
  `GITHUB_APP_PRIVATE_KEY` from `company-ops-secrets`. Those env vars are inherited by every
  child of the gateway pod (hive, kubernetes, container-manager, builder) — a known blast-radius
  tradeoff, not an accident.
- The gateway must not upgrade itself: container-manager runs inside `mcp-gateway`, so
  `mcp-gateway` is excluded for this identity and still goes through the independent
  hive/engineering path with explicit user approval. `hermes-gateway` was removed from the
  exclusion on 2026-10-25 at the Board's request so the local session can roll out CEO fixes.

## Build (never from a mutable ref)
`builder_build_and_push(context_ref: https://github.com/BuildMy-house/company-os.git#<40-hex-sha>,
dockerfile_path: Dockerfile.mcp-gateway, image_repo: company-os-mcp-gateway, image_tag: <short-sha>)`
— `Dockerfile.mcp-gateway` copies the gateway scripts, including the new
`scripts/mcp-gateway-keys.js`. Then pin the returned `@sha256:` digest in
`k8s/base/mcp-gateway/mcp-gateway.yaml`.

## Migration steps (file tokens to issued keys)

1. Provision the DB role and schema, and create the secrets — admin, out of band:
   `psql -f sql/mcp_gateway_schema.sql`, then `scripts/provision-db-roles.sh --only
   mcp_gateway_keys`; create the Infisical secrets `GATEWAY_ADMIN_TOKEN` and
   `MCP_GATEWAY_DATABASE_URL`; rebuild and roll out the gateway image via the hive/engineering
   agents (never local kubectl/ssh).
2. Per identity, Nahar issues a key through `gateway-admin` and installs it as the client's env
   var — `LOCAL_CLAUDE_DEPLOY_TOKEN` for the deploy identity, `LOCAL_CLAUDE_TOKEN` /
   `LOCAL_OPENCODE_TOKEN` / `LOCAL_CODEX_TOKEN` for the others — then restarts the client. Once
   an identity has any issued key, its file token stops working automatically.
3. Flagged follow-up, **not done in this change**: after all identities are migrated, delete the
   legacy file-token path — the `legacyFileTokenAuth` function, the `tokenFile` fields in
   `policy.json` for non-admin clients, the per-client Infisical entries, and `loadPolicy`'s
   `tokenFile` handling for non-admin clients — so the single mechanism remains.

## Admin rollout steps
Run in this order (service `mcp-gateway` in namespace `company-ops`, container/service port
`8080`, NodePort `30430`):
1. Provision the key store: `psql -f sql/mcp_gateway_schema.sql`, then
   `scripts/provision-db-roles.sh --only mcp_gateway_keys` (role manifest
   `sql/roles.d/mcp_gateway_keys.yaml`; `MCP_GATEWAY_KEYS_PASSWORD` from Infisical `/hermes`).
2. Create the Infisical secrets at path `/mcp-gateway` in project
   `8806c2b0-73d2-4bea-8537-5b874c5ff592`, environment `dev`: `LOCAL_CLAUDE_TOKEN`,
   `LOCAL_OPENCODE_TOKEN`, `LOCAL_CODEX_TOKEN` (random, ≥32 bytes), `GATEWAY_ADMIN_TOKEN`, and
   `MCP_GATEWAY_DATABASE_URL`.
3. `sudo scripts/mcp-gateway-host-firewall.sh` on pop-os-1 (tailnet-only allowlist by
   default; `--remove` to undo).
4. `kubectl apply -k k8s/base/infisical-sync` then `kubectl apply -k k8s/base/mcp-gateway`
   (secret before the deployment that mounts it; the manifest includes the `mcp-gateway-deployer`
   ServiceAccount, Role and token Secret for the container-manager child).
5. Verify NetworkPolicy enforcement from a non-allowed pod — this must time out:
   `kubectl run -n company-ops --rm -it nptest --image=curlimages/curl --restart=Never -- curl -m 5 http://mcp-gateway:8080/`
   Then confirm the allowed tailnet path succeeds (e.g. from a tailnet device,
   `curl -m 5 http://<pop-os-1-tailnet-ip>:30430/` returns the gateway's 401, not a timeout).
   The cluster currently has zero NetworkPolicies, so enforcement by the k3s kube-router
   netpol controller is unproven until step 5 passes.

## Known gaps
- NetworkPolicy enforcement by this cluster is unverified until the rollout check above passes,
  and the host firewall script (the second layer) has not been run yet — ufw alone does not
  protect NodePorts (DNAT bypasses INPUT), so both layers are still outstanding.
- `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY` are pod-level env
  vars inherited by every child of the gateway pod: shared env blast radius, a known tradeoff.
- A Postgres outage fails closed: key authentication is denied, and a file-token identity whose
  migration state cannot be read is denied with 503 until the store returns.
- Removal of the legacy file-token path is pending (migration step 3 above).
- The kubernetes child's `redactTools` regexes are best-effort, not a secrets boundary; the
  ClusterRole grants no `secrets` or `configmaps` access.
