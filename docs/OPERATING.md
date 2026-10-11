# Operating company-os: one-page quick start

Namespace `company-ops` on k3s (single node today). Details live in the
linked docs; this page is the map.

## 1. Reach the cluster

- On the host: `kubectl` uses `~/.kube/config` (copy of
  `/etc/rancher/k3s/k3s.yaml`, mode 600).
- Remotely: over Tailscale. See `docs/TAILSCALE.md`; the only change for MCP
  servers and Claude Code is `kubectl config use-context company-os-tailnet`.
- Sanity: `kubectl config current-context && kubectl get nodes`.

## 2. Everyday commands

```bash
kubectl -n company-ops get pods,cronjob                    # what is running
kubectl -n company-ops logs deploy/hermes-gateway --tail=100
kubectl -n company-ops logs deploy/hive-coordinator --tail=100
kubectl -n company-ops logs deploy/pm-agent --tail=100
kubectl -n company-ops get events --sort-by=.lastTimestamp | tail -20
kubectl -n company-ops rollout status deploy/<name>
kubectl -n company-ops port-forward svc/hive-coordinator 4100:4100
kubectl kustomize k8s >/dev/null && echo manifests render    # before any manifest PR
```

Deployments: hermes-gateway, hive-coordinator, pm-agent, postgres, registry,
browser-adversary, engineering-agent, engineering-opencode,
engineering-opencode-direct. CronJobs: postgres-backup, hermes-data-backup,
dns-healthcheck. Prefer the Kubernetes MCP / container-manager MCP for
mutations (`docs/DEPLOY-ENGINEERING.md`); keep raw `kubectl apply/delete/exec`
for diagnosis and deliberate cutovers.

## 3. Secrets: Infisical only

Never create or edit secrets by hand, in `.env`, or in a manifest. Infisical is
the single source; the operator syncs it into `company-ops-secrets`
(`k8s/infisical-sync.yaml`), and `scripts/bootstrap-secrets.sh` builds a new
cluster's secrets. Add or rotate a key in Infisical, wait for the sync (about
60 s), then restart the consumer pod. Never print or paste values; list key
names only: `kubectl -n company-ops get secret company-ops-secrets -o json | jq -r '.data|keys[]'`.
Full map: `docs/SECRETS-INFISICAL.md`.

## 4. Add a DB role or an agent

- DB role: add `sql/roles.d/<role>.yaml` (format in `sql/roles.d/README.md`),
  add its password and DSN keys in Infisical `/hermes`, then
  `scripts/provision-db-roles.sh --dry-run` and `--only <role>` (reach the
  database via `kubectl port-forward svc/postgres 5432:5432`).
- Agent: it is a Hive member; follow `docs/AGENT-CONTRACT.md` and
  `k8s/agent-profile.example.yaml`, plus a Steward identity secret if it needs
  one (`docs/SECRETS-INFISICAL.md`).

## 5. Build and push images

Routine builds: the engineering manager's `builder_build_and_push` (BuildKit
Job, `docs/DEPLOY-ENGINEERING.md`). Host bootstrap or rebuild of everything:
`scripts/build-push-images.sh [--only NAME] --print-digests`. Image map and
the three manifests whose `company-os@sha256:` pins must move together:
`docs/IMAGES.md`. Pushing code or an image changes nothing running by itself.

## 6. Promote main to prod

`main` is integration; `prod` is what live images are built from. Merging
`main` into `prod` needs the user's explicit approval. Sequence: PR into
`main`, approval, merge `main` to `prod`, build from the immutable `prod`
commit, `container_test` the digest, `container_upgrade` the deployment,
verify readiness, `container_rollback` on failure
(`docs/DEPLOY-ENGINEERING.md`).

## 7. Backups and restore

Nightly to R2: postgres 03:00, hermes-data 03:30 (`docs/BACKUP.md`). Verify
with `scripts/verify-offsite-backup.sh`; restore with
`scripts/restore-postgres.sh` following `docs/RESTORE.md`. Moving hosts:
`docs/REMOTE-MIGRATION.md`.

## 8. Where telemetry lands

Axiom: `bmh-company` (agents and ops, every component) and
`buildmy-house-telemetry` (the app). Component table and ready-to-paste health
queries: `docs/TELEMETRY.md`. Fastest check: the "last event per service"
query there; a missing service means it is down or not yet rebuilt with the
emitter.
