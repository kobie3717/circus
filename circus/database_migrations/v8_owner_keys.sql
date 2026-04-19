-- v8 migration: owner_keys table for signed owner binding (Week 5, sub-steps 5.1 + 5.5)
-- Creates owner keypair management table + clears active_preferences (breaking change)

CREATE TABLE IF NOT EXISTS owner_keys (
    owner_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,        -- base64 Ed25519 public key (32 bytes)
    created_at TEXT NOT NULL,        -- ISO timestamp
    description TEXT                 -- optional operator note
);

-- W5 breaking change: clear active_preferences table
-- active_preferences is derived control-plane state, not source of truth.
-- After W5, all preferences must carry signed owner_binding to activate.
-- shared_memories is preserved for audit — only the derived activation state is reset.
DELETE FROM active_preferences;
