// Gateway API-key core: generation, hashing, stores and the key service.
// Injected dependencies (store, clock, identities, audit) keep this free of
// I/O side effects so it is testable without Postgres; a later ticket wires
// it into scripts/mcp-gateway.js. The 8-char fingerprint is the ONLY
// key-derived value ever logged or audited -- never the plaintext or the hash.
import { createHash, randomBytes, randomUUID } from "node:crypto";

export const KEY_PREFIX = "gwk_";

// Keys are 256-bit random, so an unsalted fast hash is appropriate: no
// dictionary or brute-force attack is feasible against that entropy, unlike
// a human-chosen password where a slow KDF would be mandatory.
export function hashKey(plaintext) {
  return createHash("sha256").update(plaintext).digest("hex");
}

export function fingerprint(plaintextOrHash) {
  const hash = /^[0-9a-f]{64}$/.test(plaintextOrHash) ? plaintextOrHash : hashKey(plaintextOrHash);
  return hash.slice(0, 8);
}

export function generateKey() {
  return KEY_PREFIX + randomBytes(32).toString("base64url");
}

// Row shape mirrors mcp_gateway.api_keys: snake_case columns, Dates for
// timestamps. Rows are cloned on the way out so callers cannot mutate store state.
export function createMemoryKeyStore() {
  const rows = new Map();
  const clone = (row) => ({ ...row });
  return {
    async insert({ keySha256, identity, label, createdBy, expiresAt }) {
      const row = {
        id: randomUUID(),
        key_sha256: keySha256,
        identity,
        label,
        created_by: createdBy,
        created_at: new Date(),
        expires_at: new Date(expiresAt),
        last_used_at: null,
        revoked_at: null,
      };
      rows.set(row.id, row);
      return clone(row);
    },
    async findByHash(keySha256) {
      for (const row of rows.values()) if (row.key_sha256 === keySha256) return clone(row);
      return null;
    },
    async getById(id) {
      const row = rows.get(id);
      return row ? clone(row) : null;
    },
    async list({ identity } = {}) {
      return [...rows.values()]
        .filter((r) => !identity || r.identity === identity)
        .map(({ key_sha256, ...rest }) => ({ ...rest, fingerprint: key_sha256.slice(0, 8) }));
    },
    async setExpiry(id, expiresAt) {
      const row = rows.get(id);
      if (row) row.expires_at = new Date(expiresAt);
    },
    async revoke(id, at) {
      const row = rows.get(id);
      if (row && !row.revoked_at) row.revoked_at = new Date(at);
    },
    async revokeAllForIdentity(identity, at) {
      let count = 0;
      const stamp = new Date(at);
      for (const row of rows.values()) {
        if (row.identity === identity && !row.revoked_at) {
          row.revoked_at = stamp;
          count++;
        }
      }
      return count;
    },
    async touchLastUsed(id, at) {
      const row = rows.get(id);
      if (row) row.last_used_at = new Date(at);
    },
    async hasIssuedKeys(identity) {
      for (const row of rows.values()) if (row.identity === identity) return true;
      return false;
    },
  };
}

