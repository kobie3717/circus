-- v8 migration: owner_keys table for signed owner binding (Week 5, sub-step 5.1)
-- Creates owner keypair management table (additive only, DELETE FROM active_preferences deferred to 5.5)

CREATE TABLE IF NOT EXISTS owner_keys (
    owner_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,        -- base64 Ed25519 public key (32 bytes)
    created_at TEXT NOT NULL,        -- ISO timestamp
    description TEXT                 -- optional operator note
);
