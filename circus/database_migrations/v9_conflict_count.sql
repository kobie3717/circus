-- v9 migration: Add conflict_count to active_preferences for W7 conflict resolution tracking

-- Add conflict_count column (default 0) if it doesn't exist
-- SQLite doesn't support IF NOT EXISTS for ALTER TABLE ADD COLUMN, so this is handled
-- in the migration runner with a defensive check

-- This migration is idempotent via Python wrapper in database.py
