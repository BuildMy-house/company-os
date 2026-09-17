#!/usr/bin/env node

// Kubernetes-backed local container manager. It deliberately manages only the
// company-ops namespace; image building/importing stays outside the pod.
import { existsSync, readFileSync } from "node:fs";
import https from "node:https";
import readline from "node:readline";

const namespace = "company-ops";
const host = process.env.KUBERNETES_SERVICE_HOST;
const port = process.env.KUBERNETES_SERVICE_PORT || "443";
const tokenPath = "/var/run/secrets/kubernetes.io/serviceaccount/token";
const caPath = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
const token = existsSync(tokenPath) ? readFileSync(tokenPath, "utf8").trim() : null;
const ca = existsSync(caPath) ? readFileSync(caPath) : null;
const base = `https://${host}:${port}`;

async function api(path, options = {}) {
  if (!token || !ca || !host) throw new Error("Kubernetes API unavailable outside a k3s pod");
  return new Promise((resolve, reject) => {
    const request = https.request(`${base}${path}`, { method: options.method || "GET", ca, headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(options.headers || {}) } }, (response) => {
      let text = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { text += chunk; });
      response.on("end", () => {
        let body;
        try { body = text ? JSON.parse(text) : null; } catch { body = text; }
        if ((response.statusCode || 500) >= 400) return reject(new Error(`${response.statusCode}: ${body?.message || text}`));
        resolve(body);
      });
    });
    request.on("error", reject);
    if (options.body) request.write(options.body);
    request.end();
  });
}

// Node's fetch does not accept a CA buffer without an https dispatcher. The
// cluster CA is mounted for standard clients; local k3s uses the service CA.
// NODE_EXTRA_CA_CERTS is set by the pod runtime where required.
async function k8s(path, options = {}) {
  return api(path, options);
}

function text(id, value) {
  return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify(value, null, 2) }] } };
}

function fail(id, message) {
  return { jsonrpc: "2.0", id, error: { code: -32000, message } };
}

function send(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const tools = [
  { name: "container_status", description: "List local company-ops deployments, images, and replica status.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "container_health", description: "Check readiness and pod health for one local deployment.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string" } }, additionalProperties: false } },
  { name: "container_logs", description: "Read logs from the current pod for one local deployment.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string" }, tail_lines: { type: "integer", minimum: 1, maximum: 500, default: 100 } }, additionalProperties: false } },
  { name: "container_upgrade", description: "Change a local deployment to an already-imported image and wait for Kubernetes rollout.", inputSchema: { type: "object", required: ["deployment", "image"], properties: { deployment: { type: "string" }, image: { type: "string" }, container: { type: "string", default: "" } }, additionalProperties: false } },
  { name: "container_restart", description: "Restart one local deployment without changing its image.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string" } }, additionalProperties: false } },
  { name: "container_rollback", description: "Roll one local deployment back to the image saved by its last container_upgrade.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string" } }, additionalProperties: false } },
  { name: "container_test", description: "Start an isolated temporary test deployment from an already-imported image and wait for its pod to start.", inputSchema: { type: "object", required: ["image"], properties: { image: { type: "string" }, timeout_seconds: { type: "integer", minimum: 5, maximum: 300, default: 90 } }, additionalProperties: false } },
  { name: "container_remove_test", description: "Remove an isolated test deployment created by container_test.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string", pattern: "^test-[a-z0-9-]+$" } }, additionalProperties: false } },
];

function validate(name) {
  if (!/^[a-z0-9][a-z0-9-]*$/.test(name)) throw new Error("invalid deployment name");
  return name;
}

async function deployment(name) {
  validate(name);
  return k8s(`/apis/apps/v1/namespaces/${namespace}/deployments/${name}`);
}

async function status() {
  const data = await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments`);
  return data.items.map((item) => ({
    deployment: item.metadata.name,
    images: item.spec.template.spec.containers.map((container) => ({ name: container.name, image: container.image })),
    replicas: { desired: item.spec.replicas ?? 1, ready: item.status.readyReplicas ?? 0, available: item.status.availableReplicas ?? 0 },
    generation: item.metadata.generation,
  }));
}

async function podFor(name) {
  const pods = await k8s(`/api/v1/namespaces/${namespace}/pods?labelSelector=app%3D${encodeURIComponent(name)}`);
  const pod = pods.items.find((item) => ["Running", "Succeeded"].includes(item.status.phase)) || pods.items[0];
  if (!pod) throw new Error(`no pod found for deployment ${name}`);
  return pod;
}

async function testDeployment(image, timeoutSeconds) {
  const suffix = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  const name = `test-${suffix}`;
  await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments`, { method: "POST", body: JSON.stringify({
    apiVersion: "apps/v1", kind: "Deployment", metadata: { name, labels: { "container-manager/test": "true" } },
    spec: { replicas: 1, revisionHistoryLimit: 0, selector: { matchLabels: { app: name } }, template: { metadata: { labels: { app: name, "container-manager/test": "true" } }, spec: { containers: [{ name: "test", image, imagePullPolicy: "Never" }] } } },
  }) });
  const deadline = Date.now() + Math.min(300, Math.max(5, Number(timeoutSeconds || 90))) * 1000;
  while (Date.now() < deadline) {
    const pod = await podFor(name).catch(() => null);
    if (pod?.status?.phase === "Running") return { deployment: name, image, pod: pod.metadata.name, status: "running", next: "inspect with container_health/container_logs, then call container_remove_test" };
    if (pod?.status?.phase === "Failed") return { deployment: name, image, pod: pod.metadata.name, status: "failed", reason: pod.status.reason || "pod failed", next: "inspect with container_logs, then call container_remove_test" };
    await sleep(2000);
  }
  return { deployment: name, image, status: "starting", next: "poll with container_health, then call container_remove_test" };
}

