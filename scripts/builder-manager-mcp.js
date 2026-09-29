#!/usr/bin/env node

// Ephemeral, rootless-BuildKit-based build+push, without ever giving the
// calling pod (hermes-gateway or engineering-agent) a docker socket or
// persistent build credentials. Instead of building locally, this creates a
// short-lived Kubernetes Job in company-ops running the rootless
// `moby/buildkit` image via `buildctl-daemonless.sh` (starts an unprivileged
// buildkitd and runs a single build in one process, no persistent daemon),
// which builds a git-context Dockerfile and pushes the result straight to
// the in-cluster registry (registry.company-ops.svc.cluster.local:5000).
//
// Replaces the previous Kaniko-based design (gcr.io/kaniko-project/executor)
// for two reasons, both verified live in this cluster on 2026-09-29:
//   1. Kaniko accepts exactly one `--context` and has no equivalent for a
//      second, separately-authenticated named context, which is what
//      `Dockerfile.engineering`'s `COPY --from=shared ...` step needs (see
//      docs/DEPLOY-ENGINEERING.md's former "Known gap" section, now
//      resolved). BuildKit's dockerfile.v0 frontend natively supports named
//      additional contexts via `--opt context:<name>=<url>` — this is the
//      exact same primitive `docker buildx build --build-context` uses
//      under the hood, so it needs no buildx, just plain BuildKit.
//   2. Rootless BuildKit runs safely inside a k3s pod with no privileged
//      workaround: `securityContext.seccompProfile: Unconfined` (needed for
//      the `unshare` syscalls rootlesskit uses) plus the
//      `container.apparmor.security.beta.kubernetes.io/<container>: unconfined`
//      pod annotation, a non-root `runAsUser`, and
//      `BUILDKITD_FLAGS=--oci-worker-no-process-sandbox` (works around a
//      "mount proc: operation not permitted" failure on RUN steps when
//      nested under the pod's own mount-namespace restrictions) are
//      sufficient — no `privileged: true`, no added Linux capabilities, no
//      `/dev/fuse` hostPath, no experimental `hostUsers` pod-level user
//      namespaces (`hostUsers: false` was tried and failed on this node's
//      newuidmap/subuid setup — not needed once the above three settings are
//      in place). All four confirmed with a real end-to-end build+push,
//      including a full `Dockerfile.engineering` build exercising the named
//      `shared` context against the private `workspace` repo.
//
// Authenticates to the Kubernetes API as the dedicated "builder-manager"
// ServiceAccount (k8s/builder-rbac.yaml) via a bound token mounted at
// /var/run/secrets/builder-manager/token — NOT the pod's default
// /var/run/secrets/kubernetes.io/serviceaccount/token, which belongs to
// the shared "company-ops" SA used by container-manager/k8s_deployment and
// can patch/delete Deployments. builder-manager's Role can
// create/get/list/watch/delete Jobs, read their pods' logs, and
// create/delete Secrets (the last one added for this change — see
// "Git credential handling" below) — nothing else, in particular no
// `deployments.apps` verbs and no get/list/watch on Secrets (it cannot read
// any Secret already in the namespace). The BuildKit Job pod itself runs
// with no ServiceAccount permissions at all (automountServiceAccountToken:
// false): it never touches the Kubernetes API, only the git remote(s) and
// the registry over plain HTTP/HTTPS.
//
// Git credential handling: `context_ref`/`contexts` values must be bare,
// credential-free `https://` URLs pinned to a full 40-hex-character commit
// SHA (immutable ref requirement — enforced here, not just by convention).
// For the private `BuildMy-house/*` repos specifically, this script mints a
// short-lived GitHub App installation token itself (reusing
// `github-app-token.js`, the same mechanism `sync-repo.sh` already uses) and
// delivers it to the Job pod via a dedicated, per-build Kubernetes Secret
// referenced with `secretKeyRef` — the token never appears in the Job
// manifest's own text (only a reference to the Secret's name/key does), so
// it is not visible to anything that can merely `get`/`list` Jobs in this
// namespace. The Secret is deleted alongside the Job on every exit path.
// Residual, documented limitation: BuildKit's own git-source log line
// echoes the resolved (credentialed) URL into the *pod's* stdout logs —
// this script masks the token in everything it returns to its caller, but
// the raw pod log is still visible for the Job's short lifetime to anything
// with `pods/log` read in company-ops. Bounded by the Job's
// `ttlSecondsAfterFinished` (10 min) and the token's own ~1h GitHub-imposed
// expiry either way.
import https from "node:https";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

