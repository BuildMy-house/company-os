import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import {
  KEY_PREFIX,
  generateKey,
  hashKey,
  fingerprint,
  createMemoryKeyStore,
  createPgKeyStore,
  createKeyService,
} from "./mcp-gateway-keys.js";

const DAY_MS = 86_400_000;
const ADMIN_ID = "gateway-admin";
const ALLOWED = new Set(["local-claude-deploy", "hive-worker"]);
const ttlDaysFor = (id) => (id === "local-claude-deploy" ? 30 : 90);

function makeService({ identities = ALLOWED, store = createMemoryKeyStore(), touchIntervalMs = 60_000 } = {}) {
  const audit = [];
  let clock = new Date("2026-06-01T00:00:00.000Z");
  const service = createKeyService({
    store,
    identities,
    ttlDaysFor,
    touchIntervalMs,
    now: () => clock,
    audit: (e) => audit.push(e),
  });
  return {
    service,
    store,
    audit,
    get clock() { return clock; },
    setClock(msOrDate) { clock = new Date(msOrDate); },
    advance(ms) { clock = new Date(clock.getTime() + ms); },
  };
}

async function issueKey(ctx, identity = "local-claude-deploy", label = "deploy", extra = {}) {
  return ctx.service.issue({ identity, label, createdBy: ADMIN_ID, ...extra });
}

async function captureConsole(fn) {
  const out = [];
  const origLog = console.log;
  const origErr = console.error;
  const stringify = (a) => a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ");
  console.log = (...a) => out.push(stringify(a));
  console.error = (...a) => out.push(stringify(a));
  try {
    const result = await fn();
    return { result, output: out.join("\n") };
  } finally {
    console.log = origLog;
    console.error = origErr;
  }
}

// --- hashing ------------------------------------------------------------------

test("generateKey: gwk_ prefix, >=43 random chars, unique per call", () => {
  const a = generateKey();
  const b = generateKey();
  assert.ok(a.startsWith(KEY_PREFIX));
  assert.equal(a.length, KEY_PREFIX.length + 43);
  assert.match(a.slice(KEY_PREFIX.length), /^[A-Za-z0-9_-]{43}$/);
  assert.notEqual(a, b);
});

test("hashKey: stable, 64-hex, differs per key", () => {
  const k = generateKey();
  const h1 = hashKey(k);
  const h2 = hashKey(generateKey());
  assert.match(h1, /^[0-9a-f]{64}$/);
  assert.equal(h1, hashKey(k));
  assert.notEqual(h1, h2);
});

test("fingerprint: 8 hex chars, equals hash prefix, same from plaintext or hash", () => {
  const k = generateKey();
  const h = hashKey(k);
  const fp = fingerprint(k);
  assert.match(fp, /^[0-9a-f]{8}$/);
  assert.equal(fp, h.slice(0, 8));
  assert.equal(fp, fingerprint(h));
});

// --- plaintext shown once ------------------------------------------------------

test("plaintext is only ever in the issue() result; stores hold the hash only", async () => {
  const ctx = makeService();
  const issued = await issueKey(ctx);
  const p = issued.plaintext;
  const h = hashKey(p);

  const row = await ctx.store.getById(issued.id);
  assert.equal(row.key_sha256, h);
  assert.ok(!JSON.stringify(row).includes(p), "stored row must not contain plaintext");
  assert.ok(JSON.stringify(row).includes(h), "stored row must contain only the hash");

  const stored = await ctx.store.findByHash(h);
  assert.equal(stored.key_sha256, h);
  assert.ok(!JSON.stringify(stored).includes(p));

  for (const r of await ctx.store.list({ identity: "local-claude-deploy" })) {
    const j = JSON.stringify(r);
    assert.ok(!("key_sha256" in r), "store.list rows must not expose the hash");
    assert.ok(!j.includes(p) && !j.includes(h), "store.list rows must not contain plaintext or hash");
    assert.equal(r.fingerprint, h.slice(0, 8));
  }

  for (const r of await ctx.service.list({ identity: "local-claude-deploy" })) {
    const j = JSON.stringify(r);
    assert.ok(!("key_sha256" in r));
    assert.ok(!j.includes(p) && !j.includes(h));
    assert.equal(r.fingerprint, h.slice(0, 8));
    assert.equal(r.status, "active");
  }
});

