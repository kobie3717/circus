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
    """Valid preference memory should be accepted and land in shared_memories (W5 updated)."""
    from unittest.mock import patch

    # Mock secrets.token_hex to return a predictable value for owner_binding.memory_id
    with patch('secrets.token_hex', return_value='validtest123456'):
        expected_memory_id = "shmem-validtest123456"

        payload = {
            "category": "user_preference",
            "domain": "preference.user",
            "content": "User prefers Afrikaans for bot responses",
            "confidence": 0.85,
            "provenance": {
                "owner_id": "kobus",
                "reasoning": "User explicitly requested Afrikaans",
                "owner_binding": {
                    "agent_id": "agent-test-123",
                    "memory_id": expected_memory_id,
                    "timestamp": "2026-04-19T10:00:00Z",
                    "signature": "dGVzdC1zaWduYXR1cmUtYmFzZTY0"
                }
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
        assert data["memory_id"] == expected_memory_id


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


# Week 5 (5.3): Publish-side owner_binding validation tests


def test_publish_preference_without_owner_binding_returns_400(client):
    """Preference memory without owner_binding should be rejected with 400 (W5 R1)."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers Afrikaans",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus"
            # Missing owner_binding
        },
        "preference": {
            "field": "user.language_preference",
            "value": "af"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == "missing owner_binding"


def test_publish_preference_with_owner_binding_missing_signature_returns_400(client):
    """owner_binding without signature should be rejected with 400 (W5 R2)."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers terse responses",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus",
            "owner_binding": {
                "agent_id": "agent-test-123",
                "memory_id": "shmem-abc123",
                "timestamp": "2026-04-19T10:00:00Z"
                # Missing signature
            }
        },
        "preference": {
            "field": "user.response_verbosity",
            "value": "terse"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == "owner_binding missing signature"


def test_publish_preference_with_owner_binding_missing_agent_id_returns_400(client):
    """owner_binding without agent_id should be rejected with 400 (W5 R2)."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers casual tone",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus",
            "owner_binding": {
                "memory_id": "shmem-abc123",
                "timestamp": "2026-04-19T10:00:00Z",
                "signature": "dGVzdC1zaWduYXR1cmUtYmFzZTY0"
                # Missing agent_id
            }
        },
        "preference": {
            "field": "user.tone_preference",
            "value": "casual"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == "owner_binding missing agent_id"


def test_publish_preference_with_owner_binding_missing_memory_id_returns_400(client):
    """owner_binding without memory_id should be rejected with 400 (W5 R2)."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers markdown format",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus",
            "owner_binding": {
                "agent_id": "agent-test-123",
                "timestamp": "2026-04-19T10:00:00Z",
                "signature": "dGVzdC1zaWduYXR1cmUtYmFzZTY0"
                # Missing memory_id
            }
        },
        "preference": {
            "field": "user.format_preference",
            "value": "markdown"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == "owner_binding missing memory_id"


def test_publish_preference_with_owner_binding_missing_timestamp_returns_400(client):
    """owner_binding without timestamp should be rejected with 400 (W5 R2)."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers verbose explanations",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus",
            "owner_binding": {
                "agent_id": "agent-test-123",
                "memory_id": "shmem-abc123",
                "signature": "dGVzdC1zaWduYXR1cmUtYmFzZTY0"
                # Missing timestamp
            }
        },
        "preference": {
            "field": "user.response_verbosity",
            "value": "verbose"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == "owner_binding missing timestamp"


def test_publish_preference_with_memory_id_mismatch_returns_400(client):
    """owner_binding.memory_id not matching actual memory_id should be rejected (W5 R3)."""
    payload = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "User prefers Afrikaans",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus",
            "owner_binding": {
                "agent_id": "agent-test-123",
                "memory_id": "shmem-wrongid",  # This won't match the server-generated ID
                "timestamp": "2026-04-19T10:00:00Z",
                "signature": "dGVzdC1zaWduYXR1cmUtYmFzZTY0"
            }
        },
        "preference": {
            "field": "user.language_preference",
            "value": "af"
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == "owner_binding memory_id mismatch"


def test_publish_preference_with_well_formed_binding_passes_shape_validation(client):
    """Preference with all owner_binding fields should pass shape validation (W5 R5).

    Note: Signature may be garbage — cryptographic verification is admission's job.
    This test only validates that publish-side accepts well-formed structure.
    """
    # First, make a request to get a memory_id, then use that in the binding
    # Actually, we need to know the memory_id in advance. Since it's server-generated,
    # we'll need to mock or pre-determine it. For now, let's test that the validation
    # logic accepts a properly structured request.

    # Import to generate a predictable memory_id for testing
    import secrets
    from unittest.mock import patch

    # Mock secrets.token_hex to return a predictable value
    with patch('secrets.token_hex', return_value='0123456789abcdef'):
        expected_memory_id = "shmem-0123456789abcdef"

        payload = {
            "category": "user_preference",
            "domain": "preference.user",
            "content": "User prefers Afrikaans",
            "confidence": 0.85,
            "provenance": {
                "owner_id": "kobus",
                "owner_binding": {
                    "agent_id": "agent-test-123",
                    "memory_id": expected_memory_id,  # Matches the mocked generated ID
                    "timestamp": "2026-04-19T10:00:00Z",
                    "signature": "dGVzdC1zaWduYXR1cmUtYmFzZTY0"  # Garbage signature, admission will verify
                }
            },
            "preference": {
                "field": "user.language_preference",
                "value": "af"
            }
        }

        response = client.post("/api/v1/memory-commons/publish", json=payload)
        # Should pass publish-side validation (200 or 201)
        # May be skipped at admission-side due to invalid signature, but that's not this test's concern
        assert response.status_code == 200, f"Publish failed: {response.json()}"
        data = response.json()
        assert data["memory_id"] == expected_memory_id


def test_publish_non_preference_with_owner_binding_is_ignored(client):
    """Non-preference memory with owner_binding should be accepted (W5 R6).

    owner_binding is only required for preference memories. For other categories,
    it's structurally harmless and should be ignored.
    """
    payload = {
        "category": "belief",
        "domain": "general.testing",
        "content": "Testing shows that owner_binding is harmless on non-preferences",
        "confidence": 0.9,
        "provenance": {
            "owner_id": "kobus",
            "owner_binding": {
                "agent_id": "agent-test-123",
                "memory_id": "shmem-whatever",
                "timestamp": "2026-04-19T10:00:00Z",
                "signature": "dGVzdC1zaWduYXR1cmUtYmFzZTY0"
            }
        }
    }

    response = client.post("/api/v1/memory-commons/publish", json=payload)
    assert response.status_code == 200, f"Publish failed: {response.json()}"
    data = response.json()
    assert "memory_id" in data
