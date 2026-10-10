#!/usr/bin/env node

// CO-DEV3: polls BuildMy-house/app's `dev` branch for new commits and, when
// one is found, builds all 3 images (app, mcp, luxcore) via the same
// ephemeral in-cluster BuildKit Job path builder-manager-mcp.js already
// uses for Hermes/engineering-agent builds, rolls out the 3
// buildmyhouse-dev Deployments to the new digest-pinned images, and only
// records the new SHA as "deployed" once a live /healthz check passes.
//
// No inbound webhook is reachable by this cluster, so this is a polling
// design (run on a schedule by k8s/buildmyhouse-dev-deployer-cronjob.yaml),
// not a push-triggered one. Reuses buildJobManifest/buildAndPush directly
// (imported, not via MCP/mcp-proxy) per PLAN.md's CO-DEV3 note — do not
// reimplement that logic here.
//
// Exit codes: 0 on NOOP (no new commit) or PASS (deployed and verified),
// 1 on FAIL (new commit found but build/rollout/healthcheck did not
// succeed) so a CronJob failure is visible and retried on the next poll —
// the ConfigMap is only updated on PASS.
import https from "node:https";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { buildAndPush, k8sRequest, NAMESPACE as BUILDER_NAMESPACE } from "./builder-manager-mcp.js";

const execFileAsync = promisify(execFile);
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

const DEV_NAMESPACE = "buildmyhouse-dev";
const STATE_CONFIGMAP = "buildmyhouse-dev-deploy-state";
const REPO = "BuildMy-house/app";
const BRANCH = "dev";

const IMAGES = [
  { repo: "buildmyhouse-app", dockerfile: "Dockerfile", deployment: "buildmyhouse-dev-app", container: "buildmyhouse-dev-app" },
  { repo: "buildmyhouse-mcp", dockerfile: "mcp/Dockerfile", deployment: "buildmyhouse-dev-mcp", container: "buildmyhouse-dev-mcp" },
  { repo: "buildmyhouse-luxcore", dockerfile: "luxcore/Dockerfile", deployment: "buildmyhouse-dev-luxcore", container: "buildmyhouse-dev-luxcore" },
];

function log(verdict, message) {
  // Single clear one-line verdict, as required by PLAN.md's CO-DEV3 DoD.
  process.stdout.write(`[dev-deploy-poller] ${verdict}: ${message}\n`);
}

async function mintGithubAppToken() {
  const { stdout } = await execFileAsync("node", [path.join(SCRIPT_DIR, "github-app-token.js")]);
  const token = stdout.trim();
  if (!token) throw new Error("github-app-token.js produced no token");
  return token;
}

function githubRequest(apiPath, token) {
  return new Promise((resolve, reject) => {
    const request = https.request(
      {
        method: "GET",
        hostname: "api.github.com",
        path: apiPath,
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "buildmyhouse-dev-deploy-poller",
          "X-GitHub-Api-Version": "2022-11-28",
        },
      },
      (response) => {
        let text = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => { text += chunk; });
        response.on("end", () => {
          let parsed;
          try { parsed = JSON.parse(text); } catch { parsed = text; }
          if ((response.statusCode || 500) >= 400) {
            return reject(new Error(`GitHub API GET ${apiPath} -> ${response.statusCode}: ${text}`));
          }
          resolve(parsed);
        });
      },
    );
    request.on("error", reject);
    request.end();
  });
}

async function resolveLatestDevSha(token) {
  const commit = await githubRequest(`/repos/${REPO}/commits/${BRANCH}`, token);
  if (!commit?.sha || !/^[0-9a-f]{40}$/.test(commit.sha)) {
    throw new Error(`unexpected GitHub response resolving ${REPO}#${BRANCH}: ${JSON.stringify(commit).slice(0, 300)}`);
  }
  return commit.sha;
}

async function readDeployState() {
  try {
    const cm = await k8sRequest("GET", `/api/v1/namespaces/${DEV_NAMESPACE}/configmaps/${STATE_CONFIGMAP}`);
    return cm.data?.lastDeployedSha || null;
  } catch (error) {
    if (/-> 404/.test(error.message)) return null;
    throw error;
  }
}