// --- TTL ----------------------------------------------------------------------

test("TTL: 30 days for local-claude-deploy, 90 for others, shortening allowed, extending rejected", async () => {
  const ctx = makeService();
  const short = await issueKey(ctx, "local-claude-deploy");
  assert.equal(short.expires_at.getTime(), ctx.clock.getTime() + 30 * DAY_MS);

  const other = await issueKey(ctx, "hive-worker");
  assert.equal(other.expires_at.getTime(), ctx.clock.getTime() + 90 * DAY_MS);

  const shortened = await issueKey(ctx, "local-claude-deploy", "deploy", { ttlDays: 5 });
  assert.equal(shortened.expires_at.getTime(), ctx.clock.getTime() + 5 * DAY_MS);

  await assert.rejects(
    issueKey(ctx, "local-claude-deploy", "deploy", { ttlDays: 31 }),
    /never extend/
  );
  await assert.rejects(issueKey(ctx, "hive-worker", "deploy", { ttlDays: 91 }), /never extend/);
});

test("TTL: without ttlDaysFor the default is 90 days", async () => {
  const audit = [];
  let clock = new Date("2026-06-01T00:00:00.000Z");
  const service = createKeyService({
    store: createMemoryKeyStore(),
    identities: ALLOWED,
    now: () => clock,
    audit: (e) => audit.push(e),
  });
  const issued = await service.issue({ identity: "local-claude-deploy", label: "deploy", createdBy: ADMIN_ID });
  assert.equal(issued.expires_at.getTime(), clock.getTime() + 90 * DAY_MS);
});

// --- expiry -------------------------------------------------------------------

test("verify fails exactly at expires_at with the injected clock", async () => {
  const ctx = makeService();
  const issued = await issueKey(ctx);

  ctx.setClock(issued.expires_at.getTime() - 1);
  assert.ok(await ctx.service.verify(issued.plaintext), "valid one millisecond before expiry");

  ctx.setClock(issued.expires_at.getTime());
  assert.equal(await ctx.service.verify(issued.plaintext), null, "fails exactly at expires_at");
  assert.deepEqual(await ctx.service.verifyDetailed(issued.plaintext), {
    ok: false,
    reason: "expired",
    fingerprint: fingerprint(issued.plaintext),
  });
});

// --- revocation ---------------------------------------------------------------

test("revoke: immediate failure and idempotent (first revoked_at wins)", async () => {
  const ctx = makeService();
  const k = await issueKey(ctx);
  const k2 = await issueKey(ctx, "local-claude-deploy", "deploy-2");

  await ctx.service.revoke({ keyId: k.id, by: ADMIN_ID });
  assert.equal(await ctx.service.verify(k.plaintext), null);
  assert.equal((await ctx.service.verifyDetailed(k.plaintext)).reason, "revoked");

  const first = (await ctx.store.getById(k.id)).revoked_at.getTime();
  ctx.advance(60_000);
  await ctx.service.revoke({ keyId: k.id, by: ADMIN_ID });
  assert.equal((await ctx.store.getById(k.id)).revoked_at.getTime(), first, "second revoke keeps first timestamp");
  assert.ok(await ctx.service.verify(k2.plaintext), "other keys unaffected");
});

