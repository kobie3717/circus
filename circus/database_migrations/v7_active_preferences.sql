-- v7 migration: active_preferences table for behavior-delta preferences (Week 4)
-- Creates read-optimized table for consuming bots to query current active preferences

CREATE TABLE IF NOT EXISTS active_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,                -- Passport identity.name (e.g., "kobus")
    field_name TEXT NOT NULL,              -- One of the four allowlisted fields
    value TEXT NOT NULL,                   -- The preference value (JSON-encoded if complex)
    source_memory_id TEXT NOT NULL,        -- FK to shared_memories (audit traceability)
    effective_confidence REAL NOT NULL,    -- Post-decay, post-trust-adjustment confidence
    updated_at TEXT NOT NULL,              -- ISO timestamp of last write
    UNIQUE(owner_id, field_name)           -- One active preference per (owner, field)
);

-- Index for fast lookup by owner
CREATE INDEX IF NOT EXISTS idx_active_prefs_owner ON active_preferences(owner_id);
