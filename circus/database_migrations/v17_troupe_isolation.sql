-- v17: Troupe-scoped memory commons isolation
-- Adds troupe_id column to partition memories by bot group

ALTER TABLE shared_memories ADD COLUMN troupe_id TEXT DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_shared_memories_troupe ON shared_memories(troupe_id);

CREATE TABLE IF NOT EXISTS troupe_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    troupe_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    UNIQUE(troupe_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_troupe_members_agent ON troupe_members(agent_id);
CREATE INDEX IF NOT EXISTS idx_troupe_members_troupe ON troupe_members(troupe_id);

UPDATE shared_memories SET troupe_id = 'default' WHERE troupe_id IS NULL;
