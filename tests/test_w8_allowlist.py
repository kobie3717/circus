"""Tests for W8: Safe Allowlist Expansion.

Tests:
- Unknown field rejected (field_not_allowlisted)
- Invalid value rejected (value_not_valid)
- Free text field accepted (timezone)
- New field admitted (code_style)
- Allowlist API returns all fields
- Allowlist API single field
- Field-specific threshold (confirmation_style needs 0.8+)
- CLI allowlist list runs
"""

import base64
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import get_db, init_database
from circus.config import settings
from circus.services.bundle_signing import canonicalize_for_signing


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
def reset_server_owner():
    """Reset cached server owner between tests."""
    import circus.services.preference_admission as admission_module
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False
    # Clean owner_keys before test
    with get_db() as conn:
        conn.execute("DELETE FROM owner_keys")
        conn.commit()
    yield
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False
    # Clean up after test
    with get_db() as conn:
        conn.execute("DELETE FROM owner_keys")
        conn.commit()


@pytest.fixture
def kobus_private_key(temp_db):
    """Generate Ed25519 keypair and seed owner_keys table for 'kobus' in temp DB."""
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
            ("kobus", public_key_b64, datetime.utcnow().isoformat())
        )
        conn.commit()

    yield private_key

    with get_db() as conn:
        conn.execute("DELETE FROM owner_keys WHERE owner_id = 'kobus'")
        conn.commit()


def _sign_owner_binding(private_key, owner_id: str, agent_id: str, memory_id: str, timestamp: str) -> str:
    """Sign owner binding payload and return base64 signature."""
    payload = {
        "agent_id": agent_id,
        "memory_id": memory_id,
        "owner_id": owner_id,
        "timestamp": timestamp,
    }
    canonical_bytes = canonicalize_for_signing(payload)
    signature = private_key.sign(canonical_bytes)
    return base64.b64encode(signature).decode('ascii')


@pytest.fixture
def client(temp_db, reset_server_owner, kobus_private_key):
    """Test client with fresh database and registered agent."""
    client = TestClient(app)

    # Register test agent
    passport = {
        "identity": {"name": "test-agent", "role": "tester"},
        "capabilities": ["memory", "preference"],
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 2.0, "graph_connections": 10},
        "score": {"total": 5.5}
    }

    response = client.post("/api/v1/agents/register", json={
        "name": "test-agent",
        "role": "tester",
        "capabilities": ["memory", "preference"],
        "home": "http://localhost:6200",
        "passport": passport
    })

    assert response.status_code in [200, 201]
    result = response.json()
    token = result["ring_token"]
    agent_id = result["agent_id"]

    return client, token, agent_id, kobus_private_key


def test_unknown_field_rejected(client, reset_server_owner):
    """Test: Unknown field is rejected at admission with reason=field_not_allowlisted."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        memory_id = "shmem-aaaa000000000000"
        timestamp = datetime.utcnow().isoformat()
        signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers dark mode",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.85,
                "privacy_tier": "public",
                "preference": {
                    "field": "user.theme_preference",  # Not in allowlist
                    "value": "dark"
                },
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id,
                        "timestamp": timestamp,
                        "signature": signature
                    }
                }
            }
        )

        # Publish-side gate should reject unknown field
        assert response.status_code == 400
        assert "not in allowlist" in response.json()["detail"]


def test_invalid_value_rejected(client, reset_server_owner):
    """Test: Invalid value is rejected at publish with value_not_valid."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        memory_id = "shmem-bbbb000000000000"
        timestamp = datetime.utcnow().isoformat()
        signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers Klingon",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.85,
                "privacy_tier": "public",
                "preference": {
                    "field": "user.language_preference",
                    "value": "klingon"  # Not in valid_values
                },
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id,
                        "timestamp": timestamp,
                        "signature": signature
                    }
                }
            }
        )

        # Publish-side gate should reject invalid value
        assert response.status_code == 400
        assert "not in valid_values" in response.json()["detail"]


