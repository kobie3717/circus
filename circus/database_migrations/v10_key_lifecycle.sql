-- v10 migration: Key lifecycle (W9) — rotation, revocation, TOFU, discovery

-- Add lifecycle columns to owner_keys table
-- These columns are NULLABLE because existing keys (v8 rows) don't have rotation/revocation history
-- NULL means the column hasn't been populated yet, NOT that the key is inactive
-- is_active is the definitive status field (1=active, 0=rotated/revoked)

-- NOTE: The v8 schema has owner_id as PRIMARY KEY, which prevents multiple keys per owner.
-- W9 requires multiple keys per owner (for rotation history).
-- We'll add the new columns first, then handle the PRIMARY KEY change via table recreation.

-- Add new columns to existing owner_keys table
ALTER TABLE owner_keys ADD COLUMN rotated_at TEXT;
ALTER TABLE owner_keys ADD COLUMN revoked_at TEXT;
ALTER TABLE owner_keys ADD COLUMN revoked_reason TEXT;
ALTER TABLE owner_keys ADD COLUMN superseded_by TEXT;
ALTER TABLE owner_keys ADD COLUMN is_active INTEGER DEFAULT 1;

-- Recreate owner_keys with composite key (owner_id, public_key) to allow multiple keys per owner
-- SQLite doesn't support DROP PRIMARY KEY, so we use the CREATE-INSERT-DROP pattern

-- 1. Create new table with correct schema
CREATE TABLE IF NOT EXISTS owner_keys_v10 (
    owner_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    description TEXT,
    rotated_at TEXT,
    revoked_at TEXT,
    revoked_reason TEXT,
    superseded_by TEXT,
    is_active INTEGER DEFAULT 1,
    PRIMARY KEY (owner_id, public_key)
);

-- 2. Copy existing data from old table to new table
INSERT INTO owner_keys_v10 (owner_id, public_key, created_at, description, rotated_at, revoked_at, revoked_reason, superseded_by, is_active)
SELECT owner_id, public_key, created_at, description, rotated_at, revoked_at, revoked_reason, superseded_by, is_active
FROM owner_keys;

-- 3. Drop old table
DROP TABLE owner_keys;

-- 4. Rename new table to owner_keys
ALTER TABLE owner_keys_v10 RENAME TO owner_keys;

-- Create key_events audit log table
CREATE TABLE IF NOT EXISTS key_events (
    id TEXT PRIMARY KEY,           -- kevent-<hex16>
    owner_id TEXT NOT NULL,
    event_type TEXT NOT NULL,      -- "registered" | "rotated" | "revoked" | "tofu_accepted"
    public_key_b64 TEXT NOT NULL,
    previous_key_b64 TEXT,         -- for rotation events
    reason TEXT,
    happened_at TEXT NOT NULL,
    actor TEXT                     -- who triggered it (agent_id or "operator")
);

-- Create indexes for key_events
CREATE INDEX IF NOT EXISTS idx_key_events_owner ON key_events(owner_id);
CREATE INDEX IF NOT EXISTS idx_key_events_happened_at ON key_events(happened_at);
CREATE INDEX IF NOT EXISTS idx_key_events_type ON key_events(event_type);
