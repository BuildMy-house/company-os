#!/usr/bin/env node

// Ephemeral, rootless-BuildKit-based build+push, without ever giving the
// calling workload (engineering-agent, or Hermes's scoped dispatcher) a
// docker socket or persistent registry credentials. Instead of building locally, this creates a
// short-lived Kubernetes Job in company-ops running two containers of the
// rootless `moby/buildkit` image — a `buildkitd` daemon (native-sidecar
// initContainer) and a `buildctl` client (regular container), talking over
// a Unix socket on a shared `emptyDir` volume, never `buildctl-daemonless.sh`'s
// single-process wrapper (see "Job-pod isolation" below for why they are
// split) — which builds a git-context Dockerfile and pushes the result
// straight to the in-cluster registry
// (registry.company-ops.svc.cluster.local:5000).
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
//      `--oci-worker-no-process-sandbox` (an argv flag on the `buildkitd`
//      container's own command, works around a "mount proc: operation not
//      permitted" failure on RUN steps when nested under the pod's own
//      mount-namespace restrictions) are sufficient — no `privileged: true`,
//      no added Linux capabilities, no `/dev/fuse` hostPath, no experimental
//      `hostUsers` pod-level user namespaces (`hostUsers: false` was tried
//      and failed on this node's newuidmap/subuid setup — not needed once
//      the above three settings are in place). All four confirmed with two
//      real end-to-end build+push Jobs against **public** repos (a baseline
//      single-context build with a real `RUN` step, and a named-additional-
//      context build) — see docs/DEPLOY-ENGINEERING.md for exact evidence.
//      The private GitHub-App-token path has since been exercised by an
//      in-cluster BuildKit build of the company-os image. Building
//      Dockerfile.engineering with its private workspace named context
//      remains a separate end-to-end verification item.
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
// SHA (immutable ref requirement — enforced here, not just by convention;
// the URL text itself never carries a credential, in any version of this
// design). For the private `BuildMy-house/*` repos specifically, this
// script mints a short-lived GitHub App installation token itself (reusing
// `github-app-token.js`, the same mechanism `sync-repo.sh` already uses) and
// delivers it to BuildKit as a **pre-flight git-auth build secret**
// (`buildctl --secret id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN`) — BuildKit's
// git source consumes a secret with exactly this reserved ID to
// authenticate the context fetch itself, entirely out of band from the
// context URL text. This replaced an earlier design that embedded
// `x-access-token:$GIT_TOKEN@` into the context URL via shell expansion:
// that put the token in a Job-pod env var AND meant BuildKit's own
// git-source log line echoed the resolved (credentialed) URL into the pod's
// stdout logs, both real exposure paths, not just theoretical ones — see
// the "Job-pod isolation" note below for why the replacement actually
// closes the gap rather than just moving it, verified with a real
// malicious-Dockerfile self-test (`Dockerfile.buildkit-selftest-secret-isolation`).
// Callers should never pass credentials in a `context_ref`/`contexts` value
// themselves — `validateContextRef` rejects any URL containing one.
//
// Job-pod isolation: this cluster's rootless-BuildKit workaround requires
// `--oci-worker-no-process-sandbox` on the `buildkitd` container (see below)
// — which, unlike BuildKit's normal fully-sandboxed RUN-step isolation,
// does NOT give a `RUN` step its own private PID namespace. A `RUN` step in
// that container can potentially read `/proc/<pid>/environ` of *any other
// process in the same container*, including `buildkitd`/`buildctl`'s own,
// if either process ever held the token as an env var. Rather than accept
// that as a residual risk, the Job pod runs `buildkitd` and `buildctl` as
// **two separate containers** (native-sidecar `initContainer` +
// `container`, sharing only a Unix socket over an `emptyDir` volume — see
// `buildJobManifest`). Kubernetes gives each container in a pod its own PID
// namespace by default (this script never sets `shareProcessNamespace:
// true` — do not add it), so `GIT_AUTH_TOKEN` — set only on the `buildctl`
// container's env, sourced from a per-build Secret — is never visible to
// `/proc` inside the `buildkitd` container, which is the one running with
// the relaxed process sandbox and the one actually executing `RUN` steps.
// `buildctl` itself needs no relaxed sandbox at all; it only speaks the
// BuildKit control gRPC protocol over the shared socket.
//
// The per-build git-token Secret is deleted alongside the Job on every exit
// path (success, failure, timeout, or a crash before either was created —
// see the `try`/`finally` in `buildAndPush`), and additionally carries an
// `ownerReference` to the Job itself, so Kubernetes' own garbage collector
// deletes it if this script's own process dies before its `finally` runs
// (the Job's `ttlSecondsAfterFinished` bounds the same crash case for the
// Job object).
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

