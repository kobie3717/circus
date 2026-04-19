"""Integration tests for preference admission with belief merge coexistence (Week 4 sub-step 4.4).

This test module proves that:
1. Preference memories flow through apply_belief_merge_pipeline() without errors
2. admit_preference() is called exactly once per publish (no double side-effects)
3. Conflict detection does not corrupt active_preferences
4. Upsert semantics work correctly under merge coexistence (idempotent updates)

These tests are the proof that belief merge and preference admission coexist cleanly
on the same publish path without interference.
"""

import os
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import init_database, get_db
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
def reset_server_owner():
    """Reset cached server owner between tests."""
    import circus.services.preference_admission as admission_module
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False
    yield
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False


@pytest.fixture
def client(temp_db, reset_server_owner):
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


def _make_owner_binding_for_memory_id(memory_id: str) -> dict:
    """Helper to create a valid (but garbage-signed) owner_binding for testing.

    W5: Publish-side requires owner_binding structure for preference memories.
    Signature can be garbage since admission-side verification is separate.
    """
    return {
        "agent_id": "agent-test-123",
        "memory_id": memory_id,
        "timestamp": "2026-04-19T10:00:00Z",
        "signature": "dGVzdC1zaWduYXR1cmUtYmFzZTY0"  # garbage base64, admission will skip
    }


def test_preference_flows_through_merge_pipeline(client):
    """Test: preference memory flows through belief merge pipeline without exceptions.

    Proves that:
    - apply_belief_merge_pipeline() does not crash on preference memories
    - Preference lands in shared_memories
    - active_preferences is populated
    - Response includes preference_activated: true

    This is the basic coexistence sanity check.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner AFTER patching env (so _get_server_owner() reads patched value)
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # W5: Mock memory_id generation to enable owner_binding.memory_id match
        with patch('secrets.token_hex', return_value='flowtest12345678'):
            expected_memory_id = "shmem-flowtest12345678"

            payload = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "User prefers Afrikaans for bot responses",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "reasoning": "User explicitly requested Afrikaans",
                    "owner_binding": _make_owner_binding_for_memory_id(expected_memory_id)
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "af"
                }
            }

            # Publish preference (goes through merge pipeline + admission)
            response = client.post("/api/v1/memory-commons/publish", json=payload)
            assert response.status_code == 200, f"Publish failed: {response.json()}"

            data = response.json()
            assert "memory_id" in data
            assert data["memory_id"] == expected_memory_id

            # Verify preference_activated is True in response
            assert data.get("preference_activated") is True, "preference_activated should be True"

        # Verify memory landed in shared_memories
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, category, domain FROM shared_memories WHERE id = ?",
                (data["memory_id"],)
            )
            memory_row = cursor.fetchone()
            assert memory_row is not None
            assert memory_row[1] == "user_preference"
            assert memory_row[2] == "preference.user"

            # Verify row in active_preferences
            cursor.execute(
                "SELECT owner_id, field_name, value FROM active_preferences WHERE owner_id = ?",
                ("kobus",)
            )
            pref_row = cursor.fetchone()
            assert pref_row is not None
            assert pref_row[0] == "kobus"
            assert pref_row[1] == "user.language_preference"
            assert pref_row[2] == "af"


def test_admission_fires_exactly_once_per_publish(client):
    """Test: admit_preference is called exactly once per publish (no double side-effects).

    This is THE critical test for 4.4 coexistence. It proves that even though
    both belief merge pipeline and admission gate run on the same publish path,
    admission does not get called multiple times.

    Uses a mock wrapper to count actual invocations of admit_preference().
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner AFTER patching env
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        from circus.services import preference_admission

        original_admit = preference_admission.admit_preference
        call_count = 0

        def counting_wrapper(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original_admit(*args, **kwargs)

        with patch.object(preference_admission, "admit_preference", side_effect=counting_wrapper):
            # W5: Mock memory_id generation to enable owner_binding.memory_id match
            with patch('secrets.token_hex', return_value='oncetest12345678'):
                expected_memory_id = "shmem-oncetest12345678"

                payload = {
                    "category": "user_preference",
                    "domain": "preference.user",
                    "content": "User prefers terse responses",
                    "confidence": 0.8,
                    "provenance": {
                        "owner_id": "kobus",
                        "owner_binding": _make_owner_binding_for_memory_id(expected_memory_id)
                    },
                    "preference": {
                        "field": "user.response_verbosity",
                        "value": "terse"
                    }
                }

                # Publish preference
                response = client.post("/api/v1/memory-commons/publish", json=payload)
                assert response.status_code == 200

                # THE assertion: admit_preference called EXACTLY once
                assert call_count == 1, f"Expected admit_preference to be called exactly once, got {call_count}"

                # Verify active_preferences has exactly 1 row for this owner+field
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                        ("kobus", "user.response_verbosity")
                    )
                    row_count = cursor.fetchone()[0]
                    assert row_count == 1, f"Expected exactly 1 row in active_preferences, got {row_count}"


