"""Test agent registration and management."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from circus.app import app
from circus.config import settings
from circus.database import init_database


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    original_path = settings.database_path
    settings.database_path = db_path
    init_database(db_path)

    yield db_path

    settings.database_path = original_path
    db_path.unlink()


@pytest.fixture
def client(temp_db):
    """Create test client."""
    return TestClient(app)


def test_agent_registration(client):
    """Test agent registration."""
    passport = {
        "identity": {"name": "Test Agent", "role": "testing"},
        "capabilities": ["testing", "debugging"],
        "predictions": {"confirmed": 8, "refuted": 2},
        "beliefs": {"total": 10, "contradictions": 1},
        "memory_stats": {
            "proof_count_avg": 2.5,
            "graph_connections": 15
        },
        "score": {"total": 7.5},
        "graph_summary": {"entities": []},
        "traits": {}
    }

    response = client.post("/api/v1/agents/register", json={
        "name": "Test Agent",
        "role": "testing",
        "capabilities": ["testing", "debugging"],
        "home": "http://test.example.com",
        "passport": passport,
        "contact": "@test"
    })

    assert response.status_code == 201
    data = response.json()

    assert "agent_id" in data
    assert "ring_token" in data
    assert "trust_score" in data
    assert "trust_tier" in data
    assert data["trust_tier"] in ["Newcomer", "Established", "Trusted", "Elder"]


def test_agent_discovery(client):
    """Test agent discovery."""
    # First register an agent
    passport = {
        "identity": {"name": "Discoverable Agent", "role": "testing"},
        "capabilities": ["code-review", "testing"],
        "predictions": {"confirmed": 10, "refuted": 0},
        "beliefs": {"total": 5, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.0, "graph_connections": 20},
        "score": {"total": 8.0},
        "graph_summary": {"entities": []},
        "traits": {}
    }

    reg_response = client.post("/api/v1/agents/register", json={
        "name": "Discoverable Agent",
        "role": "testing",
        "capabilities": ["code-review", "testing"],
        "home": "http://discover.example.com",
        "passport": passport
    })

    assert reg_response.status_code == 201

    # Now discover
    response = client.get("/api/v1/agents/discover?capability=code-review")

    assert response.status_code == 200
    data = response.json()

    assert data["count"] > 0
    assert len(data["agents"]) > 0
    assert any(a["name"] == "Discoverable Agent" for a in data["agents"])


def test_passport_refresh(client):
    """Test passport refresh."""
    # Register agent
    passport = {
        "identity": {"name": "Refresh Test", "role": "testing"},
        "capabilities": ["testing"],
        "predictions": {"confirmed": 5, "refuted": 5},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 2.0, "graph_connections": 10},
        "score": {"total": 6.0},
        "graph_summary": {"entities": []},
        "traits": {}
    }

    reg_response = client.post("/api/v1/agents/register", json={
        "name": "Refresh Test",
        "role": "testing",
        "capabilities": ["testing"],
        "home": "http://refresh.example.com",
        "passport": passport
    })

    assert reg_response.status_code == 201
    agent_id = reg_response.json()["agent_id"]
    token = reg_response.json()["ring_token"]

    # Refresh with improved passport
    improved_passport = {
        "identity": {"name": "Refresh Test", "role": "testing"},
        "capabilities": ["testing"],
        "predictions": {"confirmed": 10, "refuted": 2},
        "beliefs": {"total": 15, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.5, "graph_connections": 25},
        "score": {"total": 8.5},
        "graph_summary": {"entities": []},
        "traits": {}
    }

    response = client.put(
        f"/api/v1/agents/{agent_id}/passport",
        json={"passport": improved_passport},
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()

    assert data["trust_score"] >= reg_response.json()["trust_score"]
    assert data["passport_age_days"] == 0


def test_get_agent(client):
    """Test getting agent by ID."""
    # Register agent
    passport = {
        "identity": {"name": "Get Test", "role": "testing"},
        "capabilities": ["testing"],
        "predictions": {"confirmed": 5, "refuted": 0},
        "beliefs": {"total": 5, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 2.0, "graph_connections": 10},
        "score": {"total": 7.0},
        "graph_summary": {"entities": []},
        "traits": {}
    }

    reg_response = client.post("/api/v1/agents/register", json={
        "name": "Get Test",
        "role": "testing",
        "capabilities": ["testing"],
        "home": "http://get.example.com",
        "passport": passport
    })

    agent_id = reg_response.json()["agent_id"]

    # Get agent
    response = client.get(f"/api/v1/agents/{agent_id}")

    assert response.status_code == 200
    data = response.json()

    assert data["agent_id"] == agent_id
    assert data["name"] == "Get Test"
    assert data["role"] == "testing"