def test_free_text_field_accepted(client, reset_server_owner):
    """Test: Free text field (timezone) accepts any string value."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        memory_id = "shmem-cccc000000000000"
        timestamp = datetime.utcnow().isoformat()
        signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User is in South Africa",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.95,
                "privacy_tier": "public",
                "preference": {
                    "field": "user.timezone",
                    "value": "Africa/Johannesburg"  # Free text, any IANA timezone
                },
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id,
                        "timestamp": timestamp,
                        "signature": signature
                    }
                }
            }
        )

        assert response.status_code in [200, 201]
        data = response.json()
        assert data["preference_activated"] is True

        # Verify no value_valid gate (free text fields skip value validation)
        gates = data["decision_trace"]["gates"]
        gate_names = [g["gate"] for g in gates]
        assert "field_allowlisted" in gate_names
        # value_valid gate should not exist for free text fields
        value_gates = [g for g in gates if g["gate"] == "value_valid"]
        assert len(value_gates) == 0  # No value validation for free text


def test_new_field_admitted(client, reset_server_owner):
    """Test: New W8 field (user.code_style) is admitted successfully."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        memory_id = "shmem-dddd000000000000"
        timestamp = datetime.utcnow().isoformat()
        signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers concise code",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.80,
                "privacy_tier": "public",
                "preference": {
                    "field": "user.code_style",
                    "value": "concise"
                },
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id,
                        "timestamp": timestamp,
                        "signature": signature
                    }
                }
            }
        )

        assert response.status_code in [200, 201]
        data = response.json()
        assert data["preference_activated"] is True

        # Verify it's in active_preferences
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value FROM active_preferences
                WHERE owner_id = ? AND field_name = ?
            """, ("kobus", "user.code_style"))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "concise"


def test_allowlist_api_returns_all_fields(client):
    """Test: GET /allowlist returns all 9 fields."""
    test_client, token, agent_id, private_key = client

    # Public endpoint - no auth required
    response = test_client.get("/api/v1/memory-commons/allowlist")

    assert response.status_code == 200
    data = response.json()

    assert data["count"] == 9
    assert len(data["fields"]) == 9

    # Check field structure
    field = data["fields"][0]
    assert "name" in field
    assert "description" in field
    assert "valid_values" in field
    assert "default" in field
    assert "activation_threshold" in field
    assert "category" in field

    # Verify new W8 fields present
    field_names = [f["name"] for f in data["fields"]]
    assert "user.code_style" in field_names
    assert "user.explanation_depth" in field_names
    assert "user.confirmation_style" in field_names
    assert "user.timezone" in field_names
    assert "agent.proactive_suggestions" in field_names


def test_allowlist_api_single_field(client):
    """Test: GET /allowlist/{field_name} returns field metadata."""
    test_client, token, agent_id, private_key = client

    # Public endpoint - no auth required
    response = test_client.get("/api/v1/memory-commons/allowlist/user.language_preference")

    assert response.status_code == 200
    data = response.json()

    assert data["name"] == "user.language_preference"
    assert data["description"] == "Preferred response language"
    assert data["valid_values"] == ["en", "af", "pt", "es", "fr"]
    assert data["default"] == "en"
    assert data["activation_threshold"] == 0.7
    assert data["category"] == "communication"

    # Test 404 for unknown field
    response = test_client.get("/api/v1/memory-commons/allowlist/user.unknown_field")
    assert response.status_code == 404


def test_field_specific_threshold(client, reset_server_owner):
    """Test: Field-specific threshold (confirmation_style needs 0.8+)."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Test with confidence 0.65 (well below field threshold 0.8, even after trust adjustment)
        memory_id = "shmem-eeee000000000000"
        timestamp = datetime.utcnow().isoformat()
        signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User never wants confirmation",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.65,  # Below 0.8 threshold (even after trust boost)
                "privacy_tier": "public",
                "preference": {
                    "field": "user.confirmation_style",
                    "value": "never"
                },
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id,
                        "timestamp": timestamp,
                        "signature": signature
                    }
                }
            }
        )

        assert response.status_code in [200, 201]
        data = response.json()
        assert data["preference_activated"] is False  # Below threshold

        # Check decision trace shows field-specific threshold
        gates = data["decision_trace"]["gates"]
        conf_gate = next(g for g in gates if g["gate"] == "confidence_threshold")
        assert conf_gate["passed"] is False
        assert conf_gate["threshold"] == 0.8  # Field-specific threshold

        # Test with confidence 0.90 (well above field threshold 0.8)
        memory_id_2 = "shmem-ffff000000000000"
        timestamp_2 = datetime.utcnow().isoformat()
        signature_2 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_2, timestamp_2)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User never wants confirmation (high confidence)",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.90,  # Well above 0.8 threshold
                "privacy_tier": "public",
                "preference": {
                    "field": "user.confirmation_style",
                    "value": "never"
                },
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id_2,
                        "timestamp": timestamp_2,
                        "signature": signature_2
                    }
                }
            }
        )

        assert response.status_code in [200, 201]
        data = response.json()
        assert data["preference_activated"] is True  # Above threshold

        # Verify field-specific threshold was used
        gates = data["decision_trace"]["gates"]
        conf_gate = next(g for g in gates if g["gate"] == "confidence_threshold")
        assert conf_gate["passed"] is True
        assert conf_gate["threshold"] == 0.8  # Field-specific threshold (not global 0.7)


def test_cli_allowlist_list():
    """Test: CLI allowlist list runs without error."""
    from circus.cli import CircusCLI
    from argparse import Namespace

    cli = CircusCLI()
    args = Namespace()

    # Should not raise
    cli.allowlist_list(args)


def test_cli_allowlist_show():
    """Test: CLI allowlist show displays field details."""
    from circus.cli import CircusCLI
    from argparse import Namespace

    cli = CircusCLI()
    args = Namespace(field="user.code_style")

    # Should not raise
    cli.allowlist_show(args)