test("revoke-all: returns count, later verifies fail, hasIssuedKeys stays true", async () => {
  const ctx = makeService();
  const k1 = await issueKey(ctx);
  const k2 = await issueKey(ctx, "local-claude-deploy", "deploy-2");
  const k3 = await issueKey(ctx, "hive-worker");

  assert.equal(await ctx.service.hasIssuedKeys("local-claude-deploy"), true);
  assert.equal(await ctx.service.hasIssuedKeys("nobody"), false);

  const count = await ctx.service.revokeAllForIdentity({ identity: "local-claude-deploy", by: ADMIN_ID });
  assert.equal(count, 2);
  assert.equal(await ctx.service.verify(k1.plaintext), null);
  assert.equal(await ctx.service.verify(k2.plaintext), null);
  assert.equal((await ctx.service.verifyDetailed(k1.plaintext)).reason, "revoked");
  assert.ok(await ctx.service.verify(k3.plaintext), "hive-worker key unaffected");

  assert.equal(await ctx.service.revokeAllForIdentity({ identity: "local-claude-deploy", by: ADMIN_ID }), 0, "already-revoked rows are not re-counted");
  assert.equal(await ctx.service.hasIssuedKeys("local-claude-deploy"), true, "positive result survives revoke-all (rows are never deleted)");
});

test("hasIssuedKeys: store errors propagate (fails closed) and are not cached", async () => {
  const memory = createMemoryKeyStore();
  let fail = false;
  const store = {
    ...memory,
    async hasIssuedKeys(identity) {
      if (fail) throw new Error("db down");
      return memory.hasIssuedKeys(identity);
    },
  };
  const ctx = makeService({ store });
  assert.equal(await ctx.service.hasIssuedKeys("hive-worker"), false);
  fail = true;
  await assert.rejects(ctx.service.hasIssuedKeys("hive-worker"), /db down/);
  fail = false;
  await issueKey(ctx, "hive-worker", "h");
  assert.equal(await ctx.service.hasIssuedKeys("hive-worker"), true);
  fail = true;
  assert.equal(await ctx.service.hasIssuedKeys("hive-worker"), true, "cached true never queries again");
});

// --- rotation -----------------------------------------------------------------

test("rotation: old and new both verify during overlap; old fails after", async () => {
  const ctx = makeService();
  const old = await issueKey(ctx);
  const rot = await ctx.service.rotate({ keyId: old.id, overlapSeconds: 3600, createdBy: ADMIN_ID });

  assert.notEqual(rot.id, old.id);
  assert.equal(rot.identity, old.identity, "new key bound to same identity");
  assert.equal(rot.label, old.label);
  assert.ok(rot.plaintext.startsWith(KEY_PREFIX));
  assert.equal(rot.expires_at.getTime(), ctx.clock.getTime() + 30 * DAY_MS, "fresh full TTL");
  assert.equal(rot.old_key_expires_at.getTime(), ctx.clock.getTime() + 3600 * 1000);

  ctx.advance(1800 * 1000);
  assert.ok(await ctx.service.verify(old.plaintext), "old verifies during overlap");
  assert.ok(await ctx.service.verify(rot.plaintext), "new verifies during overlap");

  ctx.setClock(rot.old_key_expires_at.getTime());
  assert.equal(await ctx.service.verify(old.plaintext), null, "old fails once overlap ends");
  assert.equal((await ctx.service.verifyDetailed(old.plaintext)).reason, "expired");
  assert.ok(await ctx.service.verify(rot.plaintext), "new still works");
});

test("rotation: overlapSeconds clamped to [0, 86400]", async () => {
  const ctx = makeService();
  const a = await issueKey(ctx);
  const neg = await ctx.service.rotate({ keyId: a.id, overlapSeconds: -10, createdBy: ADMIN_ID });
  assert.equal(neg.old_key_expires_at.getTime(), ctx.clock.getTime(), "negative clamps to 0");
  assert.equal(await ctx.service.verify(a.plaintext), null, "zero overlap means old dies immediately");

  const b = await issueKey(ctx, "local-claude-deploy", "b");
  const huge = await ctx.service.rotate({ keyId: b.id, overlapSeconds: 999_999, createdBy: ADMIN_ID });
  assert.equal(huge.old_key_expires_at.getTime(), ctx.clock.getTime() + 86_400 * 1000, "clamped to one day");
  assert.ok(await ctx.service.verify(b.plaintext));
});