const K8S_HOST = process.env.KUBERNETES_SERVICE_HOST || "kubernetes.default.svc";
const K8S_PORT = process.env.KUBERNETES_SERVICE_PORT || "443";
const NAMESPACE = "company-ops";
// NOT overridable via process.env.REGISTRY_HOST/REGISTRY_PORT — verified
// live 2026-09-29 that Kubernetes auto-injects Docker-links-style discovery
// env vars for any Service named "registry" in this namespace (k8s/registry.yaml),
// including one literally named `REGISTRY_PORT` set to `tcp://<clusterIP>:5000`.
// Reading that as an override silently replaced the intended "5000" default,
// producing a malformed `registry.company-ops.svc.cluster.local:tcp://10.43.x.x:5000/...`
// push destination and failing every build at the final push step with
// "invalid reference format". These two are fixed cluster topology, not
// meant to vary per invocation, so there is no legitimate override case to
// preserve.
const REGISTRY_HOST = "registry.company-ops.svc.cluster.local";
const REGISTRY_PORT = "5000";
// Pinned by digest (not the floating `master-rootless` tag) for the build
// tool's own reproducibility — resolved from and verified against
// `moby/buildkit:master-rootless` live in this cluster on 2026-09-29.
const BUILDKIT_IMAGE = process.env.BUILDKIT_IMAGE
  || "docker.io/moby/buildkit@sha256:c334b42fdcd64e2fa70e06e18a9c17c7fa73d9ad0c22cef50b30307aa049dc06";

// Deliberately NOT the pod's default in-cluster SA path — see header.
const TOKEN_PATH = process.env.BUILDER_MANAGER_TOKEN_PATH || "/var/run/secrets/builder-manager/token";
// The CA is the same cluster CA regardless of which SA's token is used —
// safe to read from the pod's normal (company-ops-SA) mount point, which
// every pod that could run this script already has.
const CA_PATH = process.env.BUILDER_MANAGER_CA_PATH || "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";

function readToken() {
  return fs.readFileSync(TOKEN_PATH, "utf8").trim();
}

function k8sRequest(method, path_, body) {
  return new Promise((resolve, reject) => {
    const token = readToken();
    const data = body ? Buffer.from(JSON.stringify(body)) : null;
    const options = {
      method,
      hostname: K8S_HOST,
      port: K8S_PORT,
      path: path_,
      ca: fs.readFileSync(CA_PATH),
      headers: {
        Authorization: `Bearer ${token}`,
        ...(data ? { "Content-Type": "application/json", "Content-Length": data.length } : {}),
      },
    };
    const request = https.request(options, (response) => {
      let text = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { text += chunk; });
      response.on("end", () => {
        let parsed;
        try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }
        if ((response.statusCode || 500) >= 400) {
          return reject(new Error(`k8s API ${method} ${path_} -> ${response.statusCode}: ${text}`));
        }
        resolve(parsed);
      });
    });
    request.on("error", reject);
    if (data) request.write(data);
    request.end();
  });
}

// Plain, unauthenticated GET to the registry to confirm the pushed tag is
// really there — belt-and-suspenders on top of BuildKit reporting success.
function registryHasTag(repo, tag) {
  return new Promise((resolve) => {
    const request = http.request(`http://${REGISTRY_HOST}:${REGISTRY_PORT}/v2/${repo}/tags/list`, (response) => {
      let text = "";
      response.on("data", (chunk) => { text += chunk; });
      response.on("end", () => {
        try {
          const body = JSON.parse(text);
          resolve(Array.isArray(body.tags) && body.tags.includes(tag));
        } catch { resolve(false); }
      });
    });
    request.on("error", () => resolve(false));
    request.end();
  });
}

function text(id, value) {
  return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }] } };
}
function fail(id, message) {
  return { jsonrpc: "2.0", id, error: { code: -32000, message } };
}
function send(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }

function validateRepo(repo) {
  if (!/^[a-z0-9][a-z0-9._/-]*$/.test(repo)) throw new Error(`invalid image_repo: ${repo}`);
  return repo;
}
function validateTag(tag) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(tag)) throw new Error(`invalid image_tag: ${tag}`);
  return tag;
}