async function writeDeployState(sha) {
  const manifest = {
    apiVersion: "v1",
    kind: "ConfigMap",
    metadata: { name: STATE_CONFIGMAP, namespace: DEV_NAMESPACE },
    data: { lastDeployedSha: sha, updatedAt: new Date().toISOString() },
  };
  try {
    await k8sRequest("POST", `/api/v1/namespaces/${DEV_NAMESPACE}/configmaps`, manifest);
  } catch (error) {
    if (!/-> 409/.test(error.message)) throw error;
    await k8sRequest(
      "PUT",
      `/api/v1/namespaces/${DEV_NAMESPACE}/configmaps/${STATE_CONFIGMAP}`,
      { ...manifest, metadata: { ...manifest.metadata, resourceVersion: (await k8sRequest("GET", `/api/v1/namespaces/${DEV_NAMESPACE}/configmaps/${STATE_CONFIGMAP}`)).metadata.resourceVersion } },
    );
  }
}

// JSON strategic-merge patch of exactly one container's image, by name —
// never a blind positional patch, so a future container-list reordering
// can't silently patch the wrong container.
async function patchDeploymentImage(deploymentName, containerName, image) {
  const patch = { spec: { template: { spec: { containers: [{ name: containerName, image }] } } } };
  await k8sRequest(
    "PATCH",
    `/apis/apps/v1/namespaces/${DEV_NAMESPACE}/deployments/${deploymentName}`,
    patch,
    "application/strategic-merge-patch+json",
  );
}

async function waitForRollout(deploymentName, deadlineMs) {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    const deployment = await k8sRequest("GET", `/apis/apps/v1/namespaces/${DEV_NAMESPACE}/deployments/${deploymentName}`);
    const spec = deployment.spec || {};
    const status = deployment.status || {};
    const desired = spec.replicas ?? 1;
    if (
      status.observedGeneration >= deployment.metadata.generation
      && (status.updatedReplicas || 0) >= desired
      && (status.availableReplicas || 0) >= desired
      && (status.replicas || 0) === (status.updatedReplicas || 0)
    ) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  return false;
}

async function checkHealthz() {
  const http = await import("node:http");
  return new Promise((resolve) => {
    const request = http.request(
      { method: "GET", hostname: `buildmyhouse-dev-app.${DEV_NAMESPACE}.svc.cluster.local`, port: 3000, path: "/healthz" },
      (response) => {
        let body = "";
        response.on("data", (chunk) => { body += chunk; });
        response.on("end", () => resolve({ ok: response.statusCode === 200, status: response.statusCode, body }));
      },
    );
    request.on("error", (error) => resolve({ ok: false, error: error.message }));
    request.setTimeout(10000, () => { request.destroy(new Error("timeout")); });
    request.end();
  });
}

async function main() {
  const token = await mintGithubAppToken();
  const latestSha = await resolveLatestDevSha(token);
  const lastDeployedSha = await readDeployState();

  if (lastDeployedSha === latestSha) {
    log("NOOP", `${REPO}#${BRANCH} unchanged at ${latestSha}`);
    return;
  }

  log("BUILDING", `${REPO}#${BRANCH} moved ${lastDeployedSha || "(none)"} -> ${latestSha}`);
  const shortSha = latestSha.slice(0, 7);
  const tag = `dev-${shortSha}`;
  const contextRef = `https://github.com/${REPO}.git#${latestSha}`;

  const built = [];
  for (const image of IMAGES) {
    const result = await buildAndPush({
      context_ref: contextRef,
      dockerfile_path: image.dockerfile,
      image_repo: image.repo,
      image_tag: tag,
      timeout_seconds: 900,
    });
    built.push({ ...image, image: result.image });
  }

  for (const image of built) {
    await patchDeploymentImage(image.deployment, image.container, image.image);
  }

  for (const image of built) {
    const rolledOut = await waitForRollout(image.deployment, 180000);
    if (!rolledOut) {
      log("FAIL", `${image.deployment} did not finish rolling out within 180s`);
      process.exitCode = 1;
      return;
    }
  }

  const health = await checkHealthz();
  if (!health.ok) {
    log("FAIL", `post-rollout /healthz check failed: ${JSON.stringify(health)}`);
    process.exitCode = 1;
    return;
  }

  await writeDeployState(latestSha);
  log("PASS", `deployed ${latestSha} (${built.map((b) => b.image).join(", ")}), /healthz 200`);
}

main().catch((error) => {
  log("FAIL", error.stack || error.message);
  process.exitCode = 1;
});
