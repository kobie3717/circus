"""Tests for same-owner enforcement in preference admission (Week 4 sub-step 4.2, Week 5 5.4 update)."""

import base64
import os
from datetime import datetime
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from circus.database import get_db
from circus.services.preference_admission import admit_preference
from circus.services.preference_application import get_active_preferences
from circus.services.bundle_signing import canonicalize_for_signing


@pytest.fixture
def reset_server_owner():
    """Reset the cached server owner between tests and clean owner_keys table."""
    import circus.services.preference_admission as admission_module
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False
    # Clean owner_keys before test to avoid UNIQUE constraint failures
    with get_db() as conn:
        conn.execute("DELETE FROM owner_keys")
        conn.commit()
    yield
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False
    # Clean up after test too
    with get_db() as conn:
        conn.execute("DELETE FROM owner_keys")
        conn.commit()


def _generate_test_keypair():
    """Generate Ed25519 keypair for testing."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption()
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )

    return private_key, private_bytes, public_bytes


def _insert_owner_key(conn, owner_id: str, public_key_b64: str):
    """Helper to insert owner key into DB."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
        (owner_id, public_key_b64, datetime.utcnow().isoformat())
    )
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


def test_same_owner_success(reset_server_owner):
    """Test: server with CIRCUS_OWNER_ID=kobus admits preference with owner_id=kobus (W5: with valid signature)."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # W5: Generate owner keypair and insert
        private_key, _, public_bytes = _generate_test_keypair()
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

        with get_db() as conn:
            _insert_owner_key(conn, "kobus", public_key_b64)

            # W5: Create valid owner_binding
            memory_id = "shmem-test-123"
            now = datetime.utcnow()
            timestamp = now.isoformat()
            shared_at = timestamp

            signature = _sign_owner_binding(
                private_key,
                owner_id="kobus",
                agent_id="agent-test",
                memory_id=memory_id,
                timestamp=timestamp
            )

            owner_binding = {
                "agent_id": "agent-test",
                "memory_id": memory_id,
                "timestamp": timestamp,
                "signature": signature
            }

            # Admit preference for kobus
            result = admit_preference(
                conn,
                memory_id=memory_id,
                owner_id="kobus",
                preference_field="user.language_preference",
                preference_value="af",
                effective_confidence=0.85,
                now=now,
                agent_id="agent-test",
                shared_at=shared_at,
                owner_binding=owner_binding,
            )

            # Should succeed
            assert result.admitted is True

            # Verify row in active_preferences
            cursor = conn.cursor()
            cursor.execute(
                "SELECT owner_id, field_name, value, effective_confidence FROM active_preferences WHERE owner_id = ?",
                ("kobus",),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "kobus"
            assert row[1] == "user.language_preference"
            assert row[2] == "af"
            assert row[3] == 0.85


def test_same_owner_mismatch(reset_server_owner):
    """Test: server with CIRCUS_OWNER_ID=kobus rejects preference with owner_id=jaco (fails before signature check)."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        with get_db() as conn:
            # W5: owner_binding required but won't be checked (fails at same-owner gate first)
            # Pass minimal params to avoid errors
            now = datetime.utcnow()

            # Admit preference for jaco (different owner)
            result = admit_preference(
                conn,
                memory_id="shmem-test-456",
                owner_id="jaco",
                preference_field="user.tone_preference",
                preference_value="formal",
                effective_confidence=0.75,
                now=now,
                agent_id="agent-jaco",
                shared_at=now.isoformat(),
                owner_binding=None,  # Will fail before signature check anyway
            )

            # Should be rejected (at same-owner gate, before signature check)
            assert result.admitted is False

            # Verify NO row in active_preferences for jaco
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ?",
                ("jaco",),
            )
            count = cursor.fetchone()[0]
            assert count == 0


def test_get_active_preferences_owner_isolation(reset_server_owner):
    """Test: get_active_preferences returns only the requested owner's prefs (W5: with valid signature)."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # W5: Generate owner keypair and insert
        private_key, _, public_bytes = _generate_test_keypair()
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

        with get_db() as conn:
            _insert_owner_key(conn, "kobus", public_key_b64)

            # W5: Create valid owner_binding
            memory_id = "shmem-kobus-pref"
            now = datetime.utcnow()
            timestamp = now.isoformat()

            signature = _sign_owner_binding(
                private_key,
                owner_id="kobus",
                agent_id="agent-test",
                memory_id=memory_id,
                timestamp=timestamp
            )

            owner_binding = {
                "agent_id": "agent-test",
                "memory_id": memory_id,
                "timestamp": timestamp,
                "signature": signature
            }

            # Admit preference for kobus
            admit_preference(
                conn,
                memory_id=memory_id,
                owner_id="kobus",
                preference_field="user.language_preference",
                preference_value="af",
                effective_confidence=0.85,
                now=now,
                agent_id="agent-test",
                shared_at=timestamp,
                owner_binding=owner_binding,
            )
            conn.commit()

            # Read prefs for kobus
            prefs_kobus = get_active_preferences(conn, "kobus")
            assert "user.language_preference" in prefs_kobus
            assert prefs_kobus["user.language_preference"] == "af"

            # Read prefs for jaco (different owner)
            prefs_jaco = get_active_preferences(conn, "jaco")
            assert prefs_jaco == {}  # Empty dict, no preferences for jaco


def test_preference_memory_audit_trail_preserved_on_skip(reset_server_owner):
    """Test: skipped preference still exists in shared_memories (audit preserved)."""
    import secrets
    unique_id = f"shmem-audit-{secrets.token_hex(8)}"

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        with get_db() as conn:
            # Manually insert a preference memory with owner_id=jaco
            cursor = conn.cursor()
            now = datetime.utcnow()
            cursor.execute(
                """
                INSERT INTO shared_memories (
                    id, room_id, from_agent_id, content, category, domain, tags, provenance,
                    privacy_tier, hop_count, original_author, confidence,
                    age_days, effective_confidence, shared_at, trust_verified
                ) VALUES (?, 'room-memory-commons', 'agent-test', 'Test pref', 'user_preference',
                          'preference.user', '[]', '{"owner_id": "jaco"}', 'team', 1, 'agent-test',
                          0.8, 0, 0.8, ?, 0)
                """,
                (unique_id, now.isoformat()),
            )
            conn.commit()

            # Try to admit (should skip due to owner mismatch - before signature check)
            result = admit_preference(
                conn,
                memory_id=unique_id,
                owner_id="jaco",
                preference_field="user.response_verbosity",
                preference_value="terse",
                effective_confidence=0.8,
                now=now,
                agent_id="agent-test",
                shared_at=now.isoformat(),
                owner_binding=None,  # Will fail at same-owner gate first
            )

            # Should be rejected
            assert result.admitted is False

            # Verify memory STILL exists in shared_memories
            cursor.execute("SELECT id FROM shared_memories WHERE id = ?", (unique_id,))
            memory_row = cursor.fetchone()
            assert memory_row is not None
            assert memory_row[0] == unique_id

            # Verify NO row in active_preferences
            cursor.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE source_memory_id = ?",
                (unique_id,),
            )
            count = cursor.fetchone()[0]
            assert count == 0
