# MCP gateway

`scripts/mcp-gateway.js` hosts the repo's stdio MCP servers (hive member, container-manager,
kubernetes-mcp-server) as supervised children and exposes each as stateless Streamable HTTP at
`POST /<server>`. Plan: Steward `documents/plans/mcp-gateway` + `mcp-gateway-decisions`
(the addendum wins).

## Behaviour
- **Stateless**: no `Mcp-Session-Id`; `GET` returns 405. A gateway restart loses nothing.
- **Auth**: `Authorization: Bearer <token>`; one token per client identity, read from
  `/etc/mcp-gateway/tokens/<KEY>` per request, so Infisical rotation needs no restart.
  Failures are audited with an 8-char hash fingerprint only.
- **Policy** (`k8s/mcp-gateway.yaml` ConfigMap): default-deny. A tool is reachable only when
  listed in `clients.<id>.servers.<name>.tools`; enforced on `tools/list` *and* `tools/call`.
  Optional `argLimits` per tool; `agentId` gives each identity its own hive child
  (`HIVE_AGENT_ID`), with `HIVE_REGISTER=false` so gateway children never appear as agents.
- **Supervision**: a crashed child is respawned with 1s→30s backoff; in-flight calls get
  JSON-RPC `-32002` and clients retry.
- **Long polls** (`hive_next_work`, `hive_wait`) stream as SSE with 15s keepalives; at most
  4 concurrent per identity.
- **Audit**: one JSON line per call to stdout and, if `AXIOM_TOKEN` is set, Axiom. Argument
  *hashes* only. Tool output is redacted for tools in `redactTools`.

## Build (never from a mutable ref)
`builder_build_and_push(context_ref: https://github.com/BuildMy-house/company-os.git#<40-hex-sha>,
dockerfile_path: Dockerfile.mcp-gateway, image_repo: company-os-mcp-gateway, image_tag: <short-sha>)`,
then pin the returned `@sha256:` digest in `k8s/mcp-gateway.yaml`.

## One-time admin steps (not performed by the build ticket)
1. Add `LOCAL_CLAUDE_TOKEN` (random, ≥32 bytes) at Infisical `/infra/mcp-gateway`.
2. `kubectl apply -f k8s/infisical-sync.yaml -f k8s/mcp-gateway.yaml`.
3. `sudo scripts/mcp-gateway-host-firewall.sh` on pop-os-1 (`--remove` to undo).
4. Verify from an allowed and a disallowed source: 401 without token, 200 `tools/list` with it.

## Known gaps
- NetworkPolicy enforcement by this cluster is unverified; the host script is the second layer.
- ufw alone does not protect NodePorts (DNAT bypasses INPUT) — use the host script.
- `container-manager` is configured but granted to no client: a privileged identity with its own
  token and audit trail is a follow-up.
- The kubernetes child's `redactTools` regexes are best-effort, not a secrets boundary; the
  Role has no `secrets` access.
