"""Ship gate integration test: Claw publishes → Friday receives via goal SSE.

This test verifies the full Week 1 flow:
1. Friday creates a goal subscription
2. Claw publishes a memory that matches the goal
3. Memory is routed to Friday's goal
4. Friday would receive it via SSE (mocked here since SSE is async)
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import init_database, run_v2_migration
from circus.config import settings


@pytest.fixture
def temp_db():
    """Create temporary database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    original_db = settings.database_path
    settings.database_path = db_path
    settings.memory_commons_enabled = True

    init_database(db_path)
    run_v2_migration(db_path)

    yield db_path

    settings.database_path = original_db
    db_path.unlink(missing_ok=True)


@pytest.fixture
def client(temp_db):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def setup_agents(temp_db):
    """Setup Claw and Friday agents."""
    import sqlite3
    from datetime import datetime

    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    # Create Claw (infra bot)
    cursor.execute("""
        INSERT INTO agents (
            id, name, role, capabilities, home_instance, passport_hash,
            token_hash, trust_score, trust_tier, registered_at, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "claw-ship-gate",
        "Claw",
        "infra-bot",
        '["monitoring", "alerts"]',
        "http://localhost:6200",
        "claw-hash",
        "claw-token",
        72.0,
        "Trusted",
        now,
        now
    ))

    # Create Friday (assistant bot)
    cursor.execute("""
        INSERT INTO agents (
            id, name, role, capabilities, home_instance, passport_hash,
            token_hash, trust_score, trust_tier, registered_at, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "friday-ship-gate",
        "Friday",
        "assistant",
        '["chat", "memory"]',
        "http://localhost:6200",
        "friday-hash",
        "friday-token",
        88.0,
        "Elder",
        now,
        now
    ))

    conn.commit()
    conn.close()

    return {
        "claw": "claw-ship-gate",
        "friday": "friday-ship-gate"
    }


def test_ship_gate_full_flow(client, setup_agents):
    """
    SHIP GATE TEST: Full Week 1 flow verification.

    Verifies:
    1. Friday creates goal subscription
    2. Claw publishes matching memory
    3. Memory is semantically routed to Friday's goal
    4. System returns correct routing info
    """
    from circus.routes.agents import verify_token

    agents = setup_agents

    # Step 1: Friday creates a goal
    def friday_auth():
        return agents["friday"]

    app.dependency_overrides[verify_token] = friday_auth

    goal_response = client.post(
        "/api/v1/memory-commons/goals",
        json={
            "goal_description": "PayFast webhook security and IP whitelisting",
            "min_confidence": 0.5,
            "expires_in_hours": 24
        }
    )

    assert goal_response.status_code == 200, f"Goal creation failed: {goal_response.text}"
    goal_data = goal_response.json()
    goal_id = goal_data["goal_id"]

    print(f"✓ Friday created goal: {goal_id}")

    app.dependency_overrides.clear()

    # Step 2: Claw publishes a matching memory
    def claw_auth():
        return agents["claw"]

    app.dependency_overrides[verify_token] = claw_auth

    publish_response = client.post(
        "/api/v1/memory-commons/publish",
        json={
            "content": "PayFast webhooks require IP whitelist 197.242.158.0/24 for security verification",
            "category": "architecture",
            "tags": ["payfast", "webhooks", "security", "ip-whitelist"],
            "privacy_tier": "team",
            "confidence": 0.9
        }
    )

    assert publish_response.status_code == 200, f"Publish failed: {publish_response.text}"
    publish_data = publish_response.json()

    print(f"✓ Claw published memory: {publish_data['memory_id']}")

    # Step 3: Verify memory was routed to Friday's goal
    assert goal_id in publish_data["routed_to"], (
        f"Memory not routed to Friday's goal!\n"
        f"Expected goal {goal_id} in {publish_data['routed_to']}\n"
        f"Match scores: {publish_data['match_scores']}"
    )

    match_idx = publish_data["routed_to"].index(goal_id)
    match_score = publish_data["match_scores"][match_idx]

    print(f"✓ Memory routed to Friday's goal with match score: {match_score:.2f}")
    print(f"✓ SHIP GATE PASSED: Claw → Friday memory flow works!")

    # Step 4: Verify Friday can list the goal
    app.dependency_overrides[verify_token] = friday_auth

    goals_list = client.get("/api/v1/memory-commons/goals")
    assert goals_list.status_code == 200

    friday_goals = goals_list.json()
    assert len(friday_goals) == 1
    assert friday_goals[0]["id"] == goal_id

    print(f"✓ Friday can list their goal subscriptions")

    app.dependency_overrides.clear()

    # Summary
    print("\n" + "="*60)
    print("SHIP GATE E2E TEST COMPLETE")
    print("="*60)
    print(f"Goal ID: {goal_id}")
    print(f"Memory ID: {publish_data['memory_id']}")
    print(f"Match Score: {match_score:.2%}")
    print(f"Routed To: {len(publish_data['routed_to'])} goal(s)")
    print("="*60)
