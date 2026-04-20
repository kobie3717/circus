-- v12 migration: Quarantine system for governance (W11)
-- Memories that pass most gates but fail confidence check (borderline) go here for review

CREATE TABLE IF NOT EXISTS quarantine (
    id TEXT PRIMARY KEY,              -- quar-<hex16>
    memory_id TEXT NOT NULL,          -- shared_memories.id
    owner_id TEXT NOT NULL,
    reason TEXT NOT NULL,             -- "confidence_borderline" | "conflict_unresolved" | "operator_hold" | "suspicious_pattern"
    quarantined_at TEXT NOT NULL,
    released_at TEXT,                 -- NULL = still in quarantine
    released_by TEXT,                 -- agent_id or "operator"
    release_reason TEXT,
    auto_release_at TEXT,             -- optional TTL
    FOREIGN KEY (memory_id) REFERENCES shared_memories(id) ON DELETE CASCADE
);

-- Audit log table (unified governance events)
CREATE TABLE IF NOT EXISTS governance_audit (
    id TEXT PRIMARY KEY,              -- audt-<hex16>
    event_type TEXT NOT NULL,         -- "preference_activated" | "preference_cleared" | "key_rotated" | "key_revoked" | "quarantine_created" | "quarantine_released" | "quarantine_discarded"
    actor TEXT,                       -- agent_id or "operator"
    owner_id TEXT,
    detail TEXT,                      -- JSON with context
    happened_at TEXT NOT NULL
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_quarantine_owner ON quarantine(owner_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_released ON quarantine(released_at);
CREATE INDEX IF NOT EXISTS idx_governance_audit_owner ON governance_audit(owner_id);
CREATE INDEX IF NOT EXISTS idx_governance_audit_happened_at ON governance_audit(happened_at);
CREATE INDEX IF NOT EXISTS idx_governance_audit_type ON governance_audit(event_type);
