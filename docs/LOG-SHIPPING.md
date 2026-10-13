# Container log shipping to Axiom

`k8s/base/log-shipper` runs one Vector pod per node that tails every container's
stdout/stderr in every namespace and ships it to Axiom dataset `bmh-container-logs`,
tagged `namespace`, `pod`, `app`, `container`, `image`, `node`. Applications need no
Axiom credentials and no logging code; pm-agent stays credential-free.

- Only the log-shipper pod holds the token: ingest-only `AXIOM_LOG_TOKEN`, synced from
  Infisical folder `/log-shipper` into Secret `log-shipper-secrets` (no value in git).
- Known secret shapes (bearer/token/api key/password/`xaat-`/`ghp_` etc., Postgres DSNs)
  are redacted before shipping. Best effort: do not rely on it to make logging secrets safe.
- Structured events apps already send to `bmh-company` are unchanged.
- Disk buffer 1 GiB, `drop_newest` when full, so an Axiom outage cannot fill the node.

## Rollout (human-gated)
1. Create an ingest-only Axiom token for dataset `bmh-container-logs` (create the dataset first).
2. Store it in Infisical `/log-shipper` as `AXIOM_LOG_TOKEN`.
3. Apply the overlay. The `ClusterRole`/`ClusterRoleBinding` are cluster-scoped, so the applier needs cluster-admin; the in-cluster deploy identity may not.
4. Verify in Axiom: `['bmh-container-logs'] | summarize count() by app` shows every Deployment.