async function call(name, args) {
  if (name === "container_status") return status();
  if (name === "container_test") return testDeployment(args.image, args.timeout_seconds);
  if (name === "container_remove_test") {
    if (!/^test-[a-z0-9-]+$/.test(args.deployment)) throw new Error("only test-* deployments can be removed");
    const test = await deployment(args.deployment);
    if (test.metadata.labels?.["container-manager/test"] !== "true") throw new Error("deployment is not owned by container-manager test");
    await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments/${args.deployment}`, { method: "DELETE" });
    return { deployment: args.deployment, removed: true };
  }
  const dep = await deployment(args.deployment);
  if (name === "container_health") {
    const pod = await podFor(args.deployment);
    return { deployment: args.deployment, phase: pod.status.phase, ready: dep.status.readyReplicas ?? 0, desired: dep.spec.replicas ?? 1, pod: pod.metadata.name, conditions: pod.status.conditions || [] };
  }
  if (name === "container_logs") {
    const pod = await podFor(args.deployment);
    const tail = Math.max(1, Math.min(500, Number(args.tail_lines || 100)));
    return { deployment: args.deployment, pod: pod.metadata.name, logs: await k8s(`/api/v1/namespaces/${namespace}/pods/${pod.metadata.name}/log?tailLines=${tail}`) };
  }
  if (name === "container_restart") {
    const annotations = { ...(dep.spec.template.metadata?.annotations || {}), "container-manager/restarted-at": new Date().toISOString() };
    await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments/${args.deployment}`, { method: "PATCH", headers: { "Content-Type": "application/strategic-merge-patch+json" }, body: JSON.stringify({ spec: { template: { metadata: { annotations } } } }) });
    return { deployment: args.deployment, restarted: true };
  }
  if (name === "container_upgrade") {
    const target = args.container || dep.spec.template.spec.containers[0].name;
    const containers = dep.spec.template.spec.containers.map((container) => container.name === target ? { ...container, image: args.image } : container);
    if (!dep.spec.template.spec.containers.some((container) => container.name === target)) throw new Error(`container not found: ${target}`);
    const annotations = { ...(dep.spec.template.metadata?.annotations || {}), "container-manager/previous-image": dep.spec.template.spec.containers.find((container) => container.name === target).image, "container-manager/previous-container": target, "container-manager/upgraded-at": new Date().toISOString() };
    await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments/${args.deployment}`, { method: "PATCH", headers: { "Content-Type": "application/strategic-merge-patch+json" }, body: JSON.stringify({ spec: { template: { metadata: { annotations }, spec: { containers } } } }) });
    return { deployment: args.deployment, container: target, image: args.image, previous_image: annotations["container-manager/previous-image"], rollout: "started" };
  }
  if (name === "container_rollback") {
    const annotations = dep.spec.template.metadata?.annotations || {};
    const target = annotations["container-manager/previous-container"];
    const image = annotations["container-manager/previous-image"];
    if (!target || !image) throw new Error("no previous image recorded for this deployment");
    const containers = dep.spec.template.spec.containers.map((container) => container.name === target ? { ...container, image } : container);
    await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments/${args.deployment}`, { method: "PATCH", headers: { "Content-Type": "application/strategic-merge-patch+json" }, body: JSON.stringify({ spec: { template: { spec: { containers }, metadata: { annotations: { ...annotations, "container-manager/rolled-back-at": new Date().toISOString() } } } } }) });
    return { deployment: args.deployment, container: target, image, rollback: "started" };
  }
  throw new Error(`unknown tool: ${name}`);
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let request;
  try { request = JSON.parse(line); } catch { continue; }
  if (request.method === "initialize") { send({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: request.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "container-manager", version: "0.1.0" } } }); continue; }
  if (request.method === "notifications/initialized") continue;
  if (request.method === "tools/list") { send({ jsonrpc: "2.0", id: request.id, result: { tools } }); continue; }
  if (request.method === "tools/call") {
    try { send(text(request.id, await call(request.params.name, request.params.arguments || {}))); }
    catch (error) { send(fail(request.id, error.message)); }
  }
}