test("rotation: revoked or expired old key errors; unknown id errors", async () => {
  const ctx = makeService();
  const revoked = await issueKey(ctx);
  await ctx.service.revoke({ keyId: revoked.id, by: ADMIN_ID });
  await assert.rejects(ctx.service.rotate({ keyId: revoked.id, createdBy: ADMIN_ID }), /revoked/);

  const expired = await issueKey(ctx, "local-claude-deploy", "e");
  ctx.advance(31 * DAY_MS);
  await assert.rejects(ctx.service.rotate({ keyId: expired.id, createdBy: ADMIN_ID }), /expired/);

  await assert.rejects(ctx.service.rotate({ keyId: randomUUID(), createdBy: ADMIN_ID }), /not found/);
});

// --- scope binding ------------------------------------------------------------

test("issue: unknown identity and non-allowed (admin) identity are rejected", async () => {
  const ctx = makeService();
  await assert.rejects(issueKey(ctx, ADMIN_ID, "admin key"), /not allowed/);
  await assert.rejects(issueKey(ctx, "totally-unknown", "x"), /not allowed/);
  assert.equal(await ctx.service.hasIssuedKeys(ADMIN_ID), false, "admin never receives keys");

  const fnCtx = makeService({ identities: (id) => id.startsWith("local-") });
  await assert.rejects(issueKey(fnCtx, "hive-worker", "x"), /not allowed/);
  assert.ok(await issueKey(fnCtx, "local-claude-deploy"));
});

test("issue: label validation (empty, too long, control chars)", async () => {
  const ctx = makeService();
  await assert.rejects(issueKey(ctx, "local-claude-deploy", ""), /label/);
  await assert.rejects(issueKey(ctx, "local-claude-deploy", "x".repeat(81)), /label/);
  await assert.rejects(issueKey(ctx, "local-claude-deploy", "bad\u0001label"), /label/);
  assert.ok(await issueKey(ctx, "local-claude-deploy", "x".repeat(80)));
  assert.ok(await issueKey(ctx, "local-claude-deploy", "ok label"));
});

test("verify: returns the identity of the row only; unissued and non-prefixed tokens fail", async () => {
  const ctx = makeService();
  const issued = await issueKey(ctx);
  const v = await ctx.service.verify(issued.plaintext);
  assert.equal(v.identity, "local-claude-deploy");
  assert.equal(v.keyId, issued.id);
  assert.equal(v.fingerprint, fingerprint(issued.plaintext));

  assert.equal(await ctx.service.verify(generateKey()), null, "gwk_ token that was never issued");
  assert.equal(await ctx.service.verify("Bearer something"), null);
  assert.equal(await ctx.service.verify(""), null);
  assert.equal(await ctx.service.verify(null), null);
});

// --- last_used_at throttling --------------------------------------------------

test("touchLastUsed is throttled to once per touchIntervalMs per key", async () => {
  const memory = createMemoryKeyStore();
  let touches = 0;
  const store = {
    ...memory,
    async touchLastUsed(id, at) {
      touches++;
      return memory.touchLastUsed(id, at);
    },
  };
  const ctx = makeService({ store, touchIntervalMs: 60_000 });
  const k = await issueKey(ctx);

  assert.ok(await ctx.service.verify(k.plaintext));
  assert.equal(touches, 1);

  ctx.advance(30_000);
  assert.ok(await ctx.service.verify(k.plaintext));
  assert.equal(touches, 1, "second verify inside the interval does not touch");

  ctx.advance(30_000);
  assert.ok(await ctx.service.verify(k.plaintext));
  assert.equal(touches, 2, "touches again once the interval elapses");

  assert.ok((await ctx.store.getById(k.id)).last_used_at instanceof Date);
});

