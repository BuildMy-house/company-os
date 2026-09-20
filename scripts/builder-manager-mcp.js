#!/usr/bin/env node

// Ephemeral, Kaniko-based build+push, without ever giving the calling pod
// (hermes-gateway or engineering-agent) a docker socket or persistent build
// credentials. Instead of building locally, this creates a short-lived
// Kubernetes Job in company-ops running gcr.io/kaniko-project/executor,
// which builds a git-context Dockerfile and pushes the result straight to
// the in-cluster registry (registry.company-ops.svc.cluster.local:5000).
// Kaniko needs no docker daemon and runs safely inside a k3s pod.
//
// Authenticates to the Kubernetes API as the dedicated "builder-manager"
// ServiceAccount (k8s/builder-rbac.yaml) via a bound token mounted at
// /var/run/secrets/builder-manager/token — NOT the pod's default
// /var/run/secrets/kubernetes.io/serviceaccount/token, which belongs to
// the shared "company-ops" SA used by container-manager/k8s_deployment and
// can patch/delete Deployments. builder-manager's Role can only
// create/get/list/watch/delete Jobs and read their pods' logs — nothing
// else. The Kaniko Job pod itself runs with no ServiceAccount permissions
// at all (automountServiceAccountToken: false): it never touches the
// Kubernetes API, only the git remote and the registry over plain HTTP.
import https from "node:https";
import http from "node:http";
import fs from "node:fs";
import readline from "node:readline";

const K8S_HOST = process.env.KUBERNETES_SERVICE_HOST || "kubernetes.default.svc";
const K8S_PORT = process.env.KUBERNETES_SERVICE_PORT || "443";
const NAMESPACE = "company-ops";
const REGISTRY_HOST = process.env.REGISTRY_HOST || "registry.company-ops.svc.cluster.local";
const REGISTRY_PORT = process.env.REGISTRY_PORT || "5000";
const KANIKO_IMAGE = process.env.KANIKO_IMAGE || "gcr.io/kaniko-project/executor:latest";

// Deliberately NOT the pod's default in-cluster SA path — see header.
const TOKEN_PATH = process.env.BUILDER_MANAGER_TOKEN_PATH || "/var/run/secrets/builder-manager/token";
// The CA is the same cluster CA regardless of which SA's token is used —
// safe to read from the pod's normal (company-ops-SA) mount point, which
// every pod that could run this script already has.
const CA_PATH = process.env.BUILDER_MANAGER_CA_PATH || "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";

function readToken() {
  return fs.readFileSync(TOKEN_PATH, "utf8").trim();
}

