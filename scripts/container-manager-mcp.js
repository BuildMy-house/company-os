#!/usr/bin/env node

// Kubernetes-backed local container manager. It deliberately manages only the
// company-ops namespace; building images stays outside the pod. Pulling does
// not: images pushed to the in-cluster registry (k8s/registry.yaml, reachable
// in-cluster as registry.company-ops.svc.cluster.local:5000) are pulled
// normally by kubelet — pass a full "registry.company-ops.svc.cluster.local:
// 5000/<repo>:<tag>" reference to container_upgrade/container_test.
import { existsSync, readFileSync } from "node:fs";
import assert from "node:assert/strict";
import https from "node:https";
import readline from "node:readline";

const namespace = "company-ops";
const host = process.env.KUBERNETES_SERVICE_HOST;
const port = process.env.KUBERNETES_SERVICE_PORT || "443";
const tokenPath = "/var/run/secrets/kubernetes.io/serviceaccount/token";
const caPath = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt";
const base = `https://${host}:${port}`;

// Read the ServiceAccount token/CA fresh on every request rather than once at
// module load. This process is spawned once by Hermes and kept as a
// long-lived stdio subprocess for the life of the pod; Kubernetes rotates
// projected SA tokens on its own schedule (roughly hourly), and there is also
// a startup race where kubelet may not have finished mounting the projected
// volume at the exact moment this module is first imported. Caching the
// token/ca as top-level consts meant either failure mode permanently poisons
// every future tool call for the rest of the pod's lifetime with a
// misleading "unavailable outside a k3s pod" error, even though the files on
// disk are actually fine — confirmed by a fresh process reading the same
// path succeeding immediately. builder-manager-mcp.js already reads its
// token per-call; this brings container-manager-mcp.js in line with that.
function readToken() {
  return existsSync(tokenPath) ? readFileSync(tokenPath, "utf8").trim() : null;
}

function readCa() {
  return existsSync(caPath) ? readFileSync(caPath) : null;
}

async function api(path, options = {}) {
  const token = readToken();
  const ca = readCa();
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

function candidateTemplate(source, name, image, containerName) {
  const template = JSON.parse(JSON.stringify(source));
  template.metadata.labels = { ...(template.metadata.labels || {}), app: name, "container-manager/test": "true" };
  const target = containerName || template.spec.containers[0]?.name;
  const selected = template.spec.containers.find((container) => container.name === target);
  if (!selected) throw new Error(`container not found in template: ${target}`);
  selected.image = image;
  selected.imagePullPolicy = "Always";
  // Keep non-secret runtime config/probes, but don't register or authorize a second worker.
  const secretVolumes = new Set((template.spec.volumes || []).filter((volume) => volume.secret || volume.projected).map((volume) => volume.name));
  const hostVolumes = new Set((template.spec.volumes || []).filter((volume) => volume.hostPath).map((volume) => volume.name));
  template.spec.volumes = (template.spec.volumes || []).flatMap((volume) => {
    if (secretVolumes.has(volume.name) || hostVolumes.has(volume.name)) return [];
    return volume.persistentVolumeClaim ? [{ name: volume.name, emptyDir: {} }] : [volume];
  });
  for (const container of template.spec.containers) {
    container.env = (container.env || []).filter((entry) => !["HIVE_URL", "HIVE_AGENT_ID"].includes(entry.name) && !entry.valueFrom?.secretKeyRef);
    container.envFrom = (container.envFrom || []).filter((source) => !source.secretRef);
    container.volumeMounts = (container.volumeMounts || []).filter((mount) => !secretVolumes.has(mount.name) && !hostVolumes.has(mount.name));
  }
  template.spec.serviceAccountName = "default";
  template.spec.automountServiceAccountToken = false;
  return template;
}

const tools = [
  { name: "container_status", description: "List local company-ops deployments, images, and replica status.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "container_health", description: "Check readiness and pod health for one local deployment.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string" } }, additionalProperties: false } },
  { name: "container_logs", description: "Read logs from the current pod for one local deployment.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string" }, tail_lines: { type: "integer", minimum: 1, maximum: 500, default: 100 } }, additionalProperties: false } },
  { name: "container_upgrade", description: "Change a local deployment to a pullable image and wait for readiness; restore the recorded previous image if rollout fails or times out. For self-upgrades, use an independent manager that survives the target pod restart.", inputSchema: { type: "object", required: ["deployment", "image"], properties: { deployment: { type: "string" }, image: { type: "string" }, container: { type: "string", default: "" }, timeout_seconds: { type: "integer", minimum: 30, maximum: 900, default: 300 } }, additionalProperties: false } },
  { name: "container_restart", description: "Restart one local deployment without changing its image.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string" } }, additionalProperties: false } },
  { name: "container_rollback", description: "Roll one local deployment back to the image saved by its last container_upgrade and wait for readiness.", inputSchema: { type: "object", required: ["deployment"], properties: { deployment: { type: "string" } }, additionalProperties: false } },
  { name: "container_test", description: "Start an isolated temporary test deployment from a pullable image. Set template_deployment to copy an agent deployment's non-secret runtime config and readiness probes while isolating persistent data.", inputSchema: { type: "object", required: ["image"], properties: { image: { type: "string" }, template_deployment: { type: "string", enum: ["engineering-agent", "engineering-opencode", "engineering-opencode-direct", "hermes-gateway", "browser-adversary"] }, container: { type: "string", default: "" }, timeout_seconds: { type: "integer", minimum: 5, maximum: 300, default: 300 } }, additionalProperties: false } },
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