function registryManifestDigest(repo, tag) {
  return new Promise((resolve) => {
    const request = http.request(`http://${REGISTRY_HOST}:${REGISTRY_PORT}/v2/${repo}/manifests/${tag}`, {
      method: "HEAD",
      headers: { Accept: "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json" },
    }, (response) => {
      const digest = response.headers["docker-content-digest"];
      resolve((response.statusCode || 500) < 400 && /^sha256:[0-9a-f]{64}$/.test(digest || "") ? digest : null);
      response.resume();
    });
    request.on("error", () => resolve(null));
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

function validateTimeoutSeconds(value) {
  if (value === undefined) return 900;
  // Re-validated here, not just declared in inputSchema: this is called
  // directly (self-test/manual invocation) as well as via tools/call, and
  // an MCP client is not guaranteed to enforce a tool's declared
  // minimum/maximum before sending arguments.
  if (typeof value !== "number" || !Number.isFinite(value) || value < 30 || value > 3600) {
    throw new Error(`invalid timeout_seconds (must be a number in [30, 3600]): ${value}`);
  }
  return value;
}

const tools = [
  process.env.BUILDER_SCOPE === "company-os" ? {
    name: "build_company_os_image",
    description: "Build and push the Hermes/company-os image from one immutable company-os commit using the isolated in-cluster BuildKit builder. Returns a node-pullable localhost:30500 image reference pinned by registry digest. Source repository, Dockerfile, image repository, and tag prefix are fixed by policy; this tool cannot build another repository or access an engineering workspace.",
    inputSchema: {
      type: "object",
      required: ["source_sha"],
      properties: {
        source_sha: { type: "string", description: "Full lowercase 40-character commit SHA from BuildMy-house/company-os." },
        timeout_seconds: { type: "number", minimum: 30, maximum: 3600, default: 900 },
      },
      additionalProperties: false,
    },
  } : {
    name: "builder_build_and_push",
    description:
      "Build a Dockerfile from a git context using an ephemeral, rootless BuildKit Job and push the result to the in-cluster registry. Never touches a docker socket, holds no persistent build credentials, and never mounts a Kubernetes API token into the build pod. Supports one or more additional NAMED build contexts (buildx's --build-context equivalent, native to plain BuildKit) — use this for Dockerfile.engineering's `COPY --from=shared ...` step, passing contexts: { shared: 'https://github.com/BuildMy-house/workspace.git#<40-hex-sha>' }. context_ref and every contexts[] value must be a bare (no embedded credentials) https:// URL pinned to a full 40-hex commit SHA, never a branch name — a moving ref can change between approval and the moment the build actually clones it. For the private BuildMy-house/* repos specifically, this tool mints its own short-lived GitHub App token and injects it via a per-build Kubernetes Secret; do not pass credentials in the URL yourself. Blocks until the build finishes (polling internally), verifies the registry tag and manifest digest, then returns a node-pullable localhost:30500 image reference pinned by digest, or the build failure logs (secrets masked) on failure.",
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
if (!new Set(["company-os", "engineering"]).has(process.env.BUILDER_SCOPE)) {
  throw new Error("BUILDER_SCOPE must be explicitly set to company-os or engineering");
}

// Unix socket shared between the two containers below via an emptyDir
// volume — never a host path, never TCP.
const SOCKET_DIR = "/run/buildkit";
const SOCKET_ADDR = `unix://${SOCKET_DIR}/buildkitd.sock`;

// Two-container Job: `buildkitd` (a native-sidecar initContainer, i.e.
// `restartPolicy: "Always"` — starts before, and keeps running alongside,
// the regular container below; Kubernetes does not wait for it to exit
// before considering the Job's pod — and therefore the Job itself —
// complete, and terminates it automatically once the regular container(s)
// finish; requires Kubernetes >=1.29, confirmed available here: this
// cluster runs v1.36) runs the actual daemon with the relaxed process
// sandbox this cluster needs; `buildctl` (a plain container) is the only
// place `GIT_AUTH_TOKEN` is ever set. See the "Job-pod isolation" header
// comment above for why this split (rather than one container running
// both) is what actually keeps the token out of reach of a RUN step, given
// `--oci-worker-no-process-sandbox`. Every argv array below is passed
// directly to execve (no `sh -c`), so no value — however it was validated —
// ever goes through shell parsing/expansion.
function buildJobManifest({ jobName, contextRef, namedContexts, dockerfilePath, destination, buildArgs, secretName }) {
  const buildctlArgs = [
    "build",
    "--frontend", "dockerfile.v0",
    "--opt", `context=${contextRef}`,
    ...Object.entries(namedContexts).flatMap(([name, ref]) => ["--opt", `context:${name}=${ref}`]),
    "--opt", `filename=${dockerfilePath}`,
    ...Object.entries(buildArgs).flatMap(([key, value]) => ["--opt", `build-arg:${key}=${value}`]),
    "--output", `type=image,name=${REGISTRY_HOST}:${REGISTRY_PORT}/${destination},push=true,registry.insecure=true`,
  ];
  if (secretName) {
    // BuildKit's git source treats a secret with exactly this reserved ID
    // as pre-flight HTTP(S) git-auth material for the context fetch itself
    // — consumed internally by buildkitd's git source, never written to
    // the build filesystem unless a Dockerfile explicitly does
    // `RUN --mount=type=secret,id=GIT_AUTH_TOKEN` (neither self-test
    // Dockerfile in this repo does). `env=GIT_AUTH_TOKEN` tells buildctl to
    // read the value from its own process's environment (set below via
    // secretKeyRef) and ship it to buildkitd over the control connection —
    // the value itself never appears in this argv.
    buildctlArgs.push("--secret", "id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN");
  }

  const sharedVolume = { name: "buildkit-socket", emptyDir: {} };
  const sharedVolumeMount = { name: "buildkit-socket", mountPath: SOCKET_DIR };

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
          // Keyed to the buildkitd container specifically — the only one
          // that needs the relaxed AppArmor profile to run RUN steps.
          annotations: { "container.apparmor.security.beta.kubernetes.io/buildkitd": "unconfined" },
        },
        spec: {
          restartPolicy: "Never",
          automountServiceAccountToken: false,
          // Deliberately absent (defaults to false): each container in
          // this pod must keep its own PID namespace, or `buildkitd`'s
          // relaxed process sandbox could see `buildctl`'s env — see the
          // "Job-pod isolation" header comment.
          volumes: [sharedVolume],
          initContainers: [
            {
              name: "buildkitd",
              restartPolicy: "Always", // native sidecar; does not block Job completion
              image: BUILDKIT_IMAGE,
              command: [
                "rootlesskit", "buildkitd",
                // Works around a rootless-BuildKit-in-k8s "mount proc:
                // operation not permitted" failure on Dockerfile RUN
                // steps — verified necessary and sufficient live in this
                // cluster on 2026-09-29, no broader privileged workaround
                // needed alongside it. This is also precisely the setting
                // that makes the two-container split above load-bearing
                // rather than defense-in-depth-only.
                "--oci-worker-no-process-sandbox",
                "--addr", SOCKET_ADDR,
              ],
              securityContext: {
                seccompProfile: { type: "Unconfined" },
                runAsUser: 1000,
                runAsGroup: 1000,
              },
              volumeMounts: [sharedVolumeMount],
              // Gates startup of the `buildctl` container below on the
              // socket actually existing — native sidecars only block
              // regular-container start on Startedness (startupProbe
              // success), not plain process liveness.
              startupProbe: {
                exec: { command: ["sh", "-c", `test -S ${SOCKET_DIR}/buildkitd.sock`] },
                periodSeconds: 1,
                failureThreshold: 60,
              },
              resources: {
                requests: { cpu: "500m", memory: "1Gi" },
                limits: { cpu: "2", memory: "3Gi" },
              },
            },
          ],
          containers: [
            {
              name: "buildctl",
              image: BUILDKIT_IMAGE,
              command: ["buildctl", "--addr", SOCKET_ADDR, ...buildctlArgs],
              // GIT_AUTH_TOKEN is set ONLY here, never on buildkitd above —
              // this is the entire point of the split. Absent when no
              // private-repo ref is in play.
              env: secretName
                ? [{ name: "GIT_AUTH_TOKEN", valueFrom: { secretKeyRef: { name: secretName, key: "token" } } }]
                : [],
              securityContext: { runAsUser: 1000, runAsGroup: 1000 },
              volumeMounts: [sharedVolumeMount],
              resources: {
                requests: { cpu: "100m", memory: "128Mi" },
                limits: { cpu: "500m", memory: "256Mi" },
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

async function fetchContainerLog(podName, container) {
  try {
    const logs = await k8sRequest("GET", `/api/v1/namespaces/${NAMESPACE}/pods/${podName}/log?container=${container}&tailLines=200`);
    return typeof logs === "string" ? logs : JSON.stringify(logs);
  } catch (error) {
    return `(could not fetch ${container} logs: ${error.message})`;
  }
}

async function fetchJobPodLogs(jobName) {
  const pods = await k8sRequest("GET", `/api/v1/namespaces/${NAMESPACE}/pods?labelSelector=job-name=${jobName}`);
  const podName = pods.items?.[0]?.metadata?.name;
  if (!podName) return "(no pod found for job)";
  const [buildctlLog, buildkitdLog] = await Promise.all([
    fetchContainerLog(podName, "buildctl"),
    fetchContainerLog(podName, "buildkitd"),
  ]);
  return `--- buildctl (client) ---\n${buildctlLog}\n--- buildkitd (daemon) ---\n${buildkitdLog}`;
}

async function deleteJob(jobName) {
  try {
    await k8sRequest("DELETE", `/apis/batch/v1/namespaces/${NAMESPACE}/jobs/${jobName}?propagationPolicy=Background`);
  } catch {
    // best-effort cleanup; ttlSecondsAfterFinished on the Job spec is the backstop
  }
}

// Called AFTER the Job is created (needs the Job's own UID), so the Secret
// can carry an ownerReference to it — this Role has no `patch` on Secrets
// (see k8s/builder-rbac.yaml), so the ownerReference must be set at create
// time, not added later. Kubernetes' garbage collector will then delete
// this Secret on its own if this script's own process dies before its
// `finally` block runs `deleteSecret` — the Job's own
// `ttlSecondsAfterFinished` is the equivalent backstop for the Job object.
async function createGitTokenSecret(jobName, jobUid, token) {
  const secretName = `${jobName}-git-token`;
  const manifest = {
    apiVersion: "v1",
    kind: "Secret",
    metadata: {
      name: secretName,
      namespace: NAMESPACE,
      labels: { "builder-manager/job": "true" },
      ownerReferences: [{ apiVersion: "batch/v1", kind: "Job", name: jobName, uid: jobUid, blockOwnerDeletion: false }],
    },
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
  const timeoutSeconds = validateTimeoutSeconds(args.timeout_seconds);

  const allRefs = [contextRef, ...Object.values(namedContexts)];
  const needsToken = allRefs.some(needsGithubAppToken);
  // Secure secret isolation (GIT_AUTH_TOKEN confined to the buildctl
  // container, never buildkitd) is covered by the malicious-Dockerfile
  // self-test. The live builder-manager Role includes create/delete on
  // ephemeral Secrets; a private company-os context build verified this
  // path in-cluster.

  const jobName = `builder-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const destination = `${imageRepo}:${imageTag}`;

  let jobCreated = false;
  let secretName = null;
  let token = null;
  try {
    // Job created first (without a Secret to reference yet — the pod
    // simply stays Pending on that container until the Secret referenced
    // by name below shows up) so the Secret can carry the Job's real UID
    // as its ownerReference; see createGitTokenSecret.
    const manifest = buildJobManifest({
      jobName, contextRef, namedContexts, dockerfilePath, destination, buildArgs,
      secretName: needsToken ? `${jobName}-git-token` : null,
    });
    const jobResp = await k8sRequest("POST", `/apis/batch/v1/namespaces/${NAMESPACE}/jobs`, manifest);
    jobCreated = true;

    if (needsToken) {
      token = await mintGithubAppToken();
      secretName = await createGitTokenSecret(jobName, jobResp.metadata.uid, token);
    }

    const timeoutMs = Math.round(timeoutSeconds * 1000);
    const result = await waitForJob(jobName, timeoutMs);
    const rawLogs = await fetchJobPodLogs(jobName);
    const logs = maskToken(rawLogs, token);

    if (!result.succeeded) {
      const reason = result.timedOut ? `timed out after ${timeoutSeconds}s` : `${result.reason}: ${maskToken(result.message, token)}`;
      throw new Error(`builder Job ${jobName} did not succeed (${reason}). Logs:\n${logs}`);
    }

    const pushed = await registryHasTag(imageRepo, imageTag);
    if (!pushed) {
      throw new Error(`builder Job ${jobName} reported success but ${destination} is not visible in the registry yet. Logs:\n${logs}`);
    }
    const digest = await registryManifestDigest(imageRepo, imageTag);
    if (!digest) throw new Error(`builder Job ${jobName} pushed ${destination} but its immutable manifest digest could not be verified`);

    // BuildKit pushes through the in-cluster DNS name, but kubelet pulls via
    // the node's localhost NodePort; cluster DNS is not resolvable by containerd.
    return { job: jobName, image: `localhost:30500/${destination}@${digest}`, digest, pushed: true };
  } finally {
    // Unconditional: runs on every exit path, including a k8sRequest
    // rejection from waitForJob/mintGithubAppToken/registryHasTag, not just
    // the success/expected-failure paths above. deleteJob/deleteSecret are
    // themselves already best-effort (internal try/catch), so this can
    // never throw over whatever error is already propagating.
    if (jobCreated) await deleteJob(jobName);
    await deleteSecret(secretName);
  }
}

async function call(name, args) {
  if (process.env.BUILDER_SCOPE === "company-os") {
    if (name !== "build_company_os_image") throw new Error(`unknown tool: ${name}`);
    if (Object.keys(args).some((key) => !["source_sha", "timeout_seconds"].includes(key))) {
      throw new Error("only source_sha and timeout_seconds are accepted");
    }
    if (typeof args.source_sha !== "string" || !/^[0-9a-f]{40}$/.test(args.source_sha)) {
      throw new Error("source_sha must be a full lowercase 40-character commit SHA");
    }
    const timeoutSeconds = args.timeout_seconds === undefined ? 900 : validateTimeoutSeconds(args.timeout_seconds);
    return buildAndPush({
      context_ref: `https://github.com/BuildMy-house/company-os.git#${args.source_sha}`,
      dockerfile_path: "Dockerfile",
      image_repo: "company-os",
      image_tag: `hermes-${args.source_sha.slice(0, 12)}`,
      timeout_seconds: timeoutSeconds,
    });
  }
  if (process.env.BUILDER_SCOPE === "engineering" && name === "builder_build_and_push") {
    return buildAndPush(args);
  }
  throw new Error(`unknown tool: ${name}`);
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let request;
  try { request = JSON.parse(line); } catch { continue; }
  if (request.method === "initialize") { send({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: request.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: process.env.BUILDER_SCOPE === "company-os" ? "hermes-build-dispatcher" : "builder-manager", version: "0.2.0" } } }); continue; }
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