// Every context (primary or named) must be a bare, credential-free https://
// URL pinned to a full 40-hex-char commit SHA — enforced here, not left to
// caller/docs discipline, since a mutable branch ref (#main/#prod) can move
// between approval and the moment BuildKit actually clones it. The
// restricted character class also doubles as shell-injection defense: none
// of it can contain a space, quote, `$`, backtick, or `;`.
const CONTEXT_REF_RE = /^https:\/\/[a-zA-Z0-9.-]+\/[a-zA-Z0-9._/-]+\.git#[0-9a-f]{40}$/;
function validateContextRef(ref, label = "context_ref") {
  if (typeof ref !== "string" || !CONTEXT_REF_RE.test(ref)) {
    throw new Error(`invalid ${label} (must be a bare https:// git URL pinned to a full 40-hex commit SHA, e.g. https://github.com/org/repo.git#<40-hex-sha>): ${ref}`);
  }
  return ref;
}
function validateContexts(contexts) {
  const out = {};
  for (const [name, ref] of Object.entries(contexts || {})) {
    if (!/^[a-zA-Z0-9_-]+$/.test(name)) throw new Error(`invalid context name: ${name}`);
    out[name] = validateContextRef(ref, `contexts.${name}`);
  }
  return out;
}
function validateDockerfilePath(p) {
  if (typeof p !== "string" || !p || !/^[a-zA-Z0-9._/-]+$/.test(p) || p.includes("..") || p.startsWith("/")) {
    throw new Error(`invalid dockerfile_path: ${p}`);
  }
  return p;
}
function validateBuildArgs(buildArgs) {
  const out = {};
  for (const [key, value] of Object.entries(buildArgs || {})) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new Error(`invalid build_args key: ${key}`);
    if (typeof value !== "string" || !/^[a-zA-Z0-9 ._/:=-]*$/.test(value)) throw new Error(`invalid build_args value for ${key}: ${value}`);
    out[key] = value;
  }
  return out;
}

// Only the private BuildMy-house org repos need the App-installation token
// this script mints itself; anything else (a public repo, or a caller that
// points at some other host) is passed through unmodified — bare, with no
// credentials, since validateContextRef already forbids embedding any.
const GITHUB_APP_REPO_RE = /^https:\/\/github\.com\/BuildMy-house\/[a-zA-Z0-9._-]+\.git#[0-9a-f]{40}$/;
function needsGithubAppToken(ref) {
  return GITHUB_APP_REPO_RE.test(ref);
}

async function mintGithubAppToken() {
  const { stdout } = await execFileAsync("node", [path.join(SCRIPT_DIR, "github-app-token.js")]);
  const token = stdout.trim();
  if (!token) throw new Error("github-app-token.js produced no token");
  return token;
}

function maskToken(str, token) {
  if (!token || typeof str !== "string") return str;
  return str.split(token).join("ghs_****");
}

// Embeds `$GIT_TOKEN` (a literal shell variable reference, not the token
// itself) into the URL for BuildMy-house refs — the actual value only ever
// exists as a Secret-backed env var inside the Job pod, never in the Job
// manifest text. Wrapped in double quotes by the caller so the shell
// expands it; the validated character set for `ref` excludes `"`/`$`, so
// this is the only variable expansion that can occur.
function resolveContextForShell(ref) {
  if (needsGithubAppToken(ref)) {
    return ref.replace("https://github.com/", "https://x-access-token:$GIT_TOKEN@github.com/");
  }
  return ref;
}

const tools = [
  {
    name: "builder_build_and_push",
    description:
      "Build a Dockerfile from a git context using an ephemeral, rootless BuildKit Job and push the result to the in-cluster registry. Never touches a docker socket, holds no persistent build credentials, and never mounts a Kubernetes API token into the build pod. Supports one or more additional NAMED build contexts (buildx's --build-context equivalent, native to plain BuildKit) — use this for Dockerfile.engineering's `COPY --from=shared ...` step, passing contexts: { shared: 'https://github.com/BuildMy-house/workspace.git#<40-hex-sha>' }. context_ref and every contexts[] value must be a bare (no embedded credentials) https:// URL pinned to a full 40-hex commit SHA, never a branch name — a moving ref can change between approval and the moment the build actually clones it. For the private BuildMy-house/* repos specifically, this tool mints its own short-lived GitHub App token and injects it via a per-build Kubernetes Secret; do not pass credentials in the URL yourself. Blocks until the build finishes (polling internally), then returns the pushed image repo:tag, or the build failure logs (secrets masked) on failure. A successful response is still a claim to verify like any other worker self-report — independently re-check the registry (e.g. registry_list_tags) before trusting it, exactly as this tool itself does internally before reporting success.",
    inputSchema: {
      type: "object",
      required: ["context_ref", "dockerfile_path", "image_repo", "image_tag"],
      properties: {
        context_ref: { type: "string", description: "Primary build context: a bare https:// git URL pinned to a full 40-hex commit SHA, e.g. https://github.com/BuildMy-house/company-os.git#<sha>. Never a branch name." },
        dockerfile_path: { type: "string", description: "Path to the Dockerfile within the primary context, e.g. Dockerfile or Dockerfile.engineering." },
        image_repo: { type: "string", description: "Repository name to push to, e.g. company-os-engineering." },
        image_tag: { type: "string", description: "Tag to push, e.g. a date or short git SHA." },
        contexts: {
          type: "object",
          description: "Optional additional NAMED build contexts (docker buildx's --build-context, native to plain BuildKit's dockerfile frontend). Keys are context names as referenced by COPY --from=<name> in the Dockerfile; values are bare https:// git URLs pinned to a full 40-hex commit SHA, same rules as context_ref.",
          additionalProperties: { type: "string" },
        },
        build_args: {
          type: "object",
          description: "Optional Dockerfile ARG overrides, e.g. { AGENT_FLAVOR: 'opencode', INSTALL_BROWSER: 'false' }.",
          additionalProperties: { type: "string" },
        },
        timeout_seconds: { type: "number", minimum: 30, maximum: 3600, default: 900, description: "How long to wait for the build Job to finish before giving up (the Job itself keeps running/is cleaned up regardless)." },
      },
      additionalProperties: false,
    },
  },
];

