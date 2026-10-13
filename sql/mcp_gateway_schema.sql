-- mcp_gateway_schema.sql: gateway API-key store. Keys are issued per policy
-- identity and stored only as sha256 hashes; plaintext is never persisted and
-- rows are never deleted (revocation/expiry only).
-- Run with: psql -f mcp_gateway_schema.sql <connection-string>
-- Idempotent: safe to run multiple times.

CREATE SCHEMA IF NOT EXISTS mcp_gateway;

CREATE TABLE IF NOT EXISTS mcp_gateway.api_keys (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key_sha256 CHAR(64) NOT NULL UNIQUE CHECK (key_sha256 ~ '^[0-9a-f]{64}$'),
  identity TEXT NOT NULL,
  label TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  last_used_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS api_keys_identity_idx
  ON mcp_gateway.api_keys (identity);

-- Immutable columns: issuing writes the row once; afterwards only expires_at
-- (rotation overlap), last_used_at and revoked_at may ever change. Rows are
-- updated but never deleted, so revocation history is preserved.
CREATE OR REPLACE FUNCTION mcp_gateway.api_keys_guard_immutable() RETURNS trigger AS $$
BEGIN
  IF NEW.key_sha256 IS DISTINCT FROM OLD.key_sha256
     OR NEW.identity IS DISTINCT FROM OLD.identity
     OR NEW.label IS DISTINCT FROM OLD.label
     OR NEW.created_by IS DISTINCT FROM OLD.created_by
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'api_keys: immutable columns changed (only expires_at, last_used_at, revoked_at are updatable)';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS api_keys_immutable ON mcp_gateway.api_keys;
CREATE TRIGGER api_keys_immutable BEFORE UPDATE ON mcp_gateway.api_keys
  FOR EACH ROW EXECUTE FUNCTION mcp_gateway.api_keys_guard_immutable();