test("a failing touchLastUsed never fails authentication", async () => {
  const memory = createMemoryKeyStore();
  const store = {
    ...memory,
    async touchLastUsed() {
      throw new Error("touch db down");
    },
  };
  const ctx = makeService({ store });
  const k = await issueKey(ctx);
  assert.ok(await ctx.service.verify(k.plaintext));
  assert.deepEqual(await ctx.service.verifyDetailed(k.plaintext), {
    ok: true,
    identity: k.identity,
    keyId: k.id,
    fingerprint: fingerprint(k.plaintext),
  });
});

// --- verifyDetailed reasons ---------------------------------------------------

test("verifyDetailed: malformed / unknown / revoked / expired / ok", async () => {
  const ctx = makeService();
  const k = await issueKey(ctx);

  const malformed = await ctx.service.verifyDetailed("not-a-key");
  assert.equal(malformed.ok, false);
  assert.equal(malformed.reason, "malformed");
  assert.match(malformed.fingerprint, /^[0-9a-f]{8}$/);

  const unissued = generateKey();
  const unknown = await ctx.service.verifyDetailed(unissued);
  assert.deepEqual(unknown, { ok: false, reason: "unknown", fingerprint: fingerprint(unissued) });

  const ok = await ctx.service.verifyDetailed(k.plaintext);
  assert.equal(ok.ok, true);
  assert.equal(ok.reason, undefined);
  assert.equal(ok.identity, "local-claude-deploy");
  assert.equal(ok.keyId, k.id);

  await ctx.service.revoke({ keyId: k.id, by: ADMIN_ID });
  assert.equal((await ctx.service.verifyDetailed(k.plaintext)).reason, "revoked");

  const k2 = await issueKey(ctx, "local-claude-deploy", "k2");
  ctx.setClock(k2.expires_at.getTime());
  assert.equal((await ctx.service.verifyDetailed(k2.plaintext)).reason, "expired");

  for (const r of [malformed, unknown, await ctx.service.verifyDetailed(k.plaintext)]) {
    assert.ok(!JSON.stringify(r).includes(k.plaintext), "failure objects never carry the presented plaintext");
  }
});

// --- no plaintext in logs -----------------------------------------------------

test("full lifecycle: audit JSON and console output never contain plaintext or hash, fingerprint does", async () => {
  const ctx = makeService();
  const { result, output } = await captureConsole(async () => {
    const p1 = (await issueKey(ctx)).plaintext;
    const keyId = (await ctx.store.list({ identity: "local-claude-deploy" }))[0].id;
    await ctx.service.list({ identity: "local-claude-deploy" });
    await ctx.service.verify(p1);
    const rotated = await ctx.service.rotate({ keyId, overlapSeconds: 600, createdBy: ADMIN_ID });
    await ctx.service.verify(rotated.plaintext);
    await ctx.service.verify(generateKey());
    await ctx.service.verify("bad-token");
    try {
      await issueKey(ctx, ADMIN_ID, "admin");
    } catch { /* rejected identity, collected below */ }
    await ctx.service.revoke({ keyId, by: ADMIN_ID });
    await ctx.service.revokeAllForIdentity({ identity: "local-claude-deploy", by: ADMIN_ID });
    await ctx.service.hasIssuedKeys("local-claude-deploy");
    await ctx.service.hasIssuedKeys("nobody");
    return { p1, p2: rotated.plaintext, p2id: rotated.id, oldId: keyId };
  });

  const { p1, p2, p2id, oldId } = result;
  const auditJson = JSON.stringify(ctx.audit);
  const combined = auditJson + "\n" + output;

  assert.ok(!combined.includes(p1), "old plaintext must never appear");
  assert.ok(!combined.includes(p2), "new plaintext must never appear");
  assert.ok(!combined.includes(hashKey(p1)), "full hash must never appear");
  assert.ok(!combined.includes(hashKey(p2)), "full hash must never appear");
  assert.ok(auditJson.includes(fingerprint(p1)), "8-char fingerprint must appear");
  assert.ok(auditJson.includes(fingerprint(p2)), "8-char fingerprint must appear");

  for (const event of ["key_issued", "key_rotated", "key_revoked", "keys_revoked_all"]) {
    assert.ok(ctx.audit.some((e) => e.event === event), `audit emitted ${event}`);
  }
  const rotEvent = ctx.audit.find((e) => e.event === "key_rotated");
  assert.equal(rotEvent.old_key_id, oldId);
  assert.equal(rotEvent.key_id, p2id);
  assert.equal(rotEvent.old_fingerprint, fingerprint(p1));
  assert.equal(rotEvent.fingerprint, fingerprint(p2));
});

