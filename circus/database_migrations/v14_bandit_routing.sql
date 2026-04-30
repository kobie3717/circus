-- v14 migration: LinUCB bandit routing tables
-- Implements contextual multi-armed bandit for auto-routing tasks to agents

-- Arm states: One per (agent_id, task_type) pair
CREATE TABLE IF NOT EXISTS routing_arms (
    agent_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    A_blob BLOB NOT NULL,              -- d×d covariance matrix as float32 bytes
    b_blob BLOB NOT NULL,              -- d-vector reward-weighted features
    n_samples INTEGER DEFAULT 0,
    cumulative_reward REAL DEFAULT 0.0,
    last_updated_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, task_type)
);

-- Decision log: One per auto-routed task
CREATE TABLE IF NOT EXISTS routing_decisions (
    id TEXT PRIMARY KEY,               -- decision UUID
    task_id TEXT,                      -- nullable initially, filled after task creation
    picked_agent_id TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    context_blob BLOB NOT NULL,        -- raw 32-dim feature vector
    candidates_considered INTEGER,
    ucb_score REAL,
    fallback TEXT,                     -- "bandit" | "semantic"
    alpha REAL,
    created_at TEXT NOT NULL,
    reward REAL,                       -- NULL until task reaches terminal state
    reward_reason TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

-- Feature normalization stats: One row per feature dimension
CREATE TABLE IF NOT EXISTS routing_feature_stats (
    feature_idx INTEGER PRIMARY KEY,
    running_mean REAL DEFAULT 0.0,
    running_std REAL DEFAULT 1.0,
    n_samples INTEGER DEFAULT 0
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_routing_decisions_task_id ON routing_decisions(task_id);
CREATE INDEX IF NOT EXISTS idx_routing_arms_task_type ON routing_arms(task_type);
CREATE INDEX IF NOT EXISTS idx_routing_arms_updated ON routing_arms(last_updated_at);
