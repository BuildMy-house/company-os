# Browser adversary MCP

The browser adversary is a separate source-blind MCP service. Hermes calls
`browser_adversary.browser_adversary_test`; it receives only a live URL, an
optional task, and a bounded step budget. It has no repository checkout.

Task mode is the useful default. Exploration mode is deliberately bounded and
should be used to discover candidate flows that can later become repeatable
tasks.

## Build and update

Build the candidate from a pinned `prod` commit with the engineering manager's
`builder_build_and_push` tool. Use `Dockerfile.browser-adversary`, the
`company-os-browser-adversary` image repository, and a unique tag such as
`browser-adversary-<short-sha>`. The build runs in an isolated BuildKit Job;
do not build this container locally or inside an agent pod.

Test the candidate with `container_test(image, template_deployment:
"browser-adversary")`, inspect health/logs, and remove the temporary test
Deployment. The test keeps the port/readiness settings but has no Secret
values or production auth volume. After Board approval, have an independent
manager call `container_upgrade` for `browser-adversary`, then confirm health.
If readiness fails, `container_upgrade` restores the previous image and waits
for its readiness. Hermes or the engineering manager can perform the rollout;
the browser-adversary container cannot replace its own running pod.

The Hermes config entry is already wired to
`http://browser-adversary:8000/mcp`.

## Authentication

For authenticated testing, create a Kubernetes Secret whose keys are browser
storage-state filenames, for example `staging.json`. Mount it as
`browser-adversary-auth`; Hermes passes `auth_profile: "staging"`. Passwords
are never sent in an MCP call or stored in reports.

The deployment uses the existing `ZAI_CODING_PLAN_API_KEY` with Z.AI's
`glm-5.3-flash` endpoint. Hermes does not run the adversary model.
