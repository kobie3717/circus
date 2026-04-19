-- v8 migration: owner_keys table for signed owner binding (Week 5, sub-steps 5.1 + 5.5)
-- Creates owner keypair management table + clears active_preferences (breaking change)

CREATE TABLE IF NOT EXISTS owner_keys (
    owner_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,        -- base64 Ed25519 public key (32 bytes)
    created_at TEXT NOT NULL,        -- ISO timestamp
    description TEXT                 -- optional operator note
);

-- W5 breaking change (executed once on first W5 deploy):
-- Cleared active_preferences of pre-W5 unsigned entries. The gate-5 signature
-- verification rejects unsigned prefs going forward, so we do NOT re-clear on
-- subsequent startups — that would wipe legitimate signed prefs every restart.
-- To re-run the one-time clear manually, use:
--   sqlite3 /root/.circus/circus.db "DELETE FROM active_preferences WHERE ..."
-- (Removed from auto-migration to fix prefs-lost-on-circus-restart bug,
--  surfaced when circus-api hit its 500MB PM2 memory limit repeatedly.)