// Store on a pg-like pool (pool.query(text, values)). Values always travel as
// query parameters, never interpolated into SQL.
export function createPgKeyStore(pool) {
  const COLS = "id, key_sha256, identity, label, created_by, created_at, expires_at, last_used_at, revoked_at";
  return {
    async insert({ keySha256, identity, label, createdBy, expiresAt }) {
      const { rows } = await pool.query(
        `INSERT INTO mcp_gateway.api_keys (key_sha256, identity, label, created_by, expires_at)
         VALUES ($1, $2, $3, $4, $5) RETURNING ${COLS}`,
        [keySha256, identity, label, createdBy, expiresAt]
      );
      return rows[0];
    },
    async findByHash(keySha256) {
      const { rows } = await pool.query(
        `SELECT ${COLS} FROM mcp_gateway.api_keys WHERE key_sha256 = $1`,
        [keySha256]
      );
      return rows[0] || null;
    },
    async getById(id) {
      const { rows } = await pool.query(
        `SELECT ${COLS} FROM mcp_gateway.api_keys WHERE id = $1::uuid`,
        [id]
      );
      return rows[0] || null;
    },
    async list({ identity } = {}) {
      const { rows } = await pool.query(
        `SELECT id, identity, label, created_by, created_at, expires_at, last_used_at, revoked_at,
                left(key_sha256, 8) AS fingerprint
         FROM mcp_gateway.api_keys WHERE ($1::text IS NULL OR identity = $1) ORDER BY created_at`,
        [identity ?? null]
      );
      return rows;
    },
    async setExpiry(id, expiresAt) {
      await pool.query(`UPDATE mcp_gateway.api_keys SET expires_at = $2 WHERE id = $1::uuid`, [id, expiresAt]);
    },
    async revoke(id, at) {
      const { rows } = await pool.query(
        `UPDATE mcp_gateway.api_keys SET revoked_at = COALESCE(revoked_at, $2) WHERE id = $1::uuid RETURNING ${COLS}`,
        [id, at]
      );
      return rows[0] || null;
    },
    async revokeAllForIdentity(identity, at) {
      const { rowCount } = await pool.query(
        `UPDATE mcp_gateway.api_keys SET revoked_at = $2 WHERE identity = $1 AND revoked_at IS NULL`,
        [identity, at]
      );
      return rowCount;
    },
    async touchLastUsed(id, at) {
      await pool.query(`UPDATE mcp_gateway.api_keys SET last_used_at = $2 WHERE id = $1::uuid`, [id, at]);
    },
    async hasIssuedKeys(identity) {
      const { rows } = await pool.query(
        `SELECT EXISTS (SELECT 1 FROM mcp_gateway.api_keys WHERE identity = $1) AS present`,
        [identity]
      );
      return !!rows[0]?.present;
    },
  };
}

const MAX_LABEL_CHARS = 80;
const LABEL_CONTROL_CHARS = /[\u0000-\u001f\u007f]/;
const DAY_MS = 86_400_000;