async function testDeployment(image, timeoutSeconds, templateName, containerName) {
  const suffix = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  const name = `test-${suffix}`;
  let template;
  if (templateName) {
    if (!["engineering-agent", "engineering-opencode", "engineering-opencode-direct", "hermes-gateway", "browser-adversary"].includes(templateName)) throw new Error("unsupported template deployment");
    const source = await deployment(templateName);
    template = candidateTemplate(source.spec.template, name, image, containerName);
  } else {
    template = { metadata: { labels: { app: name, "container-manager/test": "true" } }, spec: { containers: [{ name: "test", image, imagePullPolicy: "Always" }] } };
  }
  await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments`, { method: "POST", body: JSON.stringify({
    apiVersion: "apps/v1", kind: "Deployment", metadata: { name, labels: { "container-manager/test": "true" } },
    spec: { replicas: 1, revisionHistoryLimit: 0, selector: { matchLabels: { app: name } }, template },
  }) });
  const deadline = Date.now() + Math.min(300, Math.max(5, Number(timeoutSeconds || 300))) * 1000;
  while (Date.now() < deadline) {
    const current = await deployment(name).catch(() => null);
    if ((current?.status?.readyReplicas || 0) >= 1) return { deployment: name, image, status: "ready", ready: current.status.readyReplicas, next: "inspect with container_health/container_logs, then call container_remove_test" };
    const pod = await podFor(name).catch(() => null);
    if (pod?.status?.phase === "Failed") return { deployment: name, image, pod: pod.metadata.name, status: "failed", reason: pod.status.reason || "pod failed", next: "inspect with container_logs, then call container_remove_test" };
    await sleep(2000);
  }
  return { deployment: name, image, status: "timeout", next: "inspect with container_health/container_logs, then call container_remove_test" };
}

async function waitForRollout(name, generation, timeoutSeconds) {
  const deadline = Date.now() + Math.min(900, Math.max(30, Number(timeoutSeconds || 300))) * 1000;
  while (Date.now() < deadline) {
    const current = await deployment(name);
    const status = current.status || {};
    const desired = current.spec.replicas ?? 1;
    const ready = status.readyReplicas ?? 0;
    const available = status.availableReplicas ?? 0;
    const progressing = (status.conditions || []).find((condition) => condition.type === "Progressing");
    if (rolloutReady(current, generation)) return { status: "ready", generation, ready, desired };
    if (progressing?.status === "False" && progressing.reason === "ProgressDeadlineExceeded") {
      return { status: "failed", reason: progressing.message || progressing.reason, ready, desired };
    }
    await sleep(2000);
  }
  const current = await deployment(name);
  return { status: "timeout", generation, ready: current.status?.readyReplicas ?? 0, desired: current.spec.replicas ?? 1 };
}

function rolloutReady(current, generation) {
  const status = current.status || {};
  const desired = current.spec.replicas ?? 1;
  return (status.observedGeneration ?? 0) >= generation
    && (status.updatedReplicas ?? 0) === desired
    && (status.readyReplicas ?? 0) === desired
    && (status.availableReplicas ?? 0) === desired;
}

function shouldRollback(rollout) {
  return rollout.status !== "ready";
}

function selfTest() {
  const candidate = candidateTemplate({
    metadata: { labels: { app: "engineering-agent" } },
    spec: {
      serviceAccountName: "company-ops",
      volumes: [{ name: "builder-manager-token", secret: {} }, { name: "agent-home", emptyDir: {} }, { name: "hermes-data", persistentVolumeClaim: { claimName: "hermes-data" } }, { name: "host", hostPath: { path: "/tmp" } }],
      containers: [{ name: "engineering-agent", image: "old", env: [{ name: "HIVE_URL" }, { name: "API_KEY" }, { name: "SECRET", valueFrom: { secretKeyRef: { name: "company-ops-secrets", key: "TOKEN" } } }], envFrom: [{ secretRef: { name: "company-ops-secrets" } }, { configMapRef: { name: "agent-config" } }], volumeMounts: [{ name: "builder-manager-token" }, { name: "agent-home" }, { name: "hermes-data", mountPath: "/opt/data" }, { name: "host", mountPath: "/host" }], readinessProbe: { tcpSocket: { port: 8000 } } }],
    },
  }, "test-candidate", "registry/candidate@sha256:abc", "engineering-agent");
  assert.equal(candidate.spec.containers[0].image, "registry/candidate@sha256:abc");
  assert.deepEqual(candidate.spec.containers[0].readinessProbe, { tcpSocket: { port: 8000 } });
  assert.deepEqual(candidate.spec.containers[0].env.map(({ name }) => name), ["API_KEY"]);
  assert.deepEqual(candidate.spec.containers[0].envFrom, [{ configMapRef: { name: "agent-config" } }]);
  assert.deepEqual(candidate.spec.containers[0].volumeMounts.map(({ name }) => name), ["agent-home", "hermes-data"]);
  assert.deepEqual(candidate.spec.volumes, [{ name: "agent-home", emptyDir: {} }, { name: "hermes-data", emptyDir: {} }]);
  assert.equal(candidate.spec.serviceAccountName, "default");
  assert.equal(candidate.spec.automountServiceAccountToken, false);
  assert.equal(rolloutReady({ spec: { replicas: 1 }, status: { observedGeneration: 2, updatedReplicas: 1, readyReplicas: 1, availableReplicas: 1 } }, 2), true);
  assert.equal(rolloutReady({ spec: { replicas: 1 }, status: { observedGeneration: 1, updatedReplicas: 1, readyReplicas: 1, availableReplicas: 1 } }, 2), false);
  assert.equal(shouldRollback({ status: "ready" }), false);
  assert.equal(shouldRollback({ status: "timeout" }), true);
}

async function call(name, args) {
  if (name === "container_status") return status();
  if (name === "container_test") return testDeployment(args.image, args.timeout_seconds, args.template_deployment, args.container);
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
    const updated = await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments/${args.deployment}`, { method: "PATCH", headers: { "Content-Type": "application/strategic-merge-patch+json" }, body: JSON.stringify({ spec: { template: { metadata: { annotations }, spec: { containers } } } }) });
    const rollout = await waitForRollout(args.deployment, updated.metadata.generation, args.timeout_seconds);
    let rollback = null;
    if (shouldRollback(rollout)) {
      const previousImage = annotations["container-manager/previous-image"];
      const previousContainers = containers.map((container) => container.name === target ? { ...container, image: previousImage } : container);
      const rollbackAnnotations = { ...annotations, "container-manager/rolled-back-at": new Date().toISOString() };
      const restored = await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments/${args.deployment}`, { method: "PATCH", headers: { "Content-Type": "application/strategic-merge-patch+json" }, body: JSON.stringify({ spec: { template: { metadata: { annotations: rollbackAnnotations }, spec: { containers: previousContainers } } } }) });
      rollback = await waitForRollout(args.deployment, restored.metadata.generation, 300);
    }
    return { deployment: args.deployment, container: target, image: args.image, previous_image: annotations["container-manager/previous-image"], rollout, rollback };
  }
  if (name === "container_rollback") {
    const annotations = dep.spec.template.metadata?.annotations || {};
    const target = annotations["container-manager/previous-container"];
    const image = annotations["container-manager/previous-image"];
    if (!target || !image) throw new Error("no previous image recorded for this deployment");
    const containers = dep.spec.template.spec.containers.map((container) => container.name === target ? { ...container, image } : container);
    const updated = await k8s(`/apis/apps/v1/namespaces/${namespace}/deployments/${args.deployment}`, { method: "PATCH", headers: { "Content-Type": "application/strategic-merge-patch+json" }, body: JSON.stringify({ spec: { template: { spec: { containers }, metadata: { annotations: { ...annotations, "container-manager/rolled-back-at": new Date().toISOString() } } } } }) });
    const rollout = await waitForRollout(args.deployment, updated.metadata.generation, 300);
    return { deployment: args.deployment, container: target, image, rollback: rollout };
  }
  throw new Error(`unknown tool: ${name}`);
}

if (process.argv.includes("--self-test")) {
  selfTest();
  console.log("container-manager self-test passed");
  process.exit(0);
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let request;
  try { request = JSON.parse(line); } catch { continue; }
  if (request.method === "initialize") { send({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: request.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "container-manager", version: "0.1.0" } } }); continue; }
  if (request.method === "notifications/initialized") continue;
  // MCP's optional "ping" utility: Hermes's keepalive probe (tools/mcp_tool_health.py
  // _keepalive_probe) sends this on every fresh transport connection and expects an
  // immediate empty result. Leaving it unhandled meant it fell through with zero
  // response (not even a JSON-RPC error) instead of Hermes's own client cleanly
  // detecting "method not found" and falling back to list_tools for keepalives --
  // the keepalive hung for the full 30s RPC timeout, logged as a TimeoutError, and
  // forced a reconnect; because the "ping unsupported" fallback is latched per
  // transport connection, each reconnect wiped that learned state and the next
  // connection's first keepalive hit the same 30s timeout again -- a permanent
  // connected/degraded/parked flap instead of one-time self-correction.
  if (request.method === "ping") { send({ jsonrpc: "2.0", id: request.id, result: {} }); continue; }
  if (request.method === "tools/list") { send({ jsonrpc: "2.0", id: request.id, result: { tools } }); continue; }
  if (request.method === "tools/call") {
    try { send(text(request.id, await call(request.params.name, request.params.arguments || {}))); }
    catch (error) { send(fail(request.id, error.message)); }
  }
}
