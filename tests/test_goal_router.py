"""Unit tests for goal router service."""

import pytest
from circus.services.goal_router import goal_router


class TestGoalRouter:
    """Test goal router semantic matching."""

    def test_semantic_match_high_similarity(self):
        """Test that semantically similar texts match."""
        goal = "debugging PayFast webhook failures"
        memory = "PayFast webhooks use IP whitelist 197.242.158.0/24 not signature verification"

        goal_embedding = goal_router.embed_to_array(goal)
        memory_embedding = goal_router.embed_to_array(memory)
        similarity = goal_router.cosine_similarity(goal_embedding, memory_embedding)

        assert similarity >= 0.6, f"Expected match score >= 0.6, got {similarity}"

    def test_semantic_match_low_similarity(self):
        """Test that unrelated texts don't match."""
        goal = "debugging PayFast webhooks"
        memory = "WhatsApp bot uses Baileys library for message handling"

        goal_embedding = goal_router.embed_to_array(goal)
        memory_embedding = goal_router.embed_to_array(memory)
        similarity = goal_router.cosine_similarity(goal_embedding, memory_embedding)

        assert similarity < 0.6, f"Expected no match (score < 0.6), got {similarity}"

    def test_embed_text_returns_bytes(self):
        """Test that embed_text returns bytes."""
        text = "test memory content"
        embedding_bytes = goal_router.embed_text(text)

        assert isinstance(embedding_bytes, bytes)
        # 384 dimensions * 4 bytes per float32 = 1536 bytes
        assert len(embedding_bytes) == 1536

    def test_bytes_to_array_roundtrip(self):
        """Test embedding bytes can be converted back to array."""
        text = "test memory content"
        original_array = goal_router.embed_to_array(text)
        embedding_bytes = original_array.tobytes()
        recovered_array = goal_router.bytes_to_array(embedding_bytes)

        # Arrays should be identical
        assert (original_array == recovered_array).all()

    def test_cosine_similarity_identical(self):
        """Test cosine similarity of identical vectors is 1.0."""
        text = "identical text"
        embedding = goal_router.embed_to_array(text)
        similarity = goal_router.cosine_similarity(embedding, embedding)

        assert abs(similarity - 1.0) < 0.001, f"Expected 1.0, got {similarity}"

    def test_cosine_similarity_zero_vector(self):
        """Test cosine similarity handles zero vectors."""
        import numpy as np

        vec1 = np.zeros(384, dtype=np.float32)
        vec2 = np.ones(384, dtype=np.float32)
        similarity = goal_router.cosine_similarity(vec1, vec2)

        assert similarity == 0.0, "Zero vector should return 0.0 similarity"