// identities: Set or predicate naming who may receive keys (non-admin policy
// client ids). ttlDaysFor(identity) gives the identity's default TTL; a caller
// may shorten it per key but never extend it. now/audit are injectable.
export function createKeyService({ store, identities, ttlDaysFor, now = () => new Date(), audit = () => {}, touchIntervalMs = 60_000 }) {
  const mayIssue = typeof identities === "function" ? identities : (id) => identities.has(id);
  const defaultTtlDays = (identity) => {
    const d = ttlDaysFor ? ttlDaysFor(identity) : undefined;
    return d === undefined ? 90 : d;
  };
  const lastTouchAt = new Map(); // key id -> epoch ms of last touch (throttle only, never a verify cache)
  const issuedEver = new Set(); // positive-result cache: rows are never deleted, so once true always true

  function validLabel(label) {
    return typeof label === "string" && label.length > 0 && label.length <= MAX_LABEL_CHARS && !LABEL_CONTROL_CHARS.test(label);
  }

  async function issue({ identity, label, createdBy, ttlDays }) {
    if (!mayIssue(identity)) throw new Error("identity is not allowed to receive keys");
    if (!validLabel(label)) throw new Error(`label must be 1-${MAX_LABEL_CHARS} characters with no control characters`);
    const def = defaultTtlDays(identity);
    const days = ttlDays === undefined ? def : ttlDays;
    if (days > def) throw new Error("ttlDays may shorten but never extend the identity's default TTL");
    const plaintext = generateKey();
    const t = now();
    const row = await store.insert({
      keySha256: hashKey(plaintext),
      identity,
      label,
      createdBy,
      expiresAt: new Date(t.getTime() + days * DAY_MS),
    });
    audit({
      event: "key_issued",
      key_id: row.id,
      identity,
      label,
      created_by: createdBy,
      fingerprint: fingerprint(row.key_sha256),
      expires_at: row.expires_at,
    });
    return {
      id: row.id,
      identity: row.identity,
      label: row.label,
      plaintext,
      fingerprint: fingerprint(row.key_sha256),
      created_at: row.created_at,
      expires_at: row.expires_at,
    };
  }

  async function rotate({ keyId, overlapSeconds = 3600, createdBy }) {
    const old = await store.getById(keyId);
    if (!old) throw new Error("key not found");
    const t = now();
    if (old.revoked_at) throw new Error("cannot rotate a revoked key");
    if (new Date(old.expires_at) <= t) throw new Error("cannot rotate an expired key");
    const overlap = Math.min(86_400, Math.max(0, overlapSeconds));
    const fresh = await issue({ identity: old.identity, label: old.label, createdBy });
    const oldExpiresAt = new Date(Math.min(new Date(old.expires_at).getTime(), t.getTime() + overlap * 1000));
    await store.setExpiry(keyId, oldExpiresAt);
    audit({
      event: "key_rotated",
      key_id: fresh.id,
      old_key_id: old.id,
      identity: old.identity,
      label: old.label,
      created_by: createdBy,
      fingerprint: fresh.fingerprint,
      old_fingerprint: fingerprint(old.key_sha256),
      expires_at: fresh.expires_at,
      old_key_expires_at: oldExpiresAt,
    });
    return { ...fresh, old_key_expires_at: oldExpiresAt };
  }

  async function revoke({ keyId, by }) {
    const row = await store.getById(keyId);
    if (!row) throw new Error("key not found");
    await store.revoke(keyId, now());
    audit({ event: "key_revoked", key_id: keyId, by, identity: row.identity, fingerprint: fingerprint(row.key_sha256) });
  }

  async function revokeAllForIdentity({ identity, by }) {
    const count = await store.revokeAllForIdentity(identity, now());
    audit({ event: "keys_revoked_all", identity, by, count });
    return count;
  }

  async function list({ identity } = {}) {
    const rows = await store.list({ identity });
    const t = now();
    return rows.map((r) => ({
      ...r,
      status: r.revoked_at ? "revoked" : new Date(r.expires_at) <= t ? "expired" : "active",
    }));
  }

  async function verifyDetailed(presentedToken) {
    const fp = typeof presentedToken === "string" ? fingerprint(presentedToken) : null;
    if (typeof presentedToken !== "string" || !presentedToken.startsWith(KEY_PREFIX)) {
      return { ok: false, reason: "malformed", fingerprint: fp };
    }
    const hash = hashKey(presentedToken);
    const row = await store.findByHash(hash);
    if (!row) return { ok: false, reason: "unknown", fingerprint: fp };
    if (row.revoked_at) return { ok: false, reason: "revoked", fingerprint: fp };
    if (new Date(row.expires_at) <= now()) return { ok: false, reason: "expired", fingerprint: fp };
    const t = now();
    const last = lastTouchAt.get(row.id);
    if (last === undefined || t.getTime() - last >= touchIntervalMs) {
      lastTouchAt.set(row.id, t.getTime());
      try {
        await store.touchLastUsed(row.id, t);
      } catch {
        // a failed usage-timestamp update must never fail authentication
      }
    }
    return { ok: true, identity: row.identity, keyId: row.id, fingerprint: fp };
  }

  async function verify(presentedToken) {
    const r = await verifyDetailed(presentedToken);
    return r.ok ? { identity: r.identity, keyId: r.keyId, fingerprint: r.fingerprint } : null;
  }

  async function hasIssuedKeys(identity) {
    if (issuedEver.has(identity)) return true;
    const has = await store.hasIssuedKeys(identity);
    if (has) issuedEver.add(identity);
    return has;
  }

  return { issue, rotate, revoke, revokeAllForIdentity, list, verify, verifyDetailed, hasIssuedKeys };
}
