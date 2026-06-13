-- v37 migration: Add embedding column to memory_claims for semantic search
-- Stores embeddings as JSON array (TEXT column) for portability

-- Add embedding column to memory_claims
ALTER TABLE memory_claims ADD COLUMN embedding TEXT;

-- Create index on embedding column (for future optimization)
CREATE INDEX IF NOT EXISTS idx_memory_claims_embedding ON memory_claims(embedding) WHERE embedding IS NOT NULL;
