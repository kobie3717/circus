-- Circus Memory Commons Migration v4
-- Week 3 Sub-step 3.4: Transport-level dedup via federation_bundles_seen
-- Date: 2026-04-18

CREATE TABLE IF NOT EXISTS federation_bundles_seen (
    bundle_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    source_instance TEXT NOT NULL,
    bundle_hash TEXT NOT NULL,
    decision TEXT NOT NULL,  -- "admitted" or "quarantined"
    memory_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_federation_bundles_seen_first_seen
    ON federation_bundles_seen(first_seen_at);
