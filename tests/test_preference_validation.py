"""Test publish-side validation for preference memories (Week 4, sub-step 4.1)."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import init_database
from circus.config import settings


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    import tempfile
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
    """Test client with fresh database."""
    client = TestClient(app)

    # Register test agent with proper passport
    passport = {
        "identity": {"name": "test-agent", "role": "tester"},
        "capabilities": ["memory", "preference"],
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 2.0, "graph_connections": 10},
        "score": {"total": 7.5},
        "graph_summary": {"entities": []},
        "traits": {}
    }

    register_payload = {
        "name": "test-agent",
        "role": "tester",
        "capabilities": ["memory", "preference"],
        "home": "http://test-instance.local",
        "passport": passport
    }
    response = client.post("/api/v1/agents/register", json=register_payload)
    assert response.status_code == 201, f"Registration failed: {response.json()}"
    token = response.json()["ring_token"]

    # Store token for tests
    client.headers = {"Authorization": f"Bearer {token}"}
    return client


def test_publish_valid_preference_memory(client):
    """Valid preference memory should be accepted and land in shared_memories."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers Afrikaans for bot responses",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus",
            "reasoning": "User explicitly requested Afrikaans"
        },
        "preference": {
            "field": "user.language_preference",
            "value": "af"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 200, f"Publish failed: {response.json()}"
    data = response.json()
    assert "memory_id" in data
    assert data["memory_id"].startswith("shmem-")


def test_publish_preference_with_invalid_field_rejects(client):
    """Preference with field not in allowlist should be rejected with 400."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User wants tool permissions",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus"
        },
        "preference": {
            "field": "user.tool_permissions",  # NOT in allowlist
            "value": "allow_shell"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert "not in allowlist" in response.json()["detail"]


def test_publish_preference_without_preference_object_rejects(client):
    """category=user_preference without preference object should be rejected."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers terse responses",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus"
        }
        # Missing preference object
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert "requires preference object" in response.json()["detail"]


def test_publish_preference_with_wrong_domain_rejects(client):
    """category=user_preference with domain != preference.user should be rejected."""
    payload = {
        "category": "user_preference",
        "domain": "general.other",  # Wrong domain
        "content": "User prefers casual tone",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus"
        },
        "preference": {
            "field": "user.tone_preference",
            "value": "casual"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert "requires domain=preference.user" in response.json()["detail"]


def test_publish_preference_without_owner_id_rejects(client):
    """category=user_preference without provenance.owner_id should be rejected."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers verbose responses",
        "confidence": 0.85,
        "provenance": {
            "reasoning": "Observed in 5 sessions"
            # Missing owner_id
        },
        "preference": {
            "field": "user.response_verbosity",
            "value": "verbose"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert "requires provenance.owner_id" in response.json()["detail"]
