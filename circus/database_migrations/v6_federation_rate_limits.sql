-- Circus Memory Commons Migration v6
-- Week 3 Sub-step 3.5b: Rate limiting for PUSH
-- Date: 2026-04-19

CREATE TABLE IF NOT EXISTS federation_rate_limits (
    peer_id TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (peer_id, window_start)
);

CREATE INDEX IF NOT EXISTS idx_federation_rate_limits_window
    ON federation_rate_limits(window_start);
