# Remote Migration — Cutover Runbook (company-ops on k3s)

Move the single-node k3s cluster (host `pop-os`, namespace `company-ops`) to a
remote machine. Old host stays **scaled to 0, not deleted**, until the new
host is verified end-to-end.

Companion docs: `docs/SECRETS-INFISICAL.md`
+ `scripts/bootstrap-secrets.sh` (secret bootstrap), `docs/IMAGES.md` +
`scripts/build-push-images.sh` (image build/push into the in-cluster registry),
`docs/BACKUP.md` (Postgres→R2 backups), `docs/RESTORE.md` +
`scripts/restore-postgres.sh` + `scripts/verify-offsite-backup.sh` (restore and
offsite verification).

Facts this runbook is built on (verified read-only, 2026-10-10):

- k3s `v1.36.4+k3s1`, kubectl `v1.36.4+k3s1`, single control-plane node,
  containerd `2.3.4-k3s1.36`; Docker `29.8.2` + buildx `v0.38.0` on the host.
- StorageClass `local-path` (rancher.io/local-path, Delete,
  WaitForFirstConsumer). PVCs persist across host reboots; the host is a
  desktop that reboots (mass restarts observed), which is the motivation to
  move.
- **No Ingress resources exist cluster-wide.** Nothing on the public internet
  points at this host. The only NodePort is the in-cluster registry
  (`5000:30500`, node-local). All cluster traffic is outbound.
- Live deployments: hermes-gateway, hive-coordinator, pm-agent, postgres,
  registry, browser-adversary, engineering-agent (live scaled to **3**; the
  manifest says 1 — see §6), engineering-opencode, engineering-opencode-direct,
  hermes-build-dispatcher (0), engineering-build-dispatcher (0),
  operator-namesp-controller-manager (helm, §5).
- CronJobs: `postgres-backup` (0 3 * * *, active), `dns-healthcheck` (*/15,
  active), `buildmyhouse-dev-deployer` (*/3, **suspended**, §7).

---

## 1. Prerequisites on the new host

| Requirement | Target | Check |
|---|---|---|
| k3s | `v1.36.4+k3s1` (exact version parity) | `sudo k3s --version` |
| kubectl | `v1.36.4+k3s1` (bundled with k3s) | `kubectl version --client` |
| kustomize | v5.x (bundled with kubectl) | `kubectl kustomize --help` |
| Docker + buildx | ≥29.x / buildx ≥0.38 (used by `scripts/build-push-images.sh` and BuildKit Jobs) | `docker version && docker buildx version` |
| Ports | 6443 (API server), 10250 (kubelet), 30500 NodePort (registry); free of firewalls **from the operator's workstation** | `nc -vz <new-host> 6443` etc. after install |
| Storage | k3s default `local-path` StorageClass present, disk sized ≥ 32 GiB for PVCs (hermes-data 5Gi + postgres-data 5Gi + registry-data 20Gi) + headroom | `kubectl get storageclass` |
| Infisical | One manual credential: `infisical-bootstrap-credentials` Secret (keys `clientId`/`clientSecret`) from the Infisical project `8806c2b0-...` universal-auth identity (see `docs/SECRETS-INFISICAL.md`) | only manual secret — everything else syncs |
| DNS / inbound | **None required.** No Ingress, no public listeners. Discord (outbound websocket), Steward, Infisical, Axiom, R2, LLM providers are all outbound HTTPS from the new host | verify outbound 443 works |
| Operator workstation | kubeconfig for the new cluster + updated `~/.mcp.json` (see §4) | `kubectl get nodes` against new host |

Install k3s pinned to the current version:

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.36.4+k3s1 sh -s - --write-kubeconfig-mode 600
sudo k3s --version   # must print v1.36.4+k3s1
```

Copy `/etc/rancher/k3s/k3s.yaml` off the new host; change `server: https://127.0.0.1:6443`
to `https://<new-host-ip>:6443` in your workstation kubeconfig.

Service CIDR defaults to `10.43.0.0/16` — identical to the old cluster, so the
hardcoded `KUBERNETES_SERVICE_HOST=10.43.0.1` in
`k8s/engineering*.yaml` / `hermes/config.yaml` /
`scripts/generate-agent-mcp-config.js` remains valid. If you ever deviate from
the default CIDR, grep for `10.43.0.1` and change every occurrence first.

## 2. Cutover runbook (execute in order)

### Step 0 — Pre-flight on the old cluster (read-only)

```bash
kubectl get deploy,cronjob,pvc -n company-ops
kubectl get pods -n company-ops -o wide
kubectl get secret company-ops-secrets -n company-ops -o json | jq -r '.data|keys[]'   # key names only, never decode
```

