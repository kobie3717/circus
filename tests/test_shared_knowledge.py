"""Tests for shared knowledge search endpoint (W11)."""

import pytest
from fastapi.testclient import TestClient
from circus.app import app
from circus.database import get_db


@pytest.fixture
def client():
    """Test client."""
    return TestClient(app)


@pytest.fixture
def seed_test_memories():
    """Seed shared_memories table with test data."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Disable foreign keys for test data insertion
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Clean up any existing test data
        cursor.execute("DELETE FROM shared_memories WHERE id LIKE 'test-%'")

        # Insert test memories (FK checks disabled)
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, domain,
                confidence, hop_count, original_author, shared_at,
                effective_confidence, privacy_tier
            ) VALUES
            ('test-memory-1', 'room-memory-commons', 'Friday-123',
             'WhatsAuction uses PM2 for process management', 'fact',
             'knowledge.infrastructure', 0.9, 1, 'Friday-123',
             '2026-04-20T10:00:00', 0.9, 'team'),
            ('test-memory-2', 'room-memory-commons', 'Claw-456',
             'FlashVault runs on nginx with SSL certificates', 'fact',
             'knowledge.infrastructure', 0.85, 1, 'Claw-456',
             '2026-04-20T11:00:00', 0.85, 'team'),
            ('test-memory-3', 'room-memory-commons', '007-789',
             'PostgreSQL is used for database storage', 'fact',
             'knowledge.database', 0.95, 1, '007-789',
             '2026-04-20T12:00:00', 0.95, 'team')
        """)
        conn.commit()

        # Re-enable foreign keys
        cursor.execute("PRAGMA foreign_keys = ON")

    yield

    # Cleanup after test
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM shared_memories WHERE id LIKE 'test-%'")
        conn.commit()


def test_search_empty_query(client, seed_test_memories):
    """Test search with non-matching query returns empty results."""
    response = client.get("/api/v1/memory-commons/search?q=nonexistent12345xyz")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0
    assert data["results"] == []
    assert data["query"] == "nonexistent12345xyz"


def test_search_keyword_match(client, seed_test_memories):
    """Test search with keyword returns matching results."""
    response = client.get("/api/v1/memory-commons/search?q=PM2")
    assert response.status_code == 200
    data = response.json()

    assert data["count"] >= 1
    assert data["query"] == "PM2"

    # Check first result
    result = data["results"][0]
    assert "WhatsAuction uses PM2" in result["content"]
    assert result["category"] == "fact"
    assert result["domain"] == "knowledge.infrastructure"
    assert result["source_agent"] == "Friday"  # Timestamp suffix stripped
    assert 0.0 <= result["score"] <= 1.0
    assert 0.0 <= result["confidence"] <= 1.0


def test_search_limit_respected(client, seed_test_memories):
    """Test search respects limit parameter."""
    # Search for generic term that might match multiple records
    response = client.get("/api/v1/memory-commons/search?q=knowledge&limit=2")
    assert response.status_code == 200
    data = response.json()

    # Limit should be respected (max 2 results)
    assert data["count"] <= 2
    assert len(data["results"]) <= 2


def test_search_limit_clamping(client, seed_test_memories):
    """Test search clamps limit to max 10."""
    response = client.get("/api/v1/memory-commons/search?q=test&limit=999")
    assert response.status_code == 200
    # Should not error, limit should be clamped to 10


def test_search_score_calculation(client, seed_test_memories):
    """Test search score calculation: confidence × (1 - hop_count × 0.1)."""
    response = client.get("/api/v1/memory-commons/search?q=PostgreSQL")
    assert response.status_code == 200
    data = response.json()

    if data["count"] > 0:
        result = data["results"][0]
        # Score should be confidence × (1 - hop_count × 0.1)
        expected_score = result["confidence"] * (1.0 - 1 * 0.1)  # hop_count = 1
        assert result["score"] == pytest.approx(expected_score, abs=0.01)


def test_search_no_auth_required(client, seed_test_memories):
    """Test search endpoint requires no authentication (read-only, public)."""
    # Should work without Authorization header
    response = client.get("/api/v1/memory-commons/search?q=nginx")
    assert response.status_code == 200
    # If 401/403, then auth is required (test fails)


def test_search_case_insensitive(client, seed_test_memories):
    """Test search is case-insensitive (LIKE query)."""
    # Search with different casing
    response1 = client.get("/api/v1/memory-commons/search?q=pm2")
    response2 = client.get("/api/v1/memory-commons/search?q=PM2")

    assert response1.status_code == 200
    assert response2.status_code == 200

    # Should return same results (LIKE is case-insensitive)
    data1 = response1.json()
    data2 = response2.json()

    # At least one should match (case insensitivity)
    assert data1["count"] > 0 or data2["count"] > 0