test("thrown error messages never contain a plaintext key or a hash", async () => {
  const ctx = makeService();
  const k = await issueKey(ctx);
  const p = k.plaintext;
  const h = hashKey(p);
  const errors = [];
  const expectThrow = async (fn) => {
    try {
      await fn();
      assert.fail("expected an error");
    } catch (e) {
      errors.push(e);
    }
  };
  await expectThrow(() => issueKey(ctx, ADMIN_ID, "l"));
  await expectThrow(() => issueKey(ctx, "local-claude-deploy", "l", { ttlDays: 999 }));
  await expectThrow(() => ctx.service.rotate({ keyId: randomUUID(), createdBy: ADMIN_ID }));
  await expectThrow(() => ctx.service.revoke({ keyId: randomUUID(), by: ADMIN_ID }));
  await ctx.service.revoke({ keyId: k.id, by: ADMIN_ID });
  await expectThrow(() => ctx.service.rotate({ keyId: k.id, createdBy: ADMIN_ID }));

  assert.ok(errors.length >= 5);
  for (const e of errors) {
    assert.ok(!e.message.includes(p), "error message must not contain plaintext");
    assert.ok(!e.message.includes(h), "error message must not contain a hash");
  }
});

// --- pg store parameterisation ------------------------------------------------

function makeFakePool(responses = []) {
  const calls = [];
  let i = 0;
  return {
    calls,
    async query(text, values) {
      calls.push({ text, values });
      return responses[i++] ?? { rows: [], rowCount: 0 };
    },
  };
}

const HASH = "a".repeat(64);
const pgRow = {
  id: "11111111-1111-1111-1111-111111111111",
  key_sha256: HASH,
  identity: "local-claude-deploy",
  label: "deploy",
  created_by: "gateway-admin",
  created_at: new Date("2026-06-01T00:00:00Z"),
  expires_at: new Date("2026-06-30T00:00:00Z"),
  last_used_at: null,
  revoked_at: null,
};

function assertParameterised(calls, secrets) {
  assert.ok(calls.length > 0, "expected queries to have been issued");
  for (const { text, values } of calls) {
    for (const s of secrets) {
      if (s === null || s === undefined) continue;
      assert.ok(!text.includes(s), `SQL text must not embed ${JSON.stringify(s.slice(0, 12))}...`);
    }
    assert.ok(/\$\d/.test(text), `query must use placeholders: ${text.slice(0, 60)}`);
  }
}

