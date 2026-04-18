"""Tests for review fixes applied to Memory Commons Week 1."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import init_database, run_v2_migration
from circus.config import settings


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    # Override settings
    original_db = settings.database_path
    settings.database_path = db_path
    settings.memory_commons_enabled = True

    # Initialize DB
    init_database(db_path)
    run_v2_migration(db_path)

    yield db_path

    # Cleanup
    settings.database_path = original_db
    db_path.unlink(missing_ok=True)


@pytest.fixture
def client(temp_db):
    """Create test client with temp DB."""
    return TestClient(app)


@pytest.fixture
def newcomer_agent(temp_db):
    """Register Newcomer agent (trust < 30) and return auth token."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO agents (
            id, name, role, capabilities, home_instance, passport_hash,
            token_hash, trust_score, trust_tier, registered_at, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
    """, (
        "newcomer-test-123",
        "Newcomer Bot",
        "test-bot",
        '["testing"]',
        "http://localhost:6200",
        "newcomer-passport-hash",
        "newcomer-token-hash",
        25.0,  # Below threshold
        "Newcomer",
    ))

    conn.commit()
    conn.close()

    return "newcomer-test-token"


@pytest.fixture
def established_agent(temp_db):
    """Register Established agent (trust 30-59) and return auth token."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO agents (
            id, name, role, capabilities, home_instance, passport_hash,
            token_hash, trust_score, trust_tier, registered_at, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
    """, (
        "established-test-456",
        "Established Bot",
        "test-bot",
        '["testing"]',
        "http://localhost:6200",
        "established-passport-hash",
        "established-token-hash",
        50.0,  # In Established range
        "Established",
    ))

    conn.commit()
    conn.close()

    return "established-test-token"


class TestReviewFixes:
    """Tests for specific review fixes."""

    def test_fix1_trust_gate_blocks_newcomer(self, client, newcomer_agent):
        """FIX 1: Newcomer (trust < 30) should be blocked from public publish."""
        from circus.routes.agents import verify_token

        def override_auth():
            return "newcomer-test-123"

        app.dependency_overrides[verify_token] = override_auth

        try:
            response = client.post(
                "/api/v1/memory-commons/publish",
                json={
                    "content": "Test public memory",
                    "category": "learning",
                    "tags": ["test"],
                    "privacy_tier": "public",
                    "confidence": 0.9
                }
            )

            # Should be forbidden
            assert response.status_code == 403
            assert "Established tier or higher required" in response.json()["detail"]
        finally:
            app.dependency_overrides.clear()

    def test_fix1_trust_gate_allows_established(self, client, established_agent):
        """FIX 1: Established (trust >= 30) should be allowed to publish public."""
        from circus.routes.agents import verify_token

        def override_auth():
            return "established-test-456"

        app.dependency_overrides[verify_token] = override_auth

        try:
            response = client.post(
                "/api/v1/memory-commons/publish",
                json={
                    "content": "Test public memory from established agent",
                    "category": "learning",
                    "tags": ["test"],
                    "privacy_tier": "public",
                    "confidence": 0.9
                }
            )

            # Should succeed
            assert response.status_code == 200
            data = response.json()
            assert "memory_id" in data
        finally:
            app.dependency_overrides.clear()

    def test_fix2_sse_requires_goal_id(self, client, established_agent):
        """FIX 2: SSE stream should require goal_id parameter."""
        from circus.routes.agents import verify_token

        def override_auth():
            return "established-test-456"

        app.dependency_overrides[verify_token] = override_auth

        try:
            # Attempt to connect without goal_id should fail
            response = client.get("/api/v1/memory-commons/stream")

            # FastAPI auto-returns 422 for missing required params
            assert response.status_code == 422
        finally:
            app.dependency_overrides.clear()

    def test_fix4_room_memory_commons_exists(self, temp_db):
        """FIX 4: Migration should auto-create room-memory-commons."""
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Verify room exists
        cursor.execute("""
            SELECT id, name, slug FROM rooms WHERE id = 'room-memory-commons'
        """)
        row = cursor.fetchone()

        assert row is not None, "room-memory-commons should exist after migration"
        assert row[0] == "room-memory-commons"
        assert row[1] == "#Memory Commons"
        assert row[2] == "memory-commons"

        conn.close()