function k8sRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const token = readToken();
    const data = body ? Buffer.from(JSON.stringify(body)) : null;
    const options = {
      method,
      hostname: K8S_HOST,
      port: K8S_PORT,
      path,
      ca: fs.readFileSync(CA_PATH),
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": body ? "application/json" : undefined,
        ...(data ? { "Content-Length": data.length } : {}),
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
          return reject(new Error(`k8s API ${method} ${path} -> ${response.statusCode}: ${text}`));
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
// really there — belt-and-suspenders on top of Kaniko reporting success.
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
function validateContextRef(ref) {
  // Kaniko's --context accepts git://, git@, https://, or a tar.gz URL.
  // Restrict to git/https to keep the attack surface small — no local
  // paths, no arbitrary schemes.
  if (!/^(git:\/\/|git@|https:\/\/)/.test(ref)) {
    throw new Error(`invalid context_ref (must start with git://, git@, or https://): ${ref}`);
  }
  return ref;
}

const tools = [
  {
    name: "builder_build_and_push",
    description: "Build a Dockerfile from a git context using an ephemeral Kaniko Job and push the result to the in-cluster registry. Never touches a docker socket or holds persistent build credentials. Blocks until the build finishes (poll internally), then returns the pushed image repo:tag or the build failure logs.",
    inputSchema: {
      type: "object",
      required: ["context_ref", "dockerfile_path", "image_repo", "image_tag"],
      properties: {
        context_ref: { type: "string", description: "Kaniko build context, e.g. a git URL: https://github.com/org/repo.git#branch, or git://... . Not a local path." },
        dockerfile_path: { type: "string", description: "Path to the Dockerfile within the context, e.g. Dockerfile or Dockerfile.engineering." },
        image_repo: { type: "string", description: "Repository name to push to, e.g. company-os-engineering." },
        image_tag: { type: "string", description: "Tag to push, e.g. a date or short git SHA." },
        timeout_seconds: { type: "number", minimum: 30, maximum: 1800, default: 600, description: "How long to wait for the build Job to finish before giving up (the Job itself keeps running/is cleaned up regardless)." },
      },
      additionalProperties: false,
    },
  },
];

function buildJobManifest(jobName, contextRef, dockerfilePath, destination) {
  return {
    apiVersion: "batch/v1",
    kind: "Job",
    metadata: { name: jobName, namespace: NAMESPACE, labels: { "builder-manager/job": "true" } },
    spec: {
      backoffLimit: 0,
      ttlSecondsAfterFinished: 600,
      template: {
        metadata: { labels: { "builder-manager/job": "true" } },
        spec: {
          restartPolicy: "Never",
          automountServiceAccountToken: false,
          containers: [
            {
              name: "kaniko",
              image: KANIKO_IMAGE,
              args: [
                `--context=${contextRef}`,
                `--dockerfile=${dockerfilePath}`,
                `--destination=${REGISTRY_HOST}:${REGISTRY_PORT}/${destination}`,
                "--insecure",
                "--insecure-pull",
                "--skip-tls-verify",
              ],
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
    const logs = await k8sRequest("GET", `/api/v1/namespaces/${NAMESPACE}/pods/${podName}/log?container=kaniko&tailLines=200`);
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

async function buildAndPush(contextRef, dockerfilePath, imageRepo, imageTag, timeoutSeconds) {
  validateContextRef(contextRef);
  validateRepo(imageRepo);
  validateTag(imageTag);
  if (!dockerfilePath || typeof dockerfilePath !== "string") throw new Error("dockerfile_path is required");

  const jobName = `builder-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const destination = `${imageRepo}:${imageTag}`;
  const manifest = buildJobManifest(jobName, contextRef, dockerfilePath, destination);

  await k8sRequest("POST", `/apis/batch/v1/namespaces/${NAMESPACE}/jobs`, manifest);

  const timeoutMs = Math.round((timeoutSeconds ?? 600) * 1000);
  const result = await waitForJob(jobName, timeoutMs);
  const logs = await fetchJobPodLogs(jobName);
  await deleteJob(jobName);

  if (!result.succeeded) {
    const reason = result.timedOut ? `timed out after ${timeoutSeconds ?? 600}s` : `${result.reason}: ${result.message}`;
    throw new Error(`builder Job ${jobName} did not succeed (${reason}). Last 200 log lines:\n${logs}`);
  }

  const pushed = await registryHasTag(imageRepo, imageTag);
  if (!pushed) {
    throw new Error(`builder Job ${jobName} reported success but ${destination} is not visible in the registry yet. Kaniko logs:\n${logs}`);
  }

  return { job: jobName, image: `${REGISTRY_HOST}:${REGISTRY_PORT}/${destination}`, pushed: true };
}

async function call(name, args) {
  if (name === "builder_build_and_push") {
    return buildAndPush(args.context_ref, args.dockerfile_path, args.image_repo, args.image_tag, args.timeout_seconds);
  }
  throw new Error(`unknown tool: ${name}`);
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let request;
  try { request = JSON.parse(line); } catch { continue; }
  if (request.method === "initialize") { send({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: request.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "builder-manager", version: "0.1.0" } } }); continue; }
  if (request.method === "notifications/initialized") continue;
  if (request.method === "tools/list") { send({ jsonrpc: "2.0", id: request.id, result: { tools } }); continue; }
  if (request.method === "tools/call") {
    try { send(text(request.id, await call(request.params.name, request.params.arguments || {}))); }
    catch (error) { send(fail(request.id, error.message)); }
  }
}
