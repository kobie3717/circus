-- Circus Memory Commons Migration v2
-- Week 1: Foundation (Goal Routing + Write-Through)
-- Date: 2026-04-18

-- Goal subscriptions for semantic routing
CREATE TABLE IF NOT EXISTS goal_subscriptions (
    id TEXT PRIMARY KEY,                    -- goal-{hex}
    agent_id TEXT NOT NULL,
    goal_description TEXT NOT NULL,         -- "debugging payment flows"
    goal_embedding BLOB,                    -- 384-dim vector (all-MiniLM-L6-v2)
    min_confidence REAL DEFAULT 0.5,        -- Only route memories >= this confidence
    created_at TEXT NOT NULL,
    expires_at TEXT,                        -- Goals can expire
    is_active INTEGER DEFAULT 1,
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_goal_agent ON goal_subscriptions(agent_id);
CREATE INDEX IF NOT EXISTS idx_goal_active ON goal_subscriptions(is_active);

-- Agent domain stewardship (Week 2, but create table now for future)
CREATE TABLE IF NOT EXISTS agent_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    domain TEXT NOT NULL,                   -- "user-preferences", "infra-state", "payment-flows"
    stewardship_level REAL DEFAULT 0.5,     -- 0-1, earned through contribution count + accuracy
    claim_reason TEXT,                      -- "Primary maintainer of user pref tracking"
    claimed_at TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    UNIQUE(agent_id, domain),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_domain ON agent_domains(domain);
CREATE INDEX IF NOT EXISTS idx_stewardship ON agent_domains(stewardship_level DESC);

-- Extend shared_memories with provenance fields
-- Check if columns exist before adding (idempotent)
-- SQLite doesn't support IF NOT EXISTS for ALTER TABLE, so we'll handle this in Python

-- Memory pulls tracking (Week 2, but create table now)
CREATE TABLE IF NOT EXISTS memory_pulls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    pulled_by_agent_id TEXT NOT NULL,
    pulled_at TEXT NOT NULL,
    hop_count INTEGER NOT NULL,             -- Hop count when pulled
    confidence_at_pull REAL NOT NULL,       -- Confidence after decay
    FOREIGN KEY (memory_id) REFERENCES shared_memories(id) ON DELETE CASCADE,
    FOREIGN KEY (pulled_by_agent_id) REFERENCES agents(id)
);
CREATE INDEX IF NOT EXISTS idx_memory_pulls_memory ON memory_pulls(memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_pulls_agent ON memory_pulls(pulled_by_agent_id);

-- Belief conflicts (Week 2, but create table now)
CREATE TABLE IF NOT EXISTS belief_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id_a TEXT NOT NULL,
    memory_id_b TEXT NOT NULL,
    conflict_type TEXT NOT NULL,            -- "contradiction", "update", "refinement"
    detected_at TEXT NOT NULL,
    resolution TEXT,                         -- "merged", "kept_a", "kept_b", "manual"
    resolved_at TEXT,
    resolved_by_agent_id TEXT,
    FOREIGN KEY (memory_id_a) REFERENCES shared_memories(id),
    FOREIGN KEY (memory_id_b) REFERENCES shared_memories(id)
);
CREATE INDEX IF NOT EXISTS idx_conflict_unresolved ON belief_conflicts(resolved_at) WHERE resolved_at IS NULL;

-- Behavior deltas (Week 4, but create table now)
CREATE TABLE IF NOT EXISTS behavior_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,          -- Which bot should apply this?
    delta_type TEXT NOT NULL,               -- "system_prompt", "config_var", "rule"
    delta_payload TEXT NOT NULL,            -- JSON: {key: "reply_style", value: "terse"}
    applied INTEGER DEFAULT 0,
    applied_at TEXT,
    FOREIGN KEY (memory_id) REFERENCES shared_memories(id),
    FOREIGN KEY (target_agent_id) REFERENCES agents(id)
);
CREATE INDEX IF NOT EXISTS idx_delta_pending ON behavior_deltas(applied) WHERE applied = 0;
CREATE INDEX IF NOT EXISTS idx_delta_target ON behavior_deltas(target_agent_id);
