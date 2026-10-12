import assert from "node:assert/strict";
import { test } from "node:test";
import { assembleAxiomBuildArgs, fetchAxiomBuildArgs, requireInfisicalEnv } from "./dev-build-args.js";

const good = [
  { secretKey: "VITE_AXIOM_TOKEN", secretValue: "xaat-1234-abcd" },
  { secretKey: "VITE_AXIOM_DATASET", secretValue: "buildmy-house-telemetry" },
  { secretKey: "VITE_AXIOM_ENDPOINT", secretValue: "https://eu-central-1.aws.edge.axiom.co" },
  { secretKey: "JWT_SECRET", secretValue: "unrelated" },
];

test("assembles only the three VITE_AXIOM_* keys", () => {
  const args = assembleAxiomBuildArgs(good);
  assert.deepEqual(Object.keys(args).sort(), ["VITE_AXIOM_DATASET", "VITE_AXIOM_ENDPOINT", "VITE_AXIOM_TOKEN"]);
});

test("missing key fails loudly naming the key, not values", () => {
  assert.throws(() => assembleAxiomBuildArgs(good.slice(1, 3)), /VITE_AXIOM_TOKEN/);
  assert.throws(() => assembleAxiomBuildArgs([{ secretKey: "VITE_AXIOM_TOKEN", secretValue: "" }, ...good.slice(1)]), /VITE_AXIOM_TOKEN/);
});

test("invalid value error never echoes the value", () => {
  const bad = [{ secretKey: "VITE_AXIOM_TOKEN", secretValue: "sekret$value" }, ...good.slice(1)];
  assert.throws(() => assembleAxiomBuildArgs(bad), (e) => /VITE_AXIOM_TOKEN/.test(e.message) && !e.message.includes("sekret"));
});

test("missing Infisical credentials fail with env var names", () => {
  assert.throws(() => requireInfisicalEnv({}), /INFISICAL_UNIVERSAL_AUTH_CLIENT_ID.*INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET/);
});

test("fetch flow logs in then lists dev secrets (mocked)", async () => {
  const calls = [];
  const fetchImpl = async (url, opts) => {
    calls.push(String(url));
    if (String(url).includes("/login")) return { ok: true, status: 200, json: async () => ({ accessToken: "t" }) };
    return { ok: true, status: 200, json: async () => ({ secrets: good }) };
  };
  const args = await fetchAxiomBuildArgs({ env: { INFISICAL_UNIVERSAL_AUTH_CLIENT_ID: "a", INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET: "b" }, fetchImpl });
  assert.equal(args.VITE_AXIOM_DATASET, "buildmy-house-telemetry");
  assert.ok(calls[1].includes("workspaceId=58b43b81-effb-4392-a937-46f2448efb78") && calls[1].includes("environment=dev"));
});

test("Infisical HTTP failure throws", async () => {
  const fetchImpl = async () => ({ ok: false, status: 401, json: async () => ({ message: "bad creds" }) });
  await assert.rejects(fetchAxiomBuildArgs({ env: { INFISICAL_UNIVERSAL_AUTH_CLIENT_ID: "a", INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET: "b" }, fetchImpl }), /login failed \(HTTP 401\)/);
});
