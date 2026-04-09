"""Test per-domain competence scoring and theory of mind briefing."""

import pytest
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import get_db
from circus.services.briefing import (
    calculate_average_competence,
    generate_boot_briefing,
    get_agent_competence,
    record_competence_observation,
)


@pytest.fixture
def client():
    """Test client fixture."""
    return TestClient(app)


@pytest.fixture
def test_agent(client):
    """Create a test agent with passport."""
    import uuid

    # Use unique name to avoid conflicts
    unique_name = f"Test Agent {uuid.uuid4().hex[:8]}"

    passport = {
        "identity": {"name": unique_name},
        "score": {"total": 7.5},
        "predictions": {"confirmed": 8, "refuted": 2},
        "beliefs": {"total": 10, "contradictions": 1},
        "memory_stats": {"proof_count_avg": 3.2, "graph_connections": 15}
    }

    response = client.post("/api/v1/agents/register", json={
        "name": unique_name,
        "role": "tester",
        "capabilities": ["testing", "coding"],
        "home": "https://test.example.com",
        "passport": passport
    })

    assert response.status_code == 201, f"Registration failed: {response.json()}"
    data = response.json()
    return {
        "agent_id": data["agent_id"],
        "token": data["ring_token"],
        "name": unique_name
    }


def test_record_competence_observation_new_domain(test_agent):
    """Test recording a competence observation for a new domain."""
    agent_id = test_agent["agent_id"]

    # Record successful coding observation
    result = record_competence_observation(
        agent_id=agent_id,
        domain="coding",
        success=True,
        weight=1.0
    )

    assert result["domain"] == "coding"
    assert result["score"] == 1.0  # First observation is successful
    assert result["observations"] == 1
    assert "last_updated" in result


def test_record_competence_observation_weighted_average(test_agent):
    """Test weighted moving average calculation."""
    agent_id = test_agent["agent_id"]

    # Record multiple observations
    record_competence_observation(agent_id, "research", success=True, weight=1.0)
    record_competence_observation(agent_id, "research", success=True, weight=1.0)
    record_competence_observation(agent_id, "research", success=False, weight=1.0)

    result = record_competence_observation(agent_id, "research", success=True, weight=1.0)

    # (1.0*1 + 1.0*1 + 0.0*1 + 1.0*1) / 4 = 0.75
    assert result["score"] == 0.75
    assert result["observations"] == 4


def test_get_agent_competence(test_agent):
    """Test retrieving agent competence scores."""
    agent_id = test_agent["agent_id"]

    # Record observations in multiple domains
    record_competence_observation(agent_id, "coding", success=True, weight=1.0)
    record_competence_observation(agent_id, "testing", success=True, weight=1.0)
    record_competence_observation(agent_id, "research", success=False, weight=1.0)

    competencies = get_agent_competence(agent_id)

    assert len(competencies) == 3
    assert any(c["domain"] == "coding" for c in competencies)
    assert any(c["domain"] == "testing" for c in competencies)
    assert any(c["domain"] == "research" for c in competencies)

    # Should be sorted by score DESC
    assert competencies[0]["score"] >= competencies[1]["score"]


def test_calculate_average_competence(test_agent):
    """Test calculating average competence across domains."""
    agent_id = test_agent["agent_id"]

    # Record observations
    record_competence_observation(agent_id, "coding", success=True, weight=1.0)  # 1.0
    record_competence_observation(agent_id, "testing", success=True, weight=1.0)
    record_competence_observation(agent_id, "testing", success=False, weight=1.0)  # 0.5

    avg = calculate_average_competence(agent_id)

    # Average of 1.0 and 0.5 = 0.75
    assert avg == 0.75


def test_calculate_average_competence_no_observations(test_agent):
    """Test average competence for agent with no observations."""
    agent_id = test_agent["agent_id"]
    avg = calculate_average_competence(agent_id)

    # Should return neutral 0.5
    assert avg == 0.5


def test_generate_boot_briefing_empty():
    """Test boot briefing generation with no competent agents."""
    briefing = generate_boot_briefing()

    assert "briefing" in briefing
    assert "agents" in briefing
    assert "generated_at" in briefing
    # Note: May have agents from other tests, just verify structure
    assert isinstance(briefing["agents"], list)
    assert isinstance(briefing["briefing"], str)


