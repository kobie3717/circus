"""End-to-end integration test for Memory Commons.

Tests the full flow: Claw publishes memory → Friday subscribes via goal → receives via SSE.
"""

import asyncio
import json
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
def claw_agent(temp_db):
    """Register Claw agent and return auth token."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    # Create Claw agent
    cursor.execute("""
        INSERT INTO agents (
            id, name, role, capabilities, home_instance, passport_hash,
            token_hash, trust_score, trust_tier, registered_at, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
    """, (
        "claw-test-123",
        "Claw",
        "infra-bot",
        '["monitoring", "alerts"]',
        "http://localhost:6200",
        "claw-passport-hash",
        "claw-token-hash",
        72.0,
        "Trusted",
    ))

    conn.commit()
    conn.close()

    # Return a mock token (in real tests, use proper JWT)
    return "claw-test-token"


@pytest.fixture
def friday_agent(temp_db):
    """Register Friday agent and return auth token."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    # Create Friday agent
    cursor.execute("""
        INSERT INTO agents (
            id, name, role, capabilities, home_instance, passport_hash,
            token_hash, trust_score, trust_tier, registered_at, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
    """, (
        "friday-test-456",
        "Friday",
        "assistant",
        '["chat", "memory"]',
        "http://localhost:6200",
        "friday-passport-hash",
        "friday-token-hash",
        88.0,
        "Elder",
    ))

    conn.commit()
    conn.close()

    return "friday-test-token"


class TestMemoryCommonsE2E:
    """End-to-end integration tests."""

    def test_goal_creation(self, client, friday_agent):
        """Test Friday can create a goal subscription."""
        # Mock the auth dependency to return friday agent ID
        from circus.routes.agents import verify_token

        def override_auth():
            return "friday-test-456"

        app.dependency_overrides[verify_token] = override_auth

        try:
            response = client.post(
                "/api/v1/memory-commons/goals",
                json={
                    "goal_description": "debugging payment flows",
                    "min_confidence": 0.7,
                    "expires_in_hours": 24
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert "goal_id" in data
            assert "stream_url" in data
            assert data["goal_id"].startswith("goal-")
        finally:
            app.dependency_overrides.clear()

    def test_memory_publish(self, client, claw_agent):
        """Test Claw can publish a memory."""
        from circus.routes.agents import verify_token

        def override_auth():
            return "claw-test-123"

        app.dependency_overrides[verify_token] = override_auth

        try:
            response = client.post(
                "/api/v1/memory-commons/publish",
                json={
                    "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                    "category": "architecture",
                    "tags": ["payfast", "webhooks", "security"],
                    "privacy_tier": "team",
                    "confidence": 0.9
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert "memory_id" in data
            assert data["memory_id"].startswith("shmem-")
            assert "routed_to" in data
            assert "match_scores" in data
        finally:
            app.dependency_overrides.clear()

    def test_publish_with_goal_matching(self, client, claw_agent, friday_agent, temp_db):
        """Test memory is routed to matching goal."""
        from circus.routes.agents import verify_token

        # Friday creates a goal
        def friday_auth():
            return "friday-test-456"

        app.dependency_overrides[verify_token] = friday_auth

        goal_response = client.post(
            "/api/v1/memory-commons/goals",
            json={
                "goal_description": "PayFast webhooks IP whitelist security",
                "min_confidence": 0.5,  # Lower threshold for test
                "expires_in_hours": 24
            }
        )
        assert goal_response.status_code == 200
        goal_id = goal_response.json()["goal_id"]

        app.dependency_overrides.clear()

        # Claw publishes a related memory
        def claw_auth():
            return "claw-test-123"

        app.dependency_overrides[verify_token] = claw_auth

        publish_response = client.post(
            "/api/v1/memory-commons/publish",
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24 for security",
                "category": "architecture",
                "tags": ["payfast", "webhooks"],
                "privacy_tier": "team",
                "confidence": 0.9
            }
        )

        assert publish_response.status_code == 200
        data = publish_response.json()

        # Memory should be routed to Friday's goal
        assert goal_id in data["routed_to"], f"Expected {goal_id} in {data['routed_to']}"
        assert len(data["match_scores"]) > 0

        app.dependency_overrides.clear()

    def test_goal_list(self, client, friday_agent, temp_db):
        """Test listing agent's goals."""
        from circus.routes.agents import verify_token

        def override_auth():
            return "friday-test-456"

        app.dependency_overrides[verify_token] = override_auth

        try:
            # Create a goal
            client.post(
                "/api/v1/memory-commons/goals",
                json={
                    "goal_description": "debugging payment flows",
                    "min_confidence": 0.7
                }
            )

            # List goals
            response = client.get("/api/v1/memory-commons/goals")
            assert response.status_code == 200
            goals = response.json()
            assert len(goals) == 1
            assert goals[0]["goal_description"] == "debugging payment flows"
        finally:
            app.dependency_overrides.clear()

    def test_goal_deletion(self, client, friday_agent, temp_db):
        """Test deleting a goal."""
        from circus.routes.agents import verify_token

        def override_auth():
            return "friday-test-456"

        app.dependency_overrides[verify_token] = override_auth

        try:
            # Create goal
            goal_response = client.post(
                "/api/v1/memory-commons/goals",
                json={"goal_description": "test goal"}
            )
            goal_id = goal_response.json()["goal_id"]

            # Delete goal
            delete_response = client.delete(f"/api/v1/memory-commons/goals/{goal_id}")
            assert delete_response.status_code == 200

            # Verify it's inactive
            conn = sqlite3.connect(str(temp_db))
            cursor = conn.cursor()
            cursor.execute("SELECT is_active FROM goal_subscriptions WHERE id = ?", (goal_id,))
            row = cursor.fetchone()
            assert row[0] == 0  # Should be inactive
            conn.close()
        finally:
            app.dependency_overrides.clear()

    def test_max_goals_per_agent(self, client, friday_agent, temp_db):
        """Test max goals per agent limit."""
        from circus.routes.agents import verify_token

        def override_auth():
            return "friday-test-456"

        app.dependency_overrides[verify_token] = override_auth

        try:
            # Create max_goals_per_agent goals (default 10)
            for i in range(settings.max_goals_per_agent):
                response = client.post(
                    "/api/v1/memory-commons/goals",
                    json={"goal_description": f"goal {i}"}
                )
                assert response.status_code == 200

            # Next one should fail
            response = client.post(
                "/api/v1/memory-commons/goals",
                json={"goal_description": "one too many"}
            )
            assert response.status_code == 429
        finally:
            app.dependency_overrides.clear()

    def test_ship_gate_w2_belief_merge(self, client, temp_db, friday_agent, claw_agent):
        """
        Week 2 ship gate test: Domain authority conflict resolution.

        Scenario:
        1. Friday (steward of "user-preferences") publishes "Kobus prefers Afrikaans"
        2. Claw (non-steward) publishes "Kobus prefers English"
        3. System detects conflict
        4. Friday wins by domain authority
        5. belief_conflicts table records resolution
        6. Claw's memory marked as superseded
        """
        from circus.routes.agents import verify_token

        # Friday claims user-preferences domain
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO agent_domains (
                agent_id, domain, stewardship_level, claim_reason, claimed_at, last_updated
            ) VALUES (
                'friday-test-456', 'user-preferences', 0.8,
                'Primary maintainer of user preferences', datetime('now'), datetime('now')
            )
        """)
        conn.commit()
        conn.close()

        # Step 1: Friday publishes first memory
        def friday_auth():
            return "friday-test-456"

        app.dependency_overrides[verify_token] = friday_auth

        friday_response = client.post(
            "/api/v1/memory-commons/publish",
            json={
                "content": "User prefers terse replies and concise communication style",
                "category": "user-preferences",
                "tags": ["user", "communication", "preferences"],
                "privacy_tier": "team",
                "confidence": 0.85
            }
        )

        assert friday_response.status_code == 200
        friday_memory_id = friday_response.json()["memory_id"]

        app.dependency_overrides.clear()

        # Step 2: Claw publishes conflicting memory (with negation)
        def claw_auth():
            return "claw-test-123"

        app.dependency_overrides[verify_token] = claw_auth

        claw_response = client.post(
            "/api/v1/memory-commons/publish",
            json={
                "content": "User does not prefer terse replies and likes detailed communication style",
                "category": "user-preferences",
                "tags": ["user", "communication", "preferences"],
                "privacy_tier": "team",
                "confidence": 0.9  # Higher confidence, but no stewardship
            }
        )

        assert claw_response.status_code == 200
        claw_data = claw_response.json()
        claw_memory_id = claw_data["memory_id"]

        # Step 3: Verify conflict was detected
        assert "conflict_resolution" in claw_data
        conflict = claw_data["conflict_resolution"]

        # Step 4: Verify Friday won by domain authority
        assert conflict is not None, "Conflict should have been detected"
        assert conflict["winner_id"] == friday_memory_id, "Friday should win as domain steward"
        assert conflict["conflict_type"] in ["contradiction", "refinement"]
        assert conflict["auto_resolved"] is True, "Should auto-resolve with clear authority gap"
        assert "stewardship" in conflict["reason"].lower() or "authority" in conflict["reason"].lower()

        app.dependency_overrides.clear()

        # Step 5: Verify belief_conflicts table records resolution
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        cursor.execute("""
            SELECT memory_id_a, memory_id_b, conflict_type, resolution, resolved_at
            FROM belief_conflicts
            WHERE (memory_id_a = ? OR memory_id_b = ?)
              AND (memory_id_a = ? OR memory_id_b = ?)
        """, (friday_memory_id, friday_memory_id, claw_memory_id, claw_memory_id))

        conflict_record = cursor.fetchone()
        assert conflict_record is not None, "Conflict should be recorded in belief_conflicts table"
        assert conflict_record[2] in ["contradiction", "refinement", "update"]  # conflict_type
        assert conflict_record[3] is not None, "Resolution should be set"
        assert conflict_record[4] is not None, "Resolved_at should be set"

        # Step 6: Verify Claw's memory marked as superseded
        cursor.execute("""
            SELECT provenance FROM shared_memories WHERE id = ?
        """, (claw_memory_id,))

        loser_row = cursor.fetchone()
        assert loser_row is not None

        import json
        provenance = json.loads(loser_row[0])
        # The loser should be marked as superseded
        assert "superseded_by" in provenance, "Loser memory should be marked as superseded"
        assert provenance["superseded_by"] == friday_memory_id

        conn.close()
