# Dev image client telemetry (Axiom)

The in-cluster dev app image (`buildmyhouse-app`, built by
`scripts/dev-deploy-poller.js`) ships client telemetry to Axiom. Vite bakes
`VITE_AXIOM_TOKEN`, `VITE_AXIOM_DATASET` and `VITE_AXIOM_ENDPOINT` into the
public client bundle at build time, so they must be supplied as Docker build
args. The app `Dockerfile` declares them (`ARG`/`ENV`) only in the
frontend-builder stage, so they never reach the runtime image environment.

## Where the values come from

Infisical project `buildmyhouse-app` (id `58b43b81-effb-4392-a937-46f2448efb78`),
environment `dev`. Nothing is hardcoded in this repo. `scripts/dev-build-args.js`
logs in with Universal Auth, reads the three keys, and the poller passes them
to `buildAndPush` as `build_args` for the app image only (mcp and luxcore get
none). The run is fail-closed: a missing credential, missing key, empty value
or disallowed character aborts with an error naming the key (never the value)
before any build starts. Values are never logged.

Expected non-secret values: dataset `buildmy-house-telemetry`, endpoint
`https://eu-central-1.aws.edge.axiom.co`.

## How the poller gets Infisical credentials

`company-ops-secrets` does not carry `INFISICAL_*` keys. The CronJob instead
maps the existing `infisical-bootstrap-credentials` Secret (namespace
`company-ops`, keys `clientId` / `clientSecret`, the same machine identity the
Infisical operator uses) into `INFISICAL_UNIVERSAL_AUTH_CLIENT_ID` /
`INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET`, plus `INFISICAL_HOST_URL`
(`https://eu.infisical.com`). The identity must have read access to the
`buildmyhouse-app` project, `dev` environment (it already syncs it into
`buildmyhouse-dev-secrets`).

## Resuming the dev deployer

The CronJob is committed with `suspend: true` and is currently suspended.
To resume (deliberate, manual step):

1. Regenerate the scripts ConfigMap (now includes `dev-build-args.js`); see the
   command at the top of `k8s/projects/buildmyhouse-dev/buildmyhouse-dev-deployer-cronjob.yaml`.
2. `kubectl apply -f k8s/projects/buildmyhouse-dev/buildmyhouse-dev-deployer-cronjob.yaml` (keeps it suspended).
3. Smoke-test with a one-off Job: `kubectl create job -n company-ops
   buildmyhouse-dev-deploy-manual-$(date +%s) --from=cronjob/buildmyhouse-dev-deployer`
   (works while suspended).
4. When satisfied: `kubectl patch cronjob buildmyhouse-dev-deployer -n company-ops
   -p '{"spec":{"suspend":false}}'`.

The app Dockerfile change must be on the app `dev` branch before telemetry
actually appears in the built image.