def test_generate_boot_briefing_with_agents(test_agent):
    """Test boot briefing generation with competent agents."""
    agent_id = test_agent["agent_id"]
    agent_name = test_agent["name"]

    # Record observations
    record_competence_observation(agent_id, "coding", success=True, weight=1.0)
    record_competence_observation(agent_id, "testing", success=True, weight=1.0)

    briefing = generate_boot_briefing()

    # Find our agent in the briefing
    our_agent = None
    for agent in briefing["agents"]:
        if agent["agent_id"] == agent_id:
            our_agent = agent
            break

    assert our_agent is not None
    assert our_agent["name"] == agent_name
    assert len(our_agent["top_domains"]) == 2
    assert agent_name in briefing["briefing"]


def test_api_record_competence(client, test_agent):
    """Test POST /agents/{agent_id}/competence API endpoint."""
    agent_id = test_agent["agent_id"]
    token = test_agent["token"]

    response = client.post(
        f"/api/v1/agents/{agent_id}/competence",
        json={
            "domain": "coding",
            "success": True,
            "weight": 1.0
        },
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == agent_id
    assert data["domain"] == "coding"
    assert data["new_score"] == 1.0
    assert data["observations"] == 1


def test_api_record_competence_invalid_domain(client, test_agent):
    """Test recording competence with invalid domain."""
    agent_id = test_agent["agent_id"]
    token = test_agent["token"]

    response = client.post(
        f"/api/v1/agents/{agent_id}/competence",
        json={
            "domain": "invalid-domain",
            "success": True,
            "weight": 1.0
        },
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 400
    assert "Invalid domain" in response.json()["detail"]


def test_api_record_competence_unauthorized(client, test_agent):
    """Test recording competence for another agent (should fail)."""
    agent_id = test_agent["agent_id"]
    token = test_agent["token"]

    # Try to record for non-existent agent
    response = client.post(
        f"/api/v1/agents/other-agent-id/competence",
        json={
            "domain": "coding",
            "success": True,
            "weight": 1.0
        },
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403


def test_api_get_agent_competence(client, test_agent):
    """Test GET /agents/{agent_id}/competence API endpoint."""
    agent_id = test_agent["agent_id"]

    # Record some observations
    record_competence_observation(agent_id, "coding", success=True, weight=1.0)
    record_competence_observation(agent_id, "testing", success=True, weight=1.0)

    response = client.get(f"/api/v1/agents/{agent_id}/competence")

    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == agent_id
    assert data["count"] == 2
    assert len(data["competencies"]) == 2


def test_api_get_boot_briefing(client, test_agent):
    """Test GET /agents/briefing/boot API endpoint."""
    agent_id = test_agent["agent_id"]

    # Record observations
    record_competence_observation(agent_id, "coding", success=True, weight=1.0)

    response = client.get("/api/v1/agents/briefing/boot")

    assert response.status_code == 200
    data = response.json()
    assert "briefing" in data
    assert "agents" in data
    assert "generated_at" in data
    assert len(data["agents"]) >= 1


def test_competence_in_agent_response(client, test_agent):
    """Test that competence is included in agent responses."""
    agent_id = test_agent["agent_id"]

    # Record observations
    record_competence_observation(agent_id, "coding", success=True, weight=1.0)
    record_competence_observation(agent_id, "testing", success=True, weight=1.0)

    # Get agent details
    response = client.get(f"/api/v1/agents/{agent_id}")

    assert response.status_code == 200
    data = response.json()
    assert "competence" in data
    assert data["competence"] is not None
    assert len(data["competence"]) == 2
    assert data["competence"][0]["domain"] in ["coding", "testing"]
    assert 0.0 <= data["competence"][0]["score"] <= 1.0


def test_room_briefing(client, test_agent):
    """Test room-specific briefing generation."""
    token = test_agent["token"]
    agent_id = test_agent["agent_id"]

    # First, we need to boost trust to create a room
    # Record some competence to boost trust via bonus
    for _ in range(5):
        record_competence_observation(agent_id, "coding", success=True, weight=1.0)

    # Get default room
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM rooms LIMIT 1")
        room = cursor.fetchone()
        if not room:
            pytest.skip("No default rooms available")

        room_id = room["id"]

        # Join the room
        cursor.execute("""
            INSERT OR IGNORE INTO room_members (room_id, agent_id, joined_at)
            VALUES (?, ?, datetime('now'))
        """, (room_id, agent_id))
        conn.commit()

    # Get room briefing
    response = client.get(f"/api/v1/rooms/{room_id}/briefing")

    assert response.status_code == 200
    data = response.json()
    assert "briefing" in data
    assert "agents" in data