Expect: all core deployments Ready, 3 PVCs Bound (hermes-data, postgres-data,
registry-data), 44 secret keys present. Run one fresh backup to prove the
pipeline works before you depend on it:

```bash
kubectl create job --from=cronjob/postgres-backup postgres-backup-preflight -n company-ops
kubectl logs -n company-ops job/postgres-backup-preflight    # expect R2 upload line
kubectl delete job postgres-backup-preflight -n company-ops
```

### Step 1 — Quiesce writers (scale to 0)

Freeze anything that writes to Postgres, `hermes-data`, git repos, or Discord:

```bash
kubectl scale deploy -n company-ops --replicas=0 \
  hermes-gateway hive-coordinator pm-agent browser-adversary \
  engineering-agent engineering-opencode engineering-opencode-direct \
  hermes-build-dispatcher engineering-build-dispatcher
kubectl get deploy -n company-ops   # only postgres, registry, operator remain
```

`hermes-gateway` must be at 0 before the new host's gateway starts (one Discord
gateway session per bot token — two live instances double-handle messages).
Postgres and registry stay up until Step 2 finishes.

### Step 2 — Final backups

```bash
# Postgres logical dump (company + observer schemas) straight to R2
kubectl exec -n company-ops deploy/postgres -- \
  pg_dump -U postgres -d homely_company --format=plain --file=/tmp/final-dump.sql
kubectl exec -n company-ops deploy/postgres -- gzip -c /tmp/final-dump.sql > /tmp/final-dump.sql.gz
# (or trigger the R2 pipeline: kubectl create job --from=cronjob/postgres-backup postgres-final-cutover -n company-ops)

# hermes-data PVC tarball (node filesystem — local-path)
D=$(ls -d /var/lib/rancher/k3s/storage/*_company-ops_hermes-data_* | head -1)
sudo tar -czf /tmp/hermes-data-final.tar.gz -C "$D" .
sha256sum /tmp/hermes-data-final.tar.gz
```

Carry both artifacts to the new host (scp). Contents of `hermes-data` include
`config.yaml`, `memories/`, `gateway_state.json`, `auth.json`, `cron/`,
`channel_directory.json`, `discord_threads.json` — the gateway's session
state; the new cluster must start from this snapshot, not from the repo
defaults.

Registry contents are **not** backed up — images are rebuilt on the new host
(Step 4).

### Step 3 — Provision the new cluster

```bash
# 1. k3s install (§1)
# 2. Secrets: the ONLY manual step — apply the bootstrap credential
kubectl create namespace company-ops
kubectl -n company-ops create secret generic infisical-bootstrap-credentials \
  --from-literal=clientId='<from Infisical>' --from-literal=clientSecret='<from Infisical>'
# 3. Infisical secrets operator (helm — same flags as documented in k8s/infisical-sync.yaml)
helm repo add infisical-helm-charts https://dl.cloudsmith.io/public/infisical/helm-charts/helm.charts/
helm repo update
helm install operator-namespaced infisical-helm-charts/secrets-operator \
  -n company-ops --set scopedNamespaces=company-ops --set scopedRBAC=true
# 4. Sync CRs → operator populates company-ops-secrets + company-ops-steward-*
kubectl apply -f k8s/infisical-sync.yaml
kubectl get secret company-ops-secrets -n company-ops -o json | jq -r '.data|keys[]' | wc -l   # expect 44
# 5. Core manifests
kubectl apply -k k8s/          # includes pm-agent; waits not required; pods will CrashLoop until secrets/images exist — expected (pm-agent also needs PM_DATABASE_URL, see NAHAR-TODO.md Group L)
```

### Step 4 — Build and push images into the new in-cluster registry

Registry must be Ready first (`kubectl rollout status deploy/registry -n company-ops`).

```bash
scripts/build-push-images.sh          # rebuilds company-os, company-os-hive, company-os-engineering,
                                      # company-os-pm-agent, company-os-browser-adversary,
                                      # company-os:dns-remediation from the prod branch into localhost:30500
docker images --digests | grep localhost:30500   # record new digests
```

The manifest image pins (`k8s/*.yaml`, e.g. `company-os@sha256:3362faca…`)
reference old digests (company-ops.yaml, postgres-backup.yaml and hermes-data-backup.yaml must move together; see docs/IMAGES.md). After rebuilding, update the pins to the new digests in
one commit on prod, then re-apply: `kubectl apply -k k8s/`. (Alternative —
restore the old registry PVC — is deliberately not used: images are
reproducible from git and the 20 Gi PVC transfer is not worth it.)

