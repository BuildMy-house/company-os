# Browser adversary MCP

The browser adversary is a separate source-blind MCP service. Hermes calls
`browser_adversary.browser_adversary_test`; it receives only a live URL, an
optional task, and a bounded step budget. It has no repository checkout.

Task mode is the useful default. Exploration mode is deliberately bounded and
should be used to discover candidate flows that can later become repeatable
tasks.

## Build and deploy locally

From `company-os/`, with the registry already running:

```bash
docker build -f Dockerfile.browser-adversary -t localhost:30500/company-os-browser-adversary:latest .
docker push localhost:30500/company-os-browser-adversary:latest
kubectl apply -f k8s/browser-adversary.yaml
kubectl rollout status deployment/browser-adversary -n company-ops
kubectl rollout restart deployment/hermes-gateway -n company-ops
```

The Hermes config entry is already wired to
`http://browser-adversary:8000/mcp`.

## Authentication

For authenticated testing, create a Kubernetes Secret whose keys are browser
storage-state filenames, for example `staging.json`. Mount it as
`browser-adversary-auth`; Hermes passes `auth_profile: "staging"`. Passwords
are never sent in an MCP call or stored in reports.

The deployment defaults to the existing `TOKENROUTER_API_KEY` and the free
`z-ai/glm-5.3-free` model. Override `ADVERSARY_MODEL` only when a stronger
model is justified.