def test_conflict_detection_does_not_corrupt_active_preferences(client):
    """Test: conflict detection on preference publish does not corrupt active_preferences.

    Scenario:
    - Pre-seed shared_memories with an existing preference memory (same field, different value)
    - Publish a new conflicting preference memory
    - Verify: active_preferences has correct final row (latest-wins semantics)
    - Verify: active_preferences does NOT have duplicates or stale data
    - Verify: active_preferences has exactly 1 row per (owner_id, field_name)

    This proves conflict detection doesn't cause double-writes or corrupt the preference table.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner AFTER patching env
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # Pre-seed an existing preference memory
        with get_db() as conn:
            cursor = conn.cursor()
            memory_id_old = "shmem-old-pref"
            now = datetime.utcnow().isoformat()

            cursor.execute(
                """
                INSERT INTO shared_memories (
                    id, room_id, from_agent_id, content, category, domain, tags, provenance,
                    privacy_tier, hop_count, original_author, confidence,
                    age_days, effective_confidence, shared_at, trust_verified
                ) VALUES (?, 'room-memory-commons', 'agent-test', 'User wants formal tone', 'user_preference',
                          'preference.user', '[]', '{"owner_id": "kobus"}', 'team', 1, 'agent-test',
                          0.75, 0, 0.75, ?, 0)
                """,
                (memory_id_old, now)
            )

            # Manually insert into active_preferences (simulating previous admission)
            cursor.execute(
                """
                INSERT INTO active_preferences (owner_id, field_name, value, source_memory_id, effective_confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("kobus", "user.tone_preference", "formal", memory_id_old, 0.75, now)
            )
            conn.commit()

        # Publish new conflicting preference (same field, different value, higher confidence)
        # W5: Mock memory_id generation to enable owner_binding.memory_id match
        with patch('secrets.token_hex', return_value='conflicttest1234'):
            expected_memory_id = "shmem-conflicttest1234"

            payload = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "User prefers casual tone now",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": _make_owner_binding_for_memory_id(expected_memory_id)
                },
                "preference": {
                    "field": "user.tone_preference",
                    "value": "casual"
                }
            }

            response = client.post("/api/v1/memory-commons/publish", json=payload)
            assert response.status_code == 200
            data = response.json()

        # Verify active_preferences has correct final row
        with get_db() as conn:
            cursor = conn.cursor()

            # Exactly 1 row for (kobus, user.tone_preference)
            cursor.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.tone_preference")
            )
            row_count = cursor.fetchone()[0]
            assert row_count == 1, f"Expected exactly 1 row, got {row_count} (duplicate or missing)"

            # Row reflects the latest publish (value="casual", higher confidence)
            cursor.execute(
                "SELECT value, effective_confidence, source_memory_id FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.tone_preference")
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "casual", "active_preferences should have latest value"
            assert row[1] >= 0.85, "effective_confidence should reflect new publish"
            assert row[2] == data["memory_id"], "source_memory_id should point to new memory"

            # Both memories exist in shared_memories (audit trail preserved)
            cursor.execute(
                "SELECT COUNT(*) FROM shared_memories WHERE category = 'user_preference' AND domain = 'preference.user'"
            )
            mem_count = cursor.fetchone()[0]
            assert mem_count >= 2, "Both old and new memories should exist in shared_memories"


def test_preference_with_update_semantics(client):
    """Test: idempotent upsert under merge coexistence (v1 → v2 update).

    Scenario:
    - Publish preference v1 (confidence 0.8, value "af")
    - Publish preference v2 (confidence 0.85, value "en", same field)
    - Verify: active_preferences has exactly 1 row for (owner_id, field_name)
    - Verify: row reflects v2 (value="en", confidence reflects 0.85)
    - Verify: shared_memories has BOTH v1 and v2 (audit trail)

    This proves upsert semantics work correctly even when belief merge runs.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner AFTER patching env
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # Publish v1
        # W5: Mock memory_id generation to enable owner_binding.memory_id match
        with patch('secrets.token_hex', return_value='updatev1test1234'):
            memory_id_v1 = "shmem-updatev1test1234"

            payload_v1 = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "User prefers Afrikaans",
                "confidence": 0.8,
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": _make_owner_binding_for_memory_id(memory_id_v1)
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "af"
                }
            }

            response_v1 = client.post("/api/v1/memory-commons/publish", json=payload_v1)
            assert response_v1.status_code == 200
            assert response_v1.json()["memory_id"] == memory_id_v1

        # Publish v2 (update)
        with patch('secrets.token_hex', return_value='updatev2test5678'):
            memory_id_v2 = "shmem-updatev2test5678"

            payload_v2 = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "User changed preference to English",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": _make_owner_binding_for_memory_id(memory_id_v2)
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "en"
                }
            }

            response_v2 = client.post("/api/v1/memory-commons/publish", json=payload_v2)
            assert response_v2.status_code == 200
            assert response_v2.json()["memory_id"] == memory_id_v2

        # Verify active_preferences has exactly 1 row
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.language_preference")
            )
            row_count = cursor.fetchone()[0]
            assert row_count == 1, f"Expected exactly 1 row (upsert), got {row_count}"

            # Row reflects v2
            cursor.execute(
                "SELECT value, effective_confidence, source_memory_id FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.language_preference")
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "en", "active_preferences should reflect v2 value"
            assert row[1] >= 0.85, "effective_confidence should reflect v2"
            assert row[2] == memory_id_v2, "source_memory_id should point to v2"

            # shared_memories has BOTH v1 and v2
            cursor.execute(
                "SELECT id FROM shared_memories WHERE id IN (?, ?)",
                (memory_id_v1, memory_id_v2)
            )
            mem_rows = cursor.fetchall()
            assert len(mem_rows) == 2, "Both v1 and v2 should exist in shared_memories (audit trail)"
