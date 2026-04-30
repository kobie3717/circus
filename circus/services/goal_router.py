"""Goal router service for semantic memory routing."""

import sqlite3
from datetime import datetime
from typing import Optional

import numpy as np

from circus.config import settings


class GoalRouter:
    """Semantic router for matching memories to goal subscriptions."""

    def __init__(self):
        """Initialize goal router with embedding model."""
        self._model = None

    @property
    def model(self):
        """Reuse shared embedding singleton from embeddings.py — prevents double-load (~700MB saved)."""
        if self._model is None:
            from circus.services.embeddings import get_embedding_model
            self._model = get_embedding_model()
        return self._model

    def embed_text(self, text: str) -> bytes:
        """Embed text and return as bytes for sqlite storage."""
        embedding = self.model.encode(text)
        return embedding.tobytes()

    def embed_to_array(self, text: str) -> np.ndarray:
        """Embed text and return as numpy array."""
        return self.model.encode(text)

    def bytes_to_array(self, embedding_bytes: bytes) -> np.ndarray:
        """Convert bytes back to numpy array."""
        return np.frombuffer(embedding_bytes, dtype=np.float32)

    def cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors."""
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(dot_product / (norm1 * norm2))

    def find_matching_goals(
        self,
        conn: sqlite3.Connection,
        memory_content: str,
        memory_confidence: float,
    ) -> list[dict]:
        """
        Find goals that match the memory content.

        Returns list of matches with goal_id, agent_id, and match_score.
        """
        # Embed memory content
        memory_embedding = self.embed_to_array(memory_content)

        # Get active goals
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, agent_id, goal_description, goal_embedding, min_confidence
            FROM goal_subscriptions
            WHERE is_active = 1
            AND (expires_at IS NULL OR expires_at > ?)
        """, (datetime.utcnow().isoformat(),))

        matches = []
        for row in cursor.fetchall():
            goal_id, agent_id, description, embedding_bytes, min_confidence = row

            # Skip if memory confidence is below goal threshold
            if memory_confidence < min_confidence:
                continue

            # Skip if no embedding (shouldn't happen but defensive)
            if not embedding_bytes:
                continue

            # Calculate similarity
            goal_embedding = self.bytes_to_array(embedding_bytes)
            similarity = self.cosine_similarity(memory_embedding, goal_embedding)

            # Include if above threshold
            if similarity >= settings.goal_similarity_threshold:
                matches.append({
                    'goal_id': goal_id,
                    'agent_id': agent_id,
                    'match_score': similarity
                })

        # Sort by match score descending
        matches.sort(key=lambda x: x['match_score'], reverse=True)
        return matches


# Global instance
goal_router = GoalRouter()