function buildJobManifest({ jobName, contextRef, namedContexts, dockerfilePath, destination, buildArgs, secretName }) {
  const optParts = [
    `--opt context="${resolveContextForShell(contextRef)}"`,
    ...Object.entries(namedContexts).map(([name, ref]) => `--opt context:${name}="${resolveContextForShell(ref)}"`),
    `--opt filename="${dockerfilePath}"`,
    ...Object.entries(buildArgs).map(([key, value]) => `--opt build-arg:${key}="${value}"`),
    `--output type=image,name="${REGISTRY_HOST}:${REGISTRY_PORT}/${destination}",push=true,registry.insecure=true`,
  ];
  const command = `buildctl-daemonless.sh build --frontend dockerfile.v0 ${optParts.join(" ")}`;

  const env = [
    // Works around a rootless-BuildKit-in-k8s "mount proc: operation not
    // permitted" failure on Dockerfile RUN steps — verified necessary and
    // sufficient live in this cluster on 2026-09-29, no broader privileged
    // workaround needed alongside it.
    { name: "BUILDKITD_FLAGS", value: "--oci-worker-no-process-sandbox" },
  ];
  if (secretName) {
    env.push({ name: "GIT_TOKEN", valueFrom: { secretKeyRef: { name: secretName, key: "token" } } });
  }

  return {
    apiVersion: "batch/v1",
    kind: "Job",
    metadata: { name: jobName, namespace: NAMESPACE, labels: { "builder-manager/job": "true" } },
    spec: {
      backoffLimit: 0,
      ttlSecondsAfterFinished: 600,
      template: {
        metadata: {
          labels: { "builder-manager/job": "true" },
          annotations: { "container.apparmor.security.beta.kubernetes.io/buildkit": "unconfined" },
        },
        spec: {
          restartPolicy: "Never",
          automountServiceAccountToken: false,
          containers: [
            {
              name: "buildkit",
              image: BUILDKIT_IMAGE,
              command: ["sh", "-c", command],
              env,
              securityContext: {
                seccompProfile: { type: "Unconfined" },
                runAsUser: 1000,
                runAsGroup: 1000,
              },
              resources: {
                requests: { cpu: "500m", memory: "1Gi" },
                limits: { cpu: "2", memory: "3Gi" },
              },
            },
          ],
        },
      },
    },
  };
}

async function waitForJob(jobName, deadlineMs) {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    const job = await k8sRequest("GET", `/apis/batch/v1/namespaces/${NAMESPACE}/jobs/${jobName}`);
    const conditions = job.status?.conditions || [];
    const complete = conditions.find((c) => c.type === "Complete" && c.status === "True");
    const failed = conditions.find((c) => c.type === "Failed" && c.status === "True");
    if (complete) return { succeeded: true, job };
    if (failed) return { succeeded: false, job, reason: failed.reason, message: failed.message };
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  return { succeeded: false, timedOut: true };
}

async function fetchJobPodLogs(jobName) {
  const pods = await k8sRequest("GET", `/api/v1/namespaces/${NAMESPACE}/pods?labelSelector=job-name=${jobName}`);
  const podName = pods.items?.[0]?.metadata?.name;
  if (!podName) return "(no pod found for job)";
  try {
    const logs = await k8sRequest("GET", `/api/v1/namespaces/${NAMESPACE}/pods/${podName}/log?container=buildkit&tailLines=200`);
    return typeof logs === "string" ? logs : JSON.stringify(logs);
  } catch (error) {
    return `(could not fetch logs: ${error.message})`;
  }
}

