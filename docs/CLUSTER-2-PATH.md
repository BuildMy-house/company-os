# Adding a second physical node / cluster later

This note records a future path — nothing here is built, applied, or scheduled.

## Where things stand today

One k3s node (`pop-os`), everything in the `company-ops` namespace. SmashLock
workers run in that same namespace (manifests under `k8s/projects/smashlock/`),
scoped per repo via `AGENT_REPOS`: a worker that serves repo X never bids on
work for repo Y. No second node, no second cluster yet.

Hive work is scoped by repo the same way. A submitter adds `repo` (a string,
e.g. `smashlock`) to the work metadata; it lands in the work payload as
`repo`. A worker declares `capabilities.repos` (an array of strings) when it
registers. The rules mirror the existing `requires_tools` /
`capabilities.tools` mechanism (see [docs/TICKET-STANDARD.md](TICKET-STANDARD.md),
"Requiring tools"):

- work with no `repo` is unscoped — any worker may bid;
- a worker with no or empty `repos` serves every repo;
- work scoped to repo R is only eligible for workers whose non-empty `repos`
  includes R, compared case-insensitively after trimming.

## Preferred path: a second k3s agent node

Add the machine to the existing cluster as an extra k3s **agent** node rather
than standing up a second cluster: same control plane, same
`hive-coordinator`, same Postgres.

Reasons:

- one hive coordinator — no second source of truth for work items and bids;
- no cross-cluster federation to build, run, and debug;
- the same in-cluster registry and the same Infisical secret sync keep working.

Workloads that must land on a specific machine are pinned with a node label
and a `nodeSelector`.

## Registry on a second node

Images are pulled from `localhost:30500` — `registry:2` in `company-ops`, plain
HTTP, no auth, exposed as NodePort `30500` (node-local). A remote node cannot
use `localhost:30500`: that is its own loopback. Its
`/etc/rancher/k3s/registries.yaml` needs a mirror entry pointing at the main
node's tailnet IP, with plain HTTP allowed:

```yaml
mirrors:
  "localhost:30500":
    endpoint:
      - "http://100.105.155.86:30500"
```

Manifest refs may keep the `localhost:30500/...` form only while every pulling
node has that mirror entry; otherwise they must name a resolvable registry host.

## Worker endpoints

A worker on a remote node registers endpoints the rest of the cluster can
actually reach: `A2A_ENDPOINT` and `HIVE_URL` pointing at the tailnet IP and a
NodePort. In-cluster DNS names such as `hive-coordinator:4100` or
`http://smashlock-opencode:8001` resolve only inside the cluster and will not
work from another machine.

The Hive API has no authentication. Exposing it on a NodePort makes it
reachable by anything that can route to the tailnet IP — keep it inside the
tailnet and do not expose it beyond.

## If a separate cluster is genuinely required

Only for real isolation. That path needs its own `hive-coordinator`, or a hive
reachable over the tailnet. Hive's repo-scoping rules are what keep work routed
correctly across the boundary: work carries its `repo`, workers declare
`capabilities.repos`, so a worker that serves repo X does not bid on repo Y's
work even when the two sit in different clusters.

Companion docs: [docs/REMOTE-MIGRATION.md](REMOTE-MIGRATION.md) (the runbook
that moved the current cluster to a new host) and
[k8s/projects/smashlock/README.md](../k8s/projects/smashlock/README.md).