### Step 5 — Restore state

```bash
# 5a. Verify the offsite backup before touching the live DB
scripts/verify-offsite-backup.sh    # downloads newest R2 dump, checksum + gunzip -t + psql parse (see docs/BACKUP.md)

# 5b. Restore Postgres from the R2 dump into a SCRATCH database first
kubectl exec -n company-ops deploy/postgres -- createdb -U postgres homely_company_verify
scripts/restore-postgres.sh homely_company_verify
kubectl exec -n company-ops deploy/postgres -- psql -U postgres -d homely_company_verify -c '\dt'   # tables present
kubectl exec -n company-ops deploy/postgres -- psql -U postgres -d homely_company_verify -c 'select count(*) from <largest table>'
kubectl exec -n company-ops deploy/postgres -- dropdb -U postgres homely_company_verify

# 5c. Restore live DB from the same dump
scripts/restore-postgres.sh homely_company

# 5d. Restore hermes-data
D=$(ls -d /var/lib/rancher/k3s/storage/*_company-ops_hermes-data_* | head -1)
sudo tar -xzf /tmp/hermes-data-final.tar.gz -C "$D"
```

### Step 6 — Start order

Strictly in this order; wait for each `rollout status` before the next:

1. `postgres` (already up) — `kubectl rollout status deploy/postgres -n company-ops`
2. `registry` — already up in Step 4
3. `hive-coordinator` — coordination plane agents register against
4. `hermes-gateway` — **only now** does the Discord bot token come online (old host still at 0)
5. `pm-agent`
6. `browser-adversary`
7. `engineering-agent`, `engineering-opencode`, `engineering-opencode-direct`
8. CronJobs: `dns-healthcheck`, `postgres-backup` resume on their own schedules
9. `buildmyhouse-dev-deployer` — **stays suspended** (§7); resume only after cutover verified

```bash
kubectl rollout status deploy/hive-coordinator -n company-ops
kubectl rollout status deploy/hermes-gateway  -n company-ops
kubectl rollout status deploy/pm-agent        -n company-ops
```

### Step 7 — Smoke tests

```bash
# hermes-gateway: its probes are exec pgrep; assert via pod status + API server
kubectl get pods -n company-ops -l app=hermes-gateway          # Running, 0 restarts
kubectl port-forward -n company-ops svc/hermes-gateway 8642:8642 &
APIKEY=$(kubectl -n company-ops get secret company-ops-secrets -o jsonpath='{.data.API_SERVER_KEY}' | base64 -d)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $APIKEY" \
  http://127.0.0.1:8642/v1/models                              # expect 200
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $APIKEY" -H 'Content-Type: application/json' \
  -d '{"model":"meituan/longcat-2.0:free","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' \
  http://127.0.0.1:8642/v1/chat/completions                    # expect 200
kill %1

# hive-coordinator: agent-card endpoint (same one its probes use)
kubectl port-forward -n company-ops svc/hive-coordinator 4100:4100 &
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4100/.well-known/agent-card.json   # expect 200
kill %1

# pm-agent: /healthz probe + startup log line ("pm-agent listening on port 4200", pm-agent/agent.py)
kubectl get pods -n company-ops -l app=pm-agent
kubectl logs deploy/pm-agent -n company-ops | grep 'listening on port'
kubectl port-forward -n company-ops svc/pm-agent 4200:4200 &
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4200/healthz   # expect 200
kill %1

# hermes-gateway Discord session: exactly one gateway websocket, from the NEW cluster
kubectl logs deploy/hermes-gateway -n company-ops | grep -i 'gateway'
```

### Step 8 — Pass/fail criteria

| Check | Pass | Fail → action |
|---|---|---|
| `kubectl get deploy -n company-ops` (core 9) | all AVAILABLE | rollback §2.9 |
| Postgres row counts vs old host (spot-check 2–3 tables) | equal | re-run restore §2.5 |
| hermes API `GET /v1/models` | HTTP 200 | check envFrom secret keys; rollback |
| hermes API `POST /v1/chat/completions` | HTTP 200 | check LLM provider keys via Infisical sync |
| hive `/.well-known/agent-card.json` | HTTP 200 | check logs for registration retry |
| pm-agent `/healthz` + log line | HTTP 200 / "listening on port 4200" | check PM_DATABASE_URL secret |
| Discord: hermes replies to a test message, exactly once | one reply | if two/duplicate or none → exactly one cluster at 1 replica; old must be 0 |
| `dns-healthcheck` next scheduled run (*/15) | completes, no coredns restart | check its pod logs; new host resolver differs |
| `postgres-backup` at 03:00 | R2 upload logged | check R2_* secret keys |
| Infisical sync refresh (60s) | secret `resourceVersion` bumps on change | §5 |

