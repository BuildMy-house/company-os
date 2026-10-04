# Dev-cluster gate flow

`buildmyhouse-dev` is an isolated, internal-only, disposable copy of the
`app` stack (app, mcp, luxcore) running in the `buildmyhouse-dev` namespace
on this cluster. It is **not** a persistent staging environment: its 3
Deployments get their images rewritten in place every time a new commit
lands on `app`'s `dev` branch, and its state (SQLite DB, assets) lives on an
`emptyDir`, so nothing in it survives a pod restart or should be treated as
durable. Its purpose is to give a human or an agent (e.g.
`browser-adversary`) something live to test against before a change merges
from `dev` to `main`/prod — it is **advisory pre-merge evidence, not a hard
CI gate**: `app`'s `dev`→`main` merge has no automated check wired to this
cluster, so a red gate does not block a merge by itself.

## Auto-deploy: CO-DEV3's poller

A CronJob, `buildmyhouse-dev-deployer` (namespace `company-ops`), polls
`BuildMy-house/app`'s `dev` branch every 3 minutes and rebuilds+redeploys
`buildmyhouse-dev` whenever the branch has moved. Check it's alive and see
its schedule:

```
kubectl get cronjob -n company-ops buildmyhouse-dev-deployer
```

Each run prints exactly one verdict line to its pod's logs — find the most
recent pod and read it:

```
kubectl get pods -n company-ops | grep buildmyhouse-dev-deployer
kubectl logs -n company-ops <pod-name>
```

The three possible verdicts, verbatim as the script prints them
(`company-os/scripts/dev-deploy-poller.js`):

- `[dev-deploy-poller] NOOP: BuildMy-house/app#dev unchanged at <sha>` — no
  new commit since the last successful deploy; nothing happened.
- `[dev-deploy-poller] PASS: deployed <sha> (<image>, <image>, <image>), /healthz 200` —
  new commit found, all 3 images rebuilt and pushed, all 3 Deployments
  rolled out, and a live `/healthz` check returned 200. The SHA is only
  recorded as deployed on this verdict.
- `[dev-deploy-poller] FAIL: <error>` — something broke partway through
  (build, rollout, or healthz); the last-deployed SHA in the ConfigMap is
  left unchanged, so the next poll (within 3 minutes) retries the same
  commit from scratch.

Real example, from the run that first proved this pipeline end-to-end
(2026-10-04, pod `buildmyhouse-dev-deployer-29852235-pgkwl`):

```
[dev-deploy-poller] PASS: deployed 914028a0b7619f3fb5022bfbf00b645de8d8fbb6 (localhost:30500/buildmyhouse-app:dev-914028a@sha256:99b2ab323acb3c01dd62c9c5466d2db0c606898e451195d8493d4455d94e9cfc, localhost:30500/buildmyhouse-mcp:dev-914028a@sha256:180b71fbf1386b6ef78fcb4c5095e235e26a411062de296ce706e0ea5075737d, localhost:30500/buildmyhouse-luxcore:dev-914028a@sha256:4916cb1e5a8df9e9d52f935b4c479d64f82e4b3d45305b5ea18e05500455d363), /healthz 200
```

To see which commit `buildmyhouse-dev` is currently running without waiting
for a poll:

```
kubectl get configmap buildmyhouse-dev-deploy-state -n buildmyhouse-dev -o jsonpath='{.data}'
```

Live output as of this writing:

```
{"lastDeployedSha":"914028a0b7619f3fb5022bfbf00b645de8d8fbb6","updatedAt":"2026-10-04T17:21:14.519Z"}
```

### Manual on-demand rebuild

Any agent with `kubectl` access to `company-ops` (every hive/opencode
worker already has this) can force an immediate run instead of waiting up
to 3 minutes for the next poll, by creating a one-off Job from the
CronJob's own template:

```
kubectl create job -n company-ops buildmyhouse-dev-deploy-manual-$(date +%s) \
  --from=cronjob/buildmyhouse-dev-deployer
```

This runs the identical script with the identical RBAC, so its verdict is
read the same way as above (`kubectl get pods`/`kubectl logs` for the job's
pod). Note: a Claude Code session's own Auto-Mode safety classifier treats
manually recreating this deploy workload (via this command, or any
`kubectl apply`/`kubectl run` that replicates it) as a sensitive
cluster-mutating action and may ask for confirmation — that is expected,
not a bug in the runbook.

## Testing against the dev app

### Human / interactive browsing

Port-forward the app Service to your machine and browse it directly:

```
kubectl port-forward -n buildmyhouse-dev svc/buildmyhouse-dev-app 3000:3000
```

then open `http://localhost:3000`. Verified live, 2026-10-04:

```
$ curl -s -w '\nHTTP_STATUS:%{http_code}\n' http://localhost:3000/healthz
{"ok":true}
HTTP_STATUS:200
```

### Adversarial / automated testing (CO-DEV4)

`browser-adversary` (a separate, source-blind MCP service running in
`company-ops`, see `docs/DEPLOY-BROWSER-ADVERSARY.md`) is already allow-
listed to test the dev app's in-cluster address. Its
`ADVERSARY_ALLOWED_ORIGINS` env var (`k8s/browser-adversary.yaml`) includes
both the prod origin and:

```
http://buildmyhouse-dev-app.buildmyhouse-dev.svc.cluster.local:3000
```

Confirmed live, 2026-10-04 — pod healthy and its MCP endpoint reachable
in-cluster:

```
$ kubectl get pods -n company-ops -l app=browser-adversary
NAME                                 READY   STATUS    RESTARTS      AGE
browser-adversary-6f8577498b-vzjcn   1/1     Running   1 (23m ago)   119m

$ kubectl exec -n company-ops deploy/hermes-gateway -- curl -s -o /dev/null -w 'HTTP_STATUS:%{http_code}\n' http://browser-adversary:8000/mcp
HTTP_STATUS:400   # expected for a bare GET against an MCP JSON-RPC endpoint — confirms reachability, not a real call
```

Hermes (or any MCP client already wired to `browser-adversary`) invokes it
with the dev origin as the `url`:

```json
{
  "tool": "browser_adversary_test",
  "arguments": {
    "url": "http://buildmyhouse-dev-app.buildmyhouse-dev.svc.cluster.local:3000",
    "task": "<what to test, e.g. 'sign up, create a plan, add a room, save'>",
    "mode": "task"
  }
}
```

The service is source-blind (no repo checkout, no code access) and bounded
(`max_steps`, default 10) — it only ever sees the live URL and whatever
`task` text you give it. Use `mode: "explore"` first on a feature you don't
already have a scripted flow for, to discover candidate flows worth turning
into a repeatable `task`.

## Reading the result

Neither the poller's PASS/FAIL nor a browser-adversary report blocks
anything automatically — `app`'s `dev`→`main` merge is not gated on either.
Treat a PASS + a clean adversary run as evidence worth citing in a PR
description before merging `dev` to `main`, and a FAIL/red adversary report
as a signal to go fix the regression and re-run, not as a hard stop
enforced by CI.
