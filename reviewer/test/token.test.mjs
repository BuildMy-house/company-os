import test from "node:test";
import assert from "node:assert/strict";
import { createPublicKey, createVerify, generateKeyPairSync } from "node:crypto";
import { appJwt, installationToken, normalizePem } from "../lib/token.mjs";

const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048, privateKeyEncoding: { type: "pkcs8", format: "pem" }, publicKeyEncoding: { type: "spki", format: "pem" } });

test("appJwt is a valid RS256 JWT for the app id with a 10 minute max lifetime", () => {
  const [h, p, s] = appJwt(42, privateKey, 1000).split(".");
  assert.deepEqual(JSON.parse(Buffer.from(h, "base64url")), { alg: "RS256", typ: "JWT" });
  const claims = JSON.parse(Buffer.from(p, "base64url"));
  assert.equal(claims.iss, "42");
  assert.ok(claims.exp - claims.iat <= 600);
  assert.ok(createVerify("RSA-SHA256").update(`${h}.${p}`).verify(createPublicKey(publicKey), s, "base64url"));
});

test("normalizePem restores single-line keys with literal \\n", () => {
  assert.equal(normalizePem("-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"), "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----");
  assert.equal(normalizePem(privateKey), privateKey);
});

test("installationToken posts to the installation endpoint and returns the token", async () => {
  let seen;
  const fetchImpl = async (url, init) => { seen = { url, init }; return { ok: true, status: 201, text: async () => JSON.stringify({ token: "ghs_x" }) }; };
  assert.equal(await installationToken({ appId: 1, installationId: 7, privateKey, fetchImpl }), "ghs_x");
  assert.equal(seen.url, "https://api.github.com/app/installations/7/access_tokens");
  assert.match(seen.init.headers.Authorization, /^Bearer ey/);
});

test("installationToken has no defaults and fails closed", async () => {
  await assert.rejects(installationToken({ appId: 1, installationId: 7 }), /required/);
  await assert.rejects(installationToken({ appId: 1, installationId: 7, privateKey: "nope" }), /PEM/);
  const bad = async () => ({ ok: false, status: 401, text: async () => JSON.stringify({ message: "Bad credentials" }) });
  await assert.rejects(installationToken({ appId: 1, installationId: 7, privateKey, fetchImpl: bad }), /401 Bad credentials/);
});
