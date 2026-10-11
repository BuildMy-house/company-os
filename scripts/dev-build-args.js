// Build-arg assembly for the dev-deploy-poller's buildmyhouse-app image.
//
// The app's Vite build bakes client telemetry config (VITE_AXIOM_*) into the
// public JS bundle, so the dev image needs them at build time. Values come
// from Infisical (project buildmyhouse-app, env dev) via Universal Auth and
// are passed to buildAndPush as build_args. Fail-closed: any missing or
// invalid value throws. Values are NEVER included in error messages or logs.
const INFISICAL_PROJECT_ID = "58b43b81-effb-4392-a937-46f2448efb78";
const INFISICAL_ENV = "dev";
const DEFAULT_HOST = "https://eu.infisical.com";

export const AXIOM_KEYS = ["VITE_AXIOM_TOKEN", "VITE_AXIOM_DATASET", "VITE_AXIOM_ENDPOINT"];

// Mirrors builder-manager-mcp.js validateBuildArgs, but never echoes the value.
const BUILD_ARG_VALUE_RE = /^[a-zA-Z0-9 ._/:=-]*$/;

export function requireInfisicalEnv(env = process.env) {
  const clientId = env.INFISICAL_UNIVERSAL_AUTH_CLIENT_ID;
  const clientSecret = env.INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET;
  const missing = [];
  if (!clientId) missing.push("INFISICAL_UNIVERSAL_AUTH_CLIENT_ID");
  if (!clientSecret) missing.push("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET");
  if (missing.length) {
    throw new Error(`cannot fetch Axiom build args from Infisical: missing env ${missing.join(", ")} (see docs/TELEMETRY.md)`);
  }
  return {
    host: (env.INFISICAL_HOST_URL || DEFAULT_HOST).replace(/\/+$/, ""),
    projectId: env.INFISICAL_PROJECT_ID || INFISICAL_PROJECT_ID,
    environment: env.INFISICAL_ENV || INFISICAL_ENV,
    clientId,
    clientSecret,
  };
}

// Pure: turns Infisical's secret list into a validated build_args object.
export function assembleAxiomBuildArgs(secrets) {
  const byKey = new Map((secrets || []).map((s) => [s.secretKey, s.secretValue]));
  const missing = AXIOM_KEYS.filter((key) => !byKey.get(key));
  if (missing.length) {
    throw new Error(`Infisical (${INFISICAL_ENV}) is missing required secret(s): ${missing.join(", ")}`);
  }
  const out = {};
  for (const key of AXIOM_KEYS) {
    const value = byKey.get(key);
    if (typeof value !== "string" || !BUILD_ARG_VALUE_RE.test(value)) {
      throw new Error(`Infisical secret ${key} has characters not allowed in a build arg (value not shown)`);
    }
    out[key] = value;
  }
  return out;
}

async function jsonOrThrow(response, what) {
  let data = {};
  try { data = await response.json(); } catch { /* non-JSON body */ }
  if (!response.ok) {
    throw new Error(`Infisical ${what} failed (HTTP ${response.status}): ${data.message || "no message"}`);
  }
  return data;
}

export async function fetchAxiomBuildArgs({ env = process.env, fetchImpl = fetch } = {}) {
  const cfg = requireInfisicalEnv(env);
  const login = await jsonOrThrow(
    await fetchImpl(`${cfg.host}/api/v1/auth/universal-auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clientId: cfg.clientId, clientSecret: cfg.clientSecret }),
    }),
    "login",
  );
  if (!login.accessToken) throw new Error("Infisical login returned no access token");

  const url = new URL(`${cfg.host}/api/v3/secrets/raw`);
  url.searchParams.set("workspaceId", cfg.projectId);
  url.searchParams.set("environment", cfg.environment);
  url.searchParams.set("secretPath", "/");
  const listed = await jsonOrThrow(
    await fetchImpl(url, { headers: { Authorization: `Bearer ${login.accessToken}` } }),
    "secrets fetch",
  );
  return assembleAxiomBuildArgs(listed.secrets);
}