test("pg store: every query is parameterised and never carries plaintext", async () => {
  const plaintext = generateKey();
  const identity = "local-claude-deploy";
  const pool = makeFakePool([{ rows: [pgRow] }, { rows: [pgRow] }, { rows: [pgRow] }, { rows: [{ fingerprint: "abcdef12" }] }, { rows: [] }, { rows: [pgRow] }, { rowCount: 3 }, { rows: [] }, { rows: [{ present: true }] }]);
  const store = createPgKeyStore(pool);

  await store.insert({ keySha256: HASH, identity, label: "deploy", createdBy: "gateway-admin", expiresAt: pgRow.expires_at });
  await store.findByHash(HASH);
  await store.getById(pgRow.id);
  await store.list({ identity });
  await store.setExpiry(pgRow.id, pgRow.expires_at);
  await store.revoke(pgRow.id, new Date("2026-06-02T00:00:00Z"));
  await store.revokeAllForIdentity(identity, new Date("2026-06-02T00:00:00Z"));
  await store.touchLastUsed(pgRow.id, new Date("2026-06-02T00:00:00Z"));
  await store.hasIssuedKeys(identity);

  assert.equal(pool.calls.length, 9);
  assertParameterised(pool.calls, [HASH, identity, "deploy", plaintext, "gwk_"]);

  const allParams = JSON.stringify(pool.calls.map((c) => c.values));
  assert.ok(!allParams.includes(plaintext), "plaintext must never reach the pool");
  assert.ok(allParams.includes(HASH), "the hash travels as a parameter");

  const insertText = pool.calls[0].text;
  assert.match(insertText, /INSERT INTO mcp_gateway\.api_keys/);
  assert.match(insertText, /VALUES \(\$1, \$2, \$3, \$4, \$5\)/);

  const selectHash = pool.calls[1].text;
  assert.match(selectHash, /WHERE key_sha256 = \$1/);
  assert.doesNotMatch(selectHash, /gwk_/, "SELECTs must never look up a plaintext value");

  const listText = pool.calls[3].text;
  assert.ok(listText.includes("left(key_sha256, 8) AS fingerprint"), "fingerprint computed in SQL, hash column not selected");
  assert.equal((listText.match(/key_sha256/g) || []).length, 1, "list must not select the full hash column");
});

test("pg store: row handling, list strips the hash, revoke semantics", async () => {
  const identity = "local-claude-deploy";

  let pool = makeFakePool([{ rows: [pgRow] }]);
  let store = createPgKeyStore(pool);
  assert.equal((await store.insert({ keySha256: HASH, identity, label: "deploy", createdBy: "gateway-admin", expiresAt: pgRow.expires_at })).key_sha256, HASH);

  pool = makeFakePool([{ rows: [pgRow] }]);
  store = createPgKeyStore(pool);
  assert.deepEqual(await store.findByHash(HASH), pgRow);

  pool = makeFakePool([{ rows: [] }]);
  store = createPgKeyStore(pool);
  assert.equal(await store.findByHash(HASH), null);
  assert.equal(await store.getById(randomUUID()), null);

  pool = makeFakePool([{ rows: [{ id: pgRow.id, identity, label: "deploy", created_by: "gateway-admin", created_at: pgRow.created_at, expires_at: pgRow.expires_at, last_used_at: null, revoked_at: null, fingerprint: "aaaaaaaa" }] }]);
  store = createPgKeyStore(pool);
  const listed = await store.list({ identity });
  assert.equal(listed.length, 1);
  assert.ok(!("key_sha256" in listed[0]));
  assert.equal(listed[0].fingerprint, "aaaaaaaa");

  pool = makeFakePool([{ rows: [pgRow] }]);
  store = createPgKeyStore(pool);
  await store.revoke(pgRow.id, pgRow.expires_at);
  assert.ok(pool.calls[0].text.includes("COALESCE(revoked_at, $2)"), "single revoke keeps the first revoked_at");

  pool = makeFakePool([{ rowCount: 3 }]);
  store = createPgKeyStore(pool);
  assert.equal(await store.revokeAllForIdentity(identity, pgRow.expires_at), 3);
  assert.ok(pool.calls[0].text.includes("revoked_at IS NULL"), "revoke-all skips already-revoked rows");

  pool = makeFakePool([{ rows: [{ present: true }] }, { rows: [{ present: false }] }]);
  store = createPgKeyStore(pool);
  assert.equal(await store.hasIssuedKeys(identity), true);
  assert.equal(await store.hasIssuedKeys("nobody"), false);
  assert.ok(pool.calls[1].text.includes("EXISTS"), "hasIssuedKeys uses EXISTS (counts revoked/expired rows too)");
});