Do not proceed to §3–§4 repointing or resume `buildmyhouse-dev-deployer` until
every row passes. Old cluster stays scaled to 0 (not deleted) until then.

### Step 9 — Rollback to the old cluster

The old host is untouched: its PVCs (hermes-data, postgres-data) still hold the
pre-cutover state and all deployments are at 0.

1. **Data first:** any messages/state created on the NEW host since cutover is
   lost on rollback. If that window matters, redo Step 2 in reverse: dump from
   the new host and restore into the old cluster first.
2. Scale the new host to 0: same `kubectl scale` as Step 1, against the new
   kubeconfig.
3. Scale the old host back up: same `kubectl scale --replicas=1 …` against the
   old kubeconfig (reverse start order: engineering → browser-adversary →
   pm-agent → hermes-gateway → hive).
4. Repoint the operator workstation back (§4: kubeconfig context + `~/.mcp.json`
   unchanged if exec-based MCPs use a context name — switch the context, don't
   edit every entry).
5. Delete nothing on either side until the migration is either re-attempted or
   formally abandoned.

## 3. MCP server repointing

Grepped for `localhost:30500`, `pop-os`, `192.168.8.3`, `127.0.0.1`,
`svc.cluster.local`, `10.43.0.1` across `k8s/`, `scripts/`, `hermes/`,
`mcp-workers.example.json`, `k8s/engineering*.yaml`. Findings split by where
the MCP client runs:

### A. In-cluster clients (hermes-gateway pod, agent pods) — NOTHING changes

Cluster-internal DNS names move with the cluster:

- `hermes/config.yaml`: `browser-adversary:8000`, `hermes-build-dispatcher:8000`
  (service names) — unchanged.
- `scripts/registry-manager-mcp.js` (`REGISTRY_HOST` default
  `registry.company-ops.svc.cluster.local`), `scripts/hermes-messenger-mcp.js`
  (`hermes-gateway.company-ops.svc.cluster.local:8642`),
  `scripts/container-manager-mcp.js` / `scripts/builder-manager-mcp.js`
  (in-cluster registry address) — unchanged.
