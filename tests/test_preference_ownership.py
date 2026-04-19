"""Tests for same-owner enforcement in preference admission (Week 4 sub-step 4.2)."""

import os
from datetime import datetime
from unittest.mock import patch

import pytest

from circus.database import get_db
from circus.services.preference_admission import admit_preference
from circus.services.preference_application import get_active_preferences


@pytest.fixture
def reset_server_owner():
    """Reset the cached server owner between tests."""
    import circus.services.preference_admission as admission_module
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False
    yield
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False


def test_same_owner_success(reset_server_owner):
    """Test: server with CIRCUS_OWNER_ID=kobus admits preference with owner_id=kobus."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        with get_db() as conn:
            # Admit preference for kobus
            result = admit_preference(
                conn,
                memory_id="shmem-test-123",
                owner_id="kobus",
                preference_field="user.language_preference",
                preference_value="af",
                effective_confidence=0.85,
                now=datetime.utcnow(),
            )

            # Should succeed
            assert result is True

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
    """Test: server with CIRCUS_OWNER_ID=kobus rejects preference with owner_id=jaco."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        with get_db() as conn:
            # Admit preference for jaco (different owner)
            result = admit_preference(
                conn,
                memory_id="shmem-test-456",
                owner_id="jaco",
                preference_field="user.tone_preference",
                preference_value="formal",
                effective_confidence=0.75,
                now=datetime.utcnow(),
            )

            # Should be rejected
            assert result is False

            # Verify NO row in active_preferences for jaco
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ?",
                ("jaco",),
            )
            count = cursor.fetchone()[0]
            assert count == 0


def test_get_active_preferences_owner_isolation(reset_server_owner):
    """Test: get_active_preferences returns only the requested owner's prefs."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        with get_db() as conn:
            # Admit preference for kobus
            admit_preference(
                conn,
                memory_id="shmem-kobus-pref",
                owner_id="kobus",
                preference_field="user.language_preference",
                preference_value="af",
                effective_confidence=0.85,
                now=datetime.utcnow(),
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
                (unique_id, datetime.utcnow().isoformat()),
            )
            conn.commit()

            # Try to admit (should skip due to owner mismatch)
            result = admit_preference(
                conn,
                memory_id=unique_id,
                owner_id="jaco",
                preference_field="user.response_verbosity",
                preference_value="terse",
                effective_confidence=0.8,
                now=datetime.utcnow(),
            )

            # Should be rejected
            assert result is False

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
