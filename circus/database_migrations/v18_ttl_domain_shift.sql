-- Circus Memory Commons Migration v18
-- Gap 1: TTL + domain shift signals on memory commons
-- Date: 2026-06-13

-- Domain shift signals table
CREATE TABLE IF NOT EXISTS domain_shift_signals (
    id TEXT PRIMARY KEY,
    domain_tag TEXT NOT NULL,
    filed_by TEXT NOT NULL,
    reason TEXT,
    filed_at TEXT NOT NULL,
    resolved_at TEXT,
    affected_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dss_domain ON domain_shift_signals(domain_tag);
CREATE INDEX IF NOT EXISTS idx_dss_resolved ON domain_shift_signals(resolved_at);
