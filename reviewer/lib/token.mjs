// GitHub App installation token minter for the reviewer. Zero dependencies (node:crypto + fetch).
// Deliberately has NO default App id, installation id or key path: the reviewer must run as its
// own App, never fall back to the engineering App's identity.
import { createSign } from "node:crypto";

export function appJwt(appId, privateKeyPem, now = Math.floor(Date.now() / 1000)) {
  const b64 = (v) => Buffer.from(JSON.stringify(v)).toString("base64url");
  const msg = `${b64({ alg: "RS256", typ: "JWT" })}.${b64({ iat: now - 60, exp: now + 540, iss: String(appId) })}`;
  return `${msg}.${createSign("RSA-SHA256").update(msg).sign(privateKeyPem, "base64url")}`;
}

// Env keys are single-line in Infisical/k8s more often than not; accept literal "\n" sequences.
export function normalizePem(raw) {
  return String(raw || "").includes("\\n") && !String(raw).includes("\n") ? String(raw).replace(/\\n/g, "\n") : String(raw || "");
}

export async function installationToken({ appId, installationId, privateKey, fetchImpl = fetch }) {
  if (!appId || !installationId || !privateKey) throw new Error("GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID and GITHUB_APP_PRIVATE_KEY are all required");
  const pem = normalizePem(privateKey);
  if (!pem.includes("PRIVATE KEY")) throw new Error("GITHUB_APP_PRIVATE_KEY is not a PEM private key");
  const res = await fetchImpl(`https://api.github.com/app/installations/${installationId}/access_tokens`, {
    method: "POST",
    headers: { Authorization: `Bearer ${appJwt(appId, pem)}`, Accept: "application/vnd.github+json", "User-Agent": "buildmy-house-reviewer", "X-GitHub-Api-Version": "2022-11-28" },
  });
  const text = await res.text();
  let j = null;
  try { j = JSON.parse(text); } catch { /* handled below */ }
  if (!res.ok || !j?.token) throw new Error(`GitHub App token request failed: HTTP ${res.status} ${String(j?.message || text).slice(0, 160)}`);
  return j.token;
}