async function deleteJob(jobName) {
  try {
    await k8sRequest("DELETE", `/apis/batch/v1/namespaces/${NAMESPACE}/jobs/${jobName}?propagationPolicy=Background`);
  } catch {
    // best-effort cleanup; ttlSecondsAfterFinished on the Job spec is the backstop
  }
}

async function createGitTokenSecret(jobName, token) {
  const secretName = `${jobName}-git-token`;
  const manifest = {
    apiVersion: "v1",
    kind: "Secret",
    metadata: { name: secretName, namespace: NAMESPACE, labels: { "builder-manager/job": "true" } },
    type: "Opaque",
    data: { token: Buffer.from(token, "utf8").toString("base64") },
  };
  await k8sRequest("POST", `/api/v1/namespaces/${NAMESPACE}/secrets`, manifest);
  return secretName;
}

async function deleteSecret(secretName) {
  if (!secretName) return;
  try {
    await k8sRequest("DELETE", `/api/v1/namespaces/${NAMESPACE}/secrets/${secretName}`);
  } catch {
    // best-effort; the token backing it self-expires (~1h, GitHub-imposed)
    // even if this delete fails, bounding exposure regardless.
  }
}

async function buildAndPush(args) {
  const contextRef = validateContextRef(args.context_ref);
  const imageRepo = validateRepo(args.image_repo);
  const imageTag = validateTag(args.image_tag);
  const dockerfilePath = validateDockerfilePath(args.dockerfile_path);
  const namedContexts = validateContexts(args.contexts);
  const buildArgs = validateBuildArgs(args.build_args);
  const timeoutSeconds = args.timeout_seconds ?? 900;

  const allRefs = [contextRef, ...Object.values(namedContexts)];
  const needsToken = allRefs.some(needsGithubAppToken);

  const jobName = `builder-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  let token = null;
  let secretName = null;
  if (needsToken) {
    token = await mintGithubAppToken();
    secretName = await createGitTokenSecret(jobName, token);
  }

  const destination = `${imageRepo}:${imageTag}`;
  const manifest = buildJobManifest({ jobName, contextRef, namedContexts, dockerfilePath, destination, buildArgs, secretName });

  try {
    await k8sRequest("POST", `/apis/batch/v1/namespaces/${NAMESPACE}/jobs`, manifest);
  } catch (error) {
    await deleteSecret(secretName);
    throw error;
  }

  const timeoutMs = Math.round(timeoutSeconds * 1000);
  const result = await waitForJob(jobName, timeoutMs);
  const rawLogs = await fetchJobPodLogs(jobName);
  const logs = maskToken(rawLogs, token);
  await deleteJob(jobName);
  await deleteSecret(secretName);

  if (!result.succeeded) {
    const reason = result.timedOut ? `timed out after ${timeoutSeconds}s` : `${result.reason}: ${maskToken(result.message, token)}`;
    throw new Error(`builder Job ${jobName} did not succeed (${reason}). Last 200 log lines:\n${logs}`);
  }

  const pushed = await registryHasTag(imageRepo, imageTag);
  if (!pushed) {
    throw new Error(`builder Job ${jobName} reported success but ${destination} is not visible in the registry yet. BuildKit logs:\n${logs}`);
  }

  return { job: jobName, image: `${REGISTRY_HOST}:${REGISTRY_PORT}/${destination}`, pushed: true };
}

async function call(name, args) {
  if (name === "builder_build_and_push") {
    return buildAndPush(args);
  }
  throw new Error(`unknown tool: ${name}`);
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let request;
  try { request = JSON.parse(line); } catch { continue; }
  if (request.method === "initialize") { send({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: request.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "builder-manager", version: "0.2.0" } } }); continue; }
  if (request.method === "notifications/initialized") continue;
  // See container-manager-mcp.js for the full explanation: MCP's optional "ping"
  // utility must get an immediate empty result or Hermes's keepalive probe hangs for
  // its 30s RPC timeout and this connection flaps connected/degraded/parked forever.
  if (request.method === "ping") { send({ jsonrpc: "2.0", id: request.id, result: {} }); continue; }
  if (request.method === "tools/list") { send({ jsonrpc: "2.0", id: request.id, result: { tools } }); continue; }
  if (request.method === "tools/call") {
    try { send(text(request.id, await call(request.params.name, request.params.arguments || {}))); }
    catch (error) { send(fail(request.id, error.message)); }
  }
}
