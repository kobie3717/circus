"""Tests for confidence threshold in preference admission (Week 4 sub-step 4.3, Week 5 5.4 update)."""

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


def test_preference_above_threshold_admitted(reset_server_owner):
    """Test: preference with effective_confidence=0.75 (above 0.7) is admitted (W5: with valid signature)."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # W5: Generate owner keypair and insert
        private_key, _, public_bytes = _generate_test_keypair()
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

        with get_db() as conn:
            _insert_owner_key(conn, "kobus", public_key_b64)

            # W5: Create valid owner_binding
            memory_id = "shmem-high-conf"
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

            # Admit preference with confidence above threshold
            result = admit_preference(
                conn,
                memory_id=memory_id,
                owner_id="kobus",
                preference_field="user.language_preference",
                preference_value="af",
                effective_confidence=0.75,
                now=now,
                agent_id="agent-test",
                shared_at=timestamp,
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
            assert row[3] == 0.75


def test_preference_below_threshold_skipped(reset_server_owner, caplog):
    """Test: preference with effective_confidence=0.65 (below 0.7) is skipped (W5: valid signature but low confidence)."""
    import secrets
    unique_id = f"shmem-low-conf-{secrets.token_hex(8)}"

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # W5: Generate owner keypair and insert (so signature check passes, but confidence check fails)
        private_key, _, public_bytes = _generate_test_keypair()
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

        with get_db() as conn:
            _insert_owner_key(conn, "kobus", public_key_b64)

            # Manually insert preference memory with low confidence
            cursor = conn.cursor()
            now = datetime.utcnow()
            timestamp = now.isoformat()

            cursor.execute(
                """
                INSERT INTO shared_memories (
                    id, room_id, from_agent_id, content, category, domain, tags, provenance,
                    privacy_tier, hop_count, original_author, confidence,
                    age_days, effective_confidence, shared_at, trust_verified
                ) VALUES (?, 'room-memory-commons', 'agent-test', 'Test low conf pref', 'user_preference',
                          'preference.user', '[]', '{"owner_id": "kobus"}', 'team', 0, 'agent-test',
                          0.65, 0, 0.65, ?, 0)
                """,
                (unique_id, timestamp),
            )
            conn.commit()

            # W5: Create valid owner_binding (signature will pass, confidence will fail)
            signature = _sign_owner_binding(
                private_key,
                owner_id="kobus",
                agent_id="agent-test",
                memory_id=unique_id,
                timestamp=timestamp
            )

            owner_binding = {
                "agent_id": "agent-test",
                "memory_id": unique_id,
                "timestamp": timestamp,
                "signature": signature
            }

            # Try to admit (should skip due to low confidence)
            with caplog.at_level("INFO"):
                result = admit_preference(
                    conn,
                    memory_id=unique_id,
                    owner_id="kobus",
                    preference_field="user.tone_preference",
                    preference_value="formal",
                    effective_confidence=0.65,
                    now=now,
                    agent_id="agent-test",
                    shared_at=timestamp,
                    owner_binding=owner_binding,
                )

            # Should be rejected
            assert result.admitted is False

            # Verify NO row in active_preferences
            cursor.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE source_memory_id = ?",
                (unique_id,),
            )
            count = cursor.fetchone()[0]
            assert count == 0

            # Verify memory STILL exists in shared_memories (audit preserved)
            cursor.execute("SELECT id FROM shared_memories WHERE id = ?", (unique_id,))
            memory_row = cursor.fetchone()
            assert memory_row is not None

            # Verify structured log was emitted
            assert "preference_skipped" in caplog.text
            # Verify log contains reason, effective_confidence, and threshold
            log_records = [r for r in caplog.records if r.message == "preference_skipped"]
            assert len(log_records) >= 1
            log_extra = log_records[-1].__dict__
            assert log_extra.get("reason") == "confidence_below_threshold"
            assert log_extra.get("effective_confidence") == 0.65
            assert log_extra.get("threshold") == 0.7


def test_preference_at_exact_threshold_admitted(reset_server_owner):
    """Test: preference with effective_confidence=0.7 (EXACTLY at threshold) is admitted (>= semantics, W5: with valid signature)."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # W5: Generate owner keypair and insert
        private_key, _, public_bytes = _generate_test_keypair()
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

        with get_db() as conn:
            _insert_owner_key(conn, "kobus", public_key_b64)

            # W5: Create valid owner_binding
            memory_id = "shmem-exact-threshold"
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

            # Admit preference with confidence exactly at threshold
            result = admit_preference(
                conn,
                memory_id=memory_id,
                owner_id="kobus",
                preference_field="user.response_verbosity",
                preference_value="terse",
                effective_confidence=0.7,
                now=now,
                agent_id="agent-test",
                shared_at=timestamp,
                owner_binding=owner_binding,
            )

            # Should succeed (>= semantics)
            assert result.admitted is True

            # Verify row in active_preferences
            cursor = conn.cursor()
            cursor.execute(
                "SELECT owner_id, field_name, value, effective_confidence FROM active_preferences WHERE field_name = ?",
                ("user.response_verbosity",),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "kobus"
            assert row[1] == "user.response_verbosity"
            assert row[2] == "terse"
            assert row[3] == 0.7


def test_consume_side_threshold_recheck_filters_low_confidence(reset_server_owner, caplog):
    """Test: get_active_preferences filters out rows with confidence below current threshold."""
    import secrets
    unique_owner = f"owner-threshold-test-{secrets.token_hex(4)}"

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": unique_owner}):
        with get_db() as conn:
            # Manually insert a preference that was admitted at 0.75
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO active_preferences (owner_id, field_name, value, source_memory_id, effective_confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (unique_owner, "user.format_preference", "markdown", "shmem-old-threshold", 0.75, datetime.utcnow().isoformat()),
            )
            conn.commit()

            # Now monkeypatch settings to raise threshold to 0.9 (simulates runtime config change)
            from circus.config import settings
            original_threshold = settings.preference_activation_threshold

            try:
                settings.preference_activation_threshold = 0.9

                # Read preferences (should filter out the 0.75 row)
                with caplog.at_level("INFO"):
                    prefs = get_active_preferences(conn, unique_owner)

                # Should be empty (row filtered out)
                assert "user.format_preference" not in prefs

                # Verify structured log was emitted
                assert "preference_skipped" in caplog.text
                log_records = [r for r in caplog.records if r.message == "preference_skipped"]
                assert len(log_records) >= 1
                log_extra = log_records[-1].__dict__
                assert log_extra.get("reason") == "confidence_below_threshold"
                assert log_extra.get("effective_confidence") == 0.75
                assert log_extra.get("threshold") == 0.9

            finally:
                # Restore original threshold
                settings.preference_activation_threshold = original_threshold
