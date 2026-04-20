-- v11 migration: Federation outbox + peer health tracking (W10)
-- Durable queuing for federation push with exponential backoff retry

CREATE TABLE IF NOT EXISTS federation_outbox (
    id TEXT PRIMARY KEY,              -- fout-<hex16>
    peer_url TEXT NOT NULL,           -- target peer's base URL
    memory_id TEXT NOT NULL,          -- the memory being federated
    payload TEXT NOT NULL,            -- JSON blob of the full memory publish body
    status TEXT DEFAULT 'pending',    -- 'pending' | 'delivered' | 'failed' | 'abandoned'
    attempt_count INTEGER DEFAULT 0,
    last_attempted_at TEXT,
    delivered_at TEXT,
    error TEXT,                       -- last error message
    created_at TEXT NOT NULL,
    next_retry_at TEXT,               -- when to retry (exponential backoff)
    CHECK (status IN ('pending', 'delivered', 'failed', 'abandoned'))
);

-- Indexes for efficient queue processing
CREATE INDEX IF NOT EXISTS idx_federation_outbox_pending
    ON federation_outbox(status, next_retry_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_federation_outbox_peer
    ON federation_outbox(peer_url, status);

CREATE INDEX IF NOT EXISTS idx_federation_outbox_memory
    ON federation_outbox(memory_id);

-- Peer health tracking (extends existing federation_peers table)
-- Note: federation_peers already exists from v3 migration, we just add columns

-- Add health tracking columns if they don't exist
-- These ALTER TABLE statements are idempotent (will fail silently if column exists)
