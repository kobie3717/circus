-- Circus Memory Commons Migration v5
-- Week 3 Sub-step 3.5a-prereq: Instance identity bootstrap for federation signing
-- Date: 2026-04-19
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS instance_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
