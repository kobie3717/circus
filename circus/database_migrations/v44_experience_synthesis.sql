-- v44 migration: Add synthesis tracking columns to agent_experiences

-- Add is_synthesis column (marks meta-experiences synthesized from multiple raw experiences)
-- Default 0 = raw experience, 1 = synthesized meta-experience
ALTER TABLE agent_experiences ADD COLUMN is_synthesis INTEGER DEFAULT 0;

-- Add source_experience_ids column (JSON array of IDs that fed this synthesis)
-- Example: ["exp-123", "exp-456", "exp-789"]
ALTER TABLE agent_experiences ADD COLUMN source_experience_ids TEXT DEFAULT '[]';

-- Add synthesis_period column (date range for synthesis window)
-- Example: "2026-06-11/2026-06-18"
ALTER TABLE agent_experiences ADD COLUMN synthesis_period TEXT;

-- Create index for querying synthesized experiences
CREATE INDEX IF NOT EXISTS idx_exp_synthesis ON agent_experiences(is_synthesis, task_type, environment);

-- Create index for synthesis period queries
CREATE INDEX IF NOT EXISTS idx_exp_synthesis_period ON agent_experiences(synthesis_period) WHERE synthesis_period IS NOT NULL;