- `scripts/generate-agent-mcp-config.js` generated configs for in-pod agents —
  unchanged, **except** `KUBERNETES_SERVICE_HOST=10.43.0.1` (hardcoded in
  `k8s/engineering.yaml`, `k8s/engineering-opencode*.yaml`,
  `hermes/config.yaml` `k8s_deployment` server, and as the script's default):
  valid only because the default k3s service CIDR is retained on the new host.

### B. Operator-workstation clients (`~/.mcp.json`, gitignored) — kubeconfig is the switch

Three entries run commands on the operator host that talk to the cluster via
`kubectl exec`:

- `hive`, `companyos-engineering-builder`, `companyos-container-manager` — all
  `kubectl --kubeconfig ~/.kube/config exec -i -n company-ops deployment/…`.

They follow the kubeconfig's **current context**. Repointing = add the new
host's kubeconfig and switch context (`kubectl config use-context <new>`). No
`.mcp.json` edits needed. Confirm `kubectl config current-context` before any
post-cutover workspace work.

Other operator-host entries (steward sse, opencode, infisical, axiom, neon,
ai-cli, kubernetes MCP, and the host-local `buildmyhouse` run.sh) never touch
this cluster — unchanged.

### C. Image references — digests change, address doesn't

`localhost:30500/...` in `k8s/*.yaml` is resolved by the **node's** kubelet
(containerd on the node), which is why the NodePort exists (in-cluster registry
DNS is not resolvable from the node). On the new host the same
`localhost:30500` address works after `scripts/build-push-images.sh` runs —
only the digests change (Step 4 pin update).

### D. Compose-era leftovers — no action

`mcp-workers.example.json` (stdio workers via `python -m company_ops worker`)
is not referenced by any script or manifest; ignore during migration.

## 4. Inbound, DNS, Discord

- **Inbound: nothing.** No Ingress resources, no LoadBalancers, no public DNS
  A records point at `pop-os`. `app.buildmy.house` and its TLS are hosted
  outside this cluster (separate platform). The registry NodePort 30500 binds
  only on the node LAN — do not expose it on the new host beyond the operator
  LAN. Migration therefore requires **no DNS changes and no TLS/cert changes**.
- **Discord: outbound only.** `hermes-gateway` opens a gateway websocket to
  Discord; no webhook receiver exists. The hard rule: **exactly one live
  gateway session per bot token** — old host scaled to 0 (Step 1) before the
  new gateway starts (Step 6.4). Two sessions double-handle every message.
  Session continuity (channel directory, threads, cron jobs) rides in
  `hermes-data` (`gateway_state.json`, `auth.json`, `channel_directory.json`,
  `cron/`) — restored in Step 5d.
- **`dns-healthcheck` CronJob** (*/15): reads CoreDNS health from inside the
  cluster, detects the stale-upstream failure mode seen 2026-09-28, restarts
  the `coredns` deployment (patch permission restricted by resourceName) and
  opens a Steward task. On the new host: first run within 15 min of Step 6;
  verify its first execution succeeds — the new host's `/etc/resolv.conf` and
  upstream resolvers differ, and CoreDNS stale-upstream behavior was exactly
  what this watches for. If it fires immediately and repeatedly on the new
  host, check the host's upstream DNS before assuming a cluster problem.
- The operator's LAN reachability changes with the new host IP: kubeconfig
  server URL (§1) and any shell aliases/port-forward habits referencing
  `192.168.8.3`.

## 5. operator-namesp-controller: 32 restarts — cause and migration decision

`deployment/operator-namesp-controller-manager` (company-ops, helm release
`operator-namespaced`, chart `secrets-operator-v0.11.9`, image
`infisical/kubernetes-operator:v0.11.9`) shows **32 restarts over ~6 days**.

Evidence (`kubectl logs --previous`): reconciliation loops run cleanly (60s
cycles, all syncs succeed — company-ops-sync 43 secrets, buildmyhouse-dev-sync
20, steward secrets 1 each) until:

```
leaderelection.go:436  Failed to update lock optimistically ... context deadline exceeded
leaderelection.go:429  error retrieving resource lock company-ops/cf2b8c44.infisical.com ...
E setup  leader election lost
```

→ process exits 1, kubelet restarts it, it re-syncs fine.

**Probable cause:** the helm install runs with `--leader-elect` (default) on a
**single-replica** controller against a **single-node k3s** API server on a
desktop host. When the host or API server stalls (reboots, desktop load,
concurrent BuildKit jobs), the lease renewal deadline is missed and the
process kills itself. It is cosmetic — no sync data loss (creationPolicy Owner,
re-sync on start) — but it also documents the flakiness that motivates this
migration.

**Decision: migrate it.** It owns the Infisical→k8s secret sync that every
deployment's `envFrom` depends on. Install order on the new host is Step 3
(helm before `infisical-sync.yaml` CRs). Optional hardening, not required for
cutover: raise its 500m CPU limit, or pass
`--set leaderElect=false` for a guaranteed-single-node install.

## 6. Engineering-agent replicas

Decision: the manifest value is authoritative. `k8s/engineering.yaml` declares
`replicas: 1`, and `kubectl apply -k k8s/` on the new host yields 1 replica
regardless of any live `kubectl scale` on the old cluster. To run more, change
`replicas:` in the manifest and commit it.

## 7. buildmyhouse-dev-deployer CronJob (suspended)

Live state: `SUSPENDED=True` (`spec.suspend: true` was set on the cluster; the
manifest has no `suspend:` field — apply-after-cutover would **resume** it by
accident). It polls every 3 min and rebuilds the disposable `buildmyhouse-dev`
namespace from the BuildMy-house/app dev branch.

Options:

| Option | Effect | Verdict |
|---|---|---|
| A. Don't migrate | dev-gate stays dark until explicitly reinstated | loses pre-merge testing during migration window |
| B. Migrate + resume immediately | poller fires */3 during cutover; hits a half-built cluster (needs buildmyhouse-dev-secrets via operator, engineering agent for builds) | noisy failures during a fragile window |
| C. **Migrate suspended; resume after cutover verified** | zero interference; one manual trigger resumes testing | **recommended** |

Implementation of C: on the new host apply `k8s/buildmyhouse-dev-*` with
`suspend: true` added to the CronJob manifest (or `kubectl patch cronjob
buildmyhouse-dev-deployer -n company-ops --type merge -p '{"spec":{"suspend":true}}'`
immediately after apply), then after all §2.8 rows pass, resume with
`kubectl patch cronjob buildmyhouse-dev-deployer -n company-ops --type merge -p
'{"spec":{"suspend":false}}'` (or one-shot `kubectl create job --from=cronjob/buildmyhouse-dev-deployer
buildmyhouse-dev-resume-$(date +%s) -n company-ops`). Regenerate its scripts
ConfigMap from the current repo per the manifest header
(`kubectl create configmap --from-file=scripts/…`). `buildmyhouse-dev` state
itself is emptyDir and disposable — nothing to back up; the poller rebuilds it.
