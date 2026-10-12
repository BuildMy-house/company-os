# MCP gateway

`scripts/mcp-gateway.js` hosts the repo's stdio MCP servers (hive member, container-manager,
kubernetes-mcp-server) as supervised children and exposes each as stateless Streamable HTTP at
`POST /<server>`. Plan: Steward `documents/plans/mcp-gateway` + `mcp-gateway-decisions`
(the addendum wins).

## Behaviour
- **Stateless**: no `Mcp-Session-Id`; `GET` returns 405. A gateway restart loses nothing.
- **Auth**: `Authorization: Bearer <token>`; one token per client identity — local-claude,
  local-opencode, local-codex (Secret keys `LOCAL_CLAUDE_TOKEN`, `LOCAL_OPENCODE_TOKEN`,
  `LOCAL_CODEX_TOKEN`, synced from Infisical `/mcp-gateway`) — read from
  `/etc/mcp-gateway/tokens/<KEY>` per request, so Infisical rotation needs no restart.
  Failures are audited with an 8-char hash fingerprint only.
- **Policy** (`k8s/mcp-gateway.yaml` ConfigMap): default-deny. A tool is reachable only when
  listed in `clients.<id>.servers.<name>.tools`; enforced on `tools/list` *and* `tools/call`.
  Hive identities get read tools (`hive_agents`, `hive_available_work`, `hive_diagnostics`,
  `hive_events`, `hive_status`, `hive_wait`, `hive_work_bids`) plus `hive_submit` only — no
  `hive_bid`/`hive_complete`/`hive_heartbeat`/`hive_next_work`. Optional `argLimits` per tool;
  `agentId` gives each identity its own hive child (`HIVE_AGENT_ID`), with `HIVE_REGISTER=false`
  so gateway children never appear as agents.
- **Supervision**: a crashed child is respawned with 1s→30s backoff; in-flight calls get
  JSON-RPC `-32002` and clients retry.
- **Long polls** (`hive_wait`) stream as SSE with 15s keepalives; at most
  4 concurrent per identity.
- **Audit**: one JSON line per call to stdout and, if `AXIOM_TOKEN` is set, Axiom. Argument
  *hashes* only. Tool output is redacted for tools in `redactTools`.

## Build (never from a mutable ref)
`builder_build_and_push(context_ref: https://github.com/BuildMy-house/company-os.git#<40-hex-sha>,
dockerfile_path: Dockerfile.mcp-gateway, image_repo: company-os-mcp-gateway, image_tag: <short-sha>)`,
then pin the returned `@sha256:` digest in `k8s/mcp-gateway.yaml`.

## Admin rollout steps
Run in this order (service `mcp-gateway` in namespace `company-ops`, container/service port
`8080`, NodePort `30430`):
1. Create the three Infisical tokens — `LOCAL_CLAUDE_TOKEN`, `LOCAL_OPENCODE_TOKEN`,
   `LOCAL_CODEX_TOKEN` (random, ≥32 bytes) — at path `/mcp-gateway` in project
   `8806c2b0-73d2-4bea-8537-5b874c5ff592`, environment `dev`.
2. `sudo scripts/mcp-gateway-host-firewall.sh` on pop-os-1 (tailnet-only allowlist by
   default; `--remove` to undo).
3. `kubectl apply -f k8s/infisical-sync.yaml` then `kubectl apply -f k8s/mcp-gateway.yaml`
   (secret before the deployment that mounts it).
4. Verify NetworkPolicy enforcement from a non-allowed pod — this must time out:
   `kubectl run -n company-ops --rm -it nptest --image=curlimages/curl --restart=Never -- curl -m 5 http://mcp-gateway:8080/`
   Then confirm the allowed tailnet path succeeds (e.g. from a tailnet device,
   `curl -m 5 http://<pop-os-1-tailnet-ip>:30430/` returns the gateway's 401, not a timeout).
   The cluster currently has zero NetworkPolicies, so enforcement by the k3s kube-router
   netpol controller is unproven until step 4 passes.

## Known gaps
- NetworkPolicy enforcement by this cluster is unverified until the rollout step above
  passes; the host script is the second layer.
- ufw alone does not protect NodePorts (DNAT bypasses INPUT) — use the host script.
- `container-manager` is configured but granted to no client: a privileged identity with its own
  token and audit trail is a follow-up.
- The kubernetes child's `redactTools` regexes are best-effort, not a secrets boundary; the
  ClusterRole grants no `secrets` or `configmaps` access.
