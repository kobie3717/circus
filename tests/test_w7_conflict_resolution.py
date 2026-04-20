"""Tests for W7: Confidence-Weighted Conflict Resolution.

Tests:
- No conflict on first publish
- Same value is idempotent
- Higher confidence wins
- Lower confidence rejected
- Tie-break by recency
- Conflict count increments
- Conflict stats endpoint
- Decision trace includes conflict gate
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


def test_no_conflict_first_publish(client, reset_server_owner):
    """Test: First publish has no conflict, new_wins."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        memory_id = "shmem-abcdef1234567890"  # 16 hex chars
        timestamp = datetime.utcnow().isoformat()

        signature = _sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id=agent_id,
            memory_id=memory_id,
            timestamp=timestamp
        )

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers Afrikaans",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.85,
                "privacy_tier": "public",
                "preference": {
                    "field": "user.language_preference",
                    "value": "af"
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
        assert "decision_trace" in data

        # Check conflict_resolution gate
        gates = data["decision_trace"]["gates"]
        conflict_gate = next(g for g in gates if g["gate"] == "conflict_resolution")
        assert conflict_gate["passed"] is True
        assert conflict_gate["resolution"] == "new_wins"
        assert conflict_gate["existing_confidence"] is None

        # Verify conflict_count is 0
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT conflict_count FROM active_preferences
                WHERE owner_id = ? AND field_name = ?
            """, ("kobus", "user.language_preference"))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 0


def test_same_value_idempotent(client, reset_server_owner):
    """Test: Publishing same field+value twice is idempotent, no conflict."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # First publish
        memory_id_1 = "shmem-1111111111111111"  # 16 hex chars
        timestamp_1 = datetime.utcnow().isoformat()
        signature_1 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_1, timestamp_1)

        test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers Afrikaans",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.85,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "af"},
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id_1,
                        "timestamp": timestamp_1,
                        "signature": signature_1
                    }
                }
            }
        )

        # Second publish (same value)
        memory_id_2 = "shmem-2222222222222222"  # 16 hex chars
        timestamp_2 = datetime.utcnow().isoformat()
        signature_2 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_2, timestamp_2)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers Afrikaans (reconfirmed)",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.87,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "af"},
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
        assert data["preference_activated"] is True

        gates = data["decision_trace"]["gates"]
        conflict_gate = next(g for g in gates if g["gate"] == "conflict_resolution")
        assert conflict_gate["passed"] is True
        assert conflict_gate["resolution"] == "new_wins"
        assert "idempotent" in conflict_gate["reason"].lower()

        # Verify conflict_count is still 0 (idempotent, not a conflict)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT conflict_count FROM active_preferences
                WHERE owner_id = ? AND field_name = ?
            """, ("kobus", "user.language_preference"))
            assert cursor.fetchone()[0] == 0


def test_higher_confidence_wins(client, reset_server_owner):
    """Test: New preference with higher confidence overwrites existing."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # First publish: confidence 0.70
        memory_id_1 = "shmem-aaaaaaaaaaaaaaaa"  # 16 hex chars
        timestamp_1 = datetime.utcnow().isoformat()
        signature_1 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_1, timestamp_1)

        test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers English",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.70,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "en"},
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id_1,
                        "timestamp": timestamp_1,
                        "signature": signature_1
                    }
                }
            }
        )

        # Second publish: confidence 0.90 (higher by >0.05)
        memory_id_2 = "shmem-bbbbbbbbbbbbbbbb"  # 16 hex chars
        timestamp_2 = datetime.utcnow().isoformat()
        signature_2 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_2, timestamp_2)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User actually prefers Afrikaans",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.90,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "af"},
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
        assert data["preference_activated"] is True

        gates = data["decision_trace"]["gates"]
        conflict_gate = next(g for g in gates if g["gate"] == "conflict_resolution")
        assert conflict_gate["passed"] is True
        assert conflict_gate["resolution"] == "new_wins"
        assert conflict_gate["existing_confidence"] is not None
        assert conflict_gate["new_confidence"] > conflict_gate["existing_confidence"]

        # Verify conflict_count incremented
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value, conflict_count FROM active_preferences
                WHERE owner_id = ? AND field_name = ?
            """, ("kobus", "user.language_preference"))
            row = cursor.fetchone()
            assert row[0] == "af"  # New value won
            assert row[1] == 1  # Conflict count incremented


def test_lower_confidence_rejected(client, reset_server_owner):
    """Test: New preference with lower confidence is rejected."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # First publish: confidence 0.90
        memory_id_1 = "shmem-cccccccccccccccc"  # 16 hex chars
        timestamp_1 = datetime.utcnow().isoformat()
        signature_1 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_1, timestamp_1)

        test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers Afrikaans",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.90,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "af"},
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id_1,
                        "timestamp": timestamp_1,
                        "signature": signature_1
                    }
                }
            }
        )

        # Second publish: confidence 0.70 (lower by >0.05)
        memory_id_2 = "shmem-dddddddddddddddd"  # 16 hex chars
        timestamp_2 = datetime.utcnow().isoformat()
        signature_2 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_2, timestamp_2)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User might prefer English",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.70,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "en"},
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
        assert data["preference_activated"] is False  # Rejected

        gates = data["decision_trace"]["gates"]
        conflict_gate = next(g for g in gates if g["gate"] == "conflict_resolution")
        assert conflict_gate["passed"] is False
        assert conflict_gate["resolution"] == "existing_wins"

        # Verify existing value still active
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value, conflict_count FROM active_preferences
                WHERE owner_id = ? AND field_name = ?
            """, ("kobus", "user.language_preference"))
            row = cursor.fetchone()
            assert row[0] == "af"  # Original value still active
            assert row[1] == 0  # Conflict count NOT incremented (existing won)


def test_tie_recency_wins(client, reset_server_owner):
    """Test: When confidence within threshold, recency wins."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # First publish: confidence 0.75
        memory_id_1 = "shmem-eeeeeeeeeeeeeeee"  # 16 hex chars
        timestamp_1 = datetime.utcnow().isoformat()
        signature_1 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_1, timestamp_1)

        test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers English",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.75,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "en"},
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id_1,
                        "timestamp": timestamp_1,
                        "signature": signature_1
                    }
                }
            }
        )

        # Second publish: confidence 0.77 (within 0.05 threshold)
        memory_id_2 = "shmem-ffffffffffffffff"  # 16 hex chars
        timestamp_2 = datetime.utcnow().isoformat()
        signature_2 = _sign_owner_binding(private_key, "kobus", agent_id, memory_id_2, timestamp_2)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers Afrikaans",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.77,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "af"},
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
        assert data["preference_activated"] is True

        gates = data["decision_trace"]["gates"]
        conflict_gate = next(g for g in gates if g["gate"] == "conflict_resolution")
        assert conflict_gate["passed"] is True
        assert conflict_gate["resolution"] == "new_wins"
        assert "recency" in conflict_gate["reason"].lower()

        # Verify new value won and conflict_count incremented
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value, conflict_count FROM active_preferences
                WHERE owner_id = ? AND field_name = ?
            """, ("kobus", "user.language_preference"))
            row = cursor.fetchone()
            assert row[0] == "af"  # New value won
            assert row[1] == 1  # Conflict count incremented


def test_conflict_count_increments(client, reset_server_owner):
    """Test: Conflict count increments with each contested update."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Publish 1: en, 0.70
        mem_ids = ["shmem-1010101010101010", "shmem-2020202020202020", "shmem-3030303030303030", "shmem-4040404040404040"]
        for i, (value, conf) in enumerate([("en", 0.70), ("af", 0.82), ("en", 0.85), ("af", 0.88)]):
            memory_id = mem_ids[i]
            timestamp = datetime.utcnow().isoformat()
            signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

            test_client.post(
                "/api/v1/memory-commons/publish",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "content": f"User prefers {value}",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "confidence": conf,
                    "privacy_tier": "public",
                    "preference": {"field": "user.language_preference", "value": value},
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

        # Verify conflict_count is 3 (3 successful overwrites after first publish)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value, conflict_count FROM active_preferences
                WHERE owner_id = ? AND field_name = ?
            """, ("kobus", "user.language_preference"))
            row = cursor.fetchone()
            assert row[0] == "af"  # Last value
            assert row[1] == 3  # 3 conflicts


def test_conflict_stats_endpoint(client, reset_server_owner):
    """Test: GET /preferences/{owner_id}/conflicts returns contested fields."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Publish two contested preferences
        # Field 1: language_preference with conflict
        mem_ids_lang = {"en": "shmem-aaaa000000000000", "af": "shmem-bbbb000000000000"}
        for value, conf in [("en", 0.75), ("af", 0.85)]:
            memory_id = mem_ids_lang[value]
            timestamp = datetime.utcnow().isoformat()
            signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

            test_client.post(
                "/api/v1/memory-commons/publish",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "content": f"User prefers {value}",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "confidence": conf,
                    "privacy_tier": "public",
                    "preference": {"field": "user.language_preference", "value": value},
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

        # Field 2: verbosity with no conflict (same value twice)
        mem_ids_verb = ["shmem-cccc000000000000", "shmem-dddd000000000000"]
        for idx in range(2):
            memory_id = mem_ids_verb[idx]
            timestamp = datetime.utcnow().isoformat()
            signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

            test_client.post(
                "/api/v1/memory-commons/publish",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "content": "User prefers terse responses",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "confidence": 0.80,
                    "privacy_tier": "public",
                    "preference": {"field": "user.response_verbosity", "value": "terse"},
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

        # Get conflicts
        response = test_client.get(
            "/api/v1/memory-commons/preferences/kobus/conflicts",
            headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code in [200, 201]
        data = response.json()

        assert data["owner_id"] == "kobus"
        assert data["count"] == 1  # Only language_preference has conflict
        assert len(data["conflicts"]) == 1

        conflict = data["conflicts"][0]
        assert conflict["field"] == "user.language_preference"
        assert conflict["value"] == "af"
        assert conflict["conflict_count"] == 1
        assert conflict["confidence"] >= 0.80


def test_decision_trace_includes_conflict_gate(client, reset_server_owner):
    """Test: Publish response includes conflict_resolution gate in decision_trace."""
    test_client, token, agent_id, private_key = client

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        memory_id = "shmem-9999999999999999"  # 16 hex chars
        timestamp = datetime.utcnow().isoformat()
        signature = _sign_owner_binding(private_key, "kobus", agent_id, memory_id, timestamp)

        response = test_client.post(
            "/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": "User prefers Afrikaans",
                "category": "user_preference",
                "domain": "preference.user",
                "confidence": 0.85,
                "privacy_tier": "public",
                "preference": {"field": "user.language_preference", "value": "af"},
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

        assert "decision_trace" in data
        gates = data["decision_trace"]["gates"]

        # Find conflict_resolution gate
        conflict_gate = next((g for g in gates if g["gate"] == "conflict_resolution"), None)
        assert conflict_gate is not None
        assert "passed" in conflict_gate
        assert "resolution" in conflict_gate
        assert "existing_confidence" in conflict_gate
        assert "new_confidence" in conflict_gate
        assert "reason" in conflict_gate

        # Check gate ordering (conflict_resolution should be between signature and confidence)
        gate_names = [g["gate"] for g in gates]
        sig_idx = gate_names.index("owner_signature_valid")
        conflict_idx = gate_names.index("conflict_resolution")
        conf_idx = gate_names.index("confidence_threshold")

        assert sig_idx < conflict_idx < conf_idx
