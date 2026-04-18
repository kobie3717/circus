-- Circus Memory Commons Migration v3
-- Week 3: Federation (domain field hardening, quarantine, dedup)
-- Date: 2026-04-18

-- Add domain column to shared_memories (nullable initially for backfill)
-- Backfill will be done in Python code for better error handling
-- SQLite doesn't support ALTER COLUMN to add NOT NULL constraint after backfill

-- Federation dedup cache (prevents boomerang/loops)
CREATE TABLE IF NOT EXISTS federation_seen (
    memory_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    source_instance TEXT NOT NULL
);

-- Federation quarantine (failed verification)
CREATE TABLE IF NOT EXISTS federation_quarantine (
    id TEXT PRIMARY KEY,
    memory_id TEXT,
    source_instance TEXT NOT NULL,
    source_passport_hash TEXT,
    reason TEXT NOT NULL,  -- hop_count_exceeded, signature_invalid, passport_unknown, awaiting_review
    payload TEXT NOT NULL,  -- Full signed bundle JSON
    received_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,  -- 7-day auto-delete
    reviewed_at TEXT,
    reviewed_by_passport TEXT,
    review_action TEXT,  -- approve, reject
    review_reason TEXT
);

-- Index for cleanup job (7-day auto-delete)
CREATE INDEX IF NOT EXISTS idx_federation_quarantine_expires ON federation_quarantine(expires_at);

-- Federation audit log (quarantine reviews, backfills, etc)
CREATE TABLE IF NOT EXISTS federation_audit (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,  -- quarantine_review_approve, quarantine_review_reject, quarantine_auto_delete, backfill_run
    actor_passport TEXT,
    target_id TEXT,
    reason TEXT,
    metadata TEXT,  -- JSON
    created_at TEXT NOT NULL
);
