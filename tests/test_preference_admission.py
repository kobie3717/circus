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


# W5: Global test owner keypair (persistent across tests in this module for performance)
_TEST_OWNER_PRIVATE_KEY = None
_TEST_OWNER_PUBLIC_KEY_B64 = None


def _ensure_test_owner_key():
    """Generate or return cached test owner keypair."""
    global _TEST_OWNER_PRIVATE_KEY, _TEST_OWNER_PUBLIC_KEY_B64

    if _TEST_OWNER_PRIVATE_KEY is None:
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        public_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )

        _TEST_OWNER_PRIVATE_KEY = private_key
        _TEST_OWNER_PUBLIC_KEY_B64 = base64.b64encode(public_bytes).decode('ascii')

    return _TEST_OWNER_PRIVATE_KEY, _TEST_OWNER_PUBLIC_KEY_B64


@pytest.fixture
def client(temp_db, reset_server_owner):
    """Test client with fresh database and test owner key registered."""
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
    agent_id = response.json()["agent_id"]

    # Store token and agent_id for tests
    client.headers = {"Authorization": f"Bearer {token}"}
    client.agent_id = agent_id  # Store for use in tests that seed DB directly

    # W5: Register test owner key for kobus (for signature verification)
    _, public_key_b64 = _ensure_test_owner_key()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
            ("kobus", public_key_b64, datetime.utcnow().isoformat())
        )
        conn.commit()

    return client


def _make_owner_binding_for_memory_id(memory_id: str) -> dict:
    """Helper to create a VALID owner_binding with real signature (W5 update).

    W5 5.4: admission-side now verifies signatures, so tests need valid signatures.
    Uses cached test owner keypair for performance.
    """
    from circus.services.bundle_signing import canonicalize_for_signing
    import base64

    private_key, _ = _ensure_test_owner_key()
    timestamp = datetime.utcnow().isoformat()

    payload = {
        "agent_id": "agent-test-123",
        "memory_id": memory_id,
        "owner_id": "kobus",
        "timestamp": timestamp,
    }
    canonical_bytes = canonicalize_for_signing(payload)
    signature = private_key.sign(canonical_bytes)
    signature_b64 = base64.b64encode(signature).decode('ascii')

    return {
        "agent_id": "agent-test-123",
        "memory_id": memory_id,
        "timestamp": timestamp,
        "signature": signature_b64
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
                ) VALUES (?, 'room-memory-commons', ?, 'User wants formal tone', 'user_preference',
                          'preference.user', '[]', '{"owner_id": "kobus"}', 'team', 1, ?,
                          0.75, 0, 0.75, ?, 0)
                """,
                (memory_id_old, client.agent_id, client.agent_id, now)
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


# ===== Week 5 (5.4): Admission-side owner signature verification tests =====


def _generate_test_keypair():
    """Generate Ed25519 keypair for testing (helper from test_owner_verification.py)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

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
    import base64
    from circus.services.bundle_signing import canonicalize_for_signing

    payload = {
        "agent_id": agent_id,
        "memory_id": memory_id,
        "owner_id": owner_id,
        "timestamp": timestamp,
    }
    canonical_bytes = canonicalize_for_signing(payload)
    signature = private_key.sign(canonical_bytes)
    return base64.b64encode(signature).decode('ascii')


def test_bad_signature_leaves_shared_memories_but_not_active_preferences(client, caplog):
    """KOBUS'S CORE NEGATIVE-PATH TEST (W5 ship gate).

    Given:
    - Preference with correct same-owner string (owner_id=kobus matches server)
    - Valid shape (all owner_binding fields present)
    - BAD signature (signed with wrong key)

    Expected:
    - HTTP publish returns 200 (shape validation passes at publish)
    - Memory IS present in shared_memories (fresh-connection query)
    - active_preferences table does NOT contain this (owner_id, field) row
    - caplog captured ONE INFO record with reason="owner_signature_invalid"

    This is the core W5 negative-path proof.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # NOTE: client fixture already registered kobus owner key
        # Generate WRONG keypair for signing (attacker scenario)
        private_key_wrong, _, _ = _generate_test_keypair()

        # Mock memory_id generation
        with patch('secrets.token_hex', return_value='badsigtest123456'):
            expected_memory_id = "shmem-badsigtest123456"
            timestamp = datetime.utcnow().isoformat()

            # Sign with WRONG key (malicious agent scenario)
            bad_signature = _sign_owner_binding(
                private_key_wrong,  # WRONG key
                owner_id="kobus",
                agent_id="agent-attacker",
                memory_id=expected_memory_id,
                timestamp=timestamp
            )

            payload = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Attacker tries to inject preference",
                "confidence": 0.9,
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": "agent-attacker",
                        "memory_id": expected_memory_id,
                        "timestamp": timestamp,
                        "signature": bad_signature  # BAD signature
                    }
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "en"
                }
            }

            # Publish should succeed (shape validation passes)
            with caplog.at_level("INFO"):
                response = client.post("/api/v1/memory-commons/publish", json=payload)
                assert response.status_code == 200, f"Publish should succeed with 200, got {response.status_code}"

        # CRITICAL ASSERTIONS: Memory is in shared_memories, NOT in active_preferences
        with get_db() as conn:
            cursor = conn.cursor()

            # Memory IS present in shared_memories
            cursor.execute("SELECT id FROM shared_memories WHERE id = ?", (expected_memory_id,))
            mem_row = cursor.fetchone()
            assert mem_row is not None, "Memory should be in shared_memories (published successfully)"

            # active_preferences does NOT contain this preference
            cursor.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.language_preference")
            )
            pref_count = cursor.fetchone()[0]
            assert pref_count == 0, "active_preferences should NOT contain preference with bad signature"

        # Log should contain ONE INFO record with reason="owner_signature_invalid"
        skip_logs = [rec for rec in caplog.records if rec.levelname == "INFO" and "preference_skipped" in rec.getMessage()]
        assert len(skip_logs) >= 1, "Should have at least one skip log"

        # Find the skip log with owner_signature_invalid
        invalid_sig_logs = [rec for rec in skip_logs if hasattr(rec, 'reason') and rec.reason == "owner_signature_invalid"]
        assert len(invalid_sig_logs) == 1, f"Should have exactly one owner_signature_invalid log, got {len(invalid_sig_logs)}"
        assert invalid_sig_logs[0].memory_id == expected_memory_id


def test_missing_owner_binding_at_admission_skips_with_owner_signature_missing(client, caplog):
    """Test: missing owner_binding at admission skips with owner_signature_missing.

    Scenario: Memory somehow gets to admission without owner_binding (defense in depth).
    This shouldn't happen after 5.3 publish validation, but federation/legacy might bypass.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # NOTE: client fixture already registered kobus owner key

        with get_db() as conn:
            # Manually insert memory into shared_memories WITHOUT owner_binding
            memory_id = "shmem-no-binding"
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()

            cursor.execute(
                """
                INSERT INTO shared_memories (
                    id, room_id, from_agent_id, content, category, domain, tags, provenance,
                    privacy_tier, hop_count, original_author, confidence,
                    age_days, effective_confidence, shared_at, trust_verified
                ) VALUES (?, 'room-memory-commons', ?, 'Test content', 'user_preference',
                          'preference.user', '[]', '{"owner_id": "kobus"}', 'team', 1, ?,
                          0.85, 0, 0.85, ?, 0)
                """,
                (memory_id, client.agent_id, client.agent_id, now)
            )
            conn.commit()

        # Try to admit preference directly (bypassing publish)
        from circus.services.preference_admission import admit_preference

        with get_db() as conn:
            with caplog.at_level("INFO"):
                result = admit_preference(
                    conn,
                    memory_id=memory_id,
                    owner_id="kobus",
                    preference_field="user.format_preference",
                    preference_value="markdown",
                    effective_confidence=0.85,
                    now=datetime.utcnow(),
                    agent_id="agent-test",
                    shared_at=now,
                    owner_binding=None  # Missing binding
                )

                assert result.admitted is False, "Should skip with missing owner_binding"

        # Check skip log reason
        skip_logs = [rec for rec in caplog.records if rec.levelname == "INFO" and hasattr(rec, 'reason')]
        missing_logs = [rec for rec in skip_logs if rec.reason == "owner_signature_missing"]
        assert len(missing_logs) == 1, f"Should have one owner_signature_missing log, got {len(missing_logs)}"


def test_unknown_owner_skips_with_owner_key_unknown(client, caplog):
    """Test: unknown owner (not in owner_keys) skips with owner_key_unknown."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # Delete the owner key that client fixture inserted (to simulate unknown owner)
        with get_db() as conn:
            conn.execute("DELETE FROM owner_keys WHERE owner_id = ?", ("kobus",))
            conn.commit()

        # Generate a keypair and sign (but owner not registered)
        import base64
        private_key, _, _ = _generate_test_keypair()

        with patch('secrets.token_hex', return_value='unknownowner1234'):
            expected_memory_id = "shmem-unknownowner1234"
            timestamp = datetime.utcnow().isoformat()

            # Sign with valid key, but owner is unknown
            signature = _sign_owner_binding(
                private_key,
                owner_id="kobus",
                agent_id="agent-test",
                memory_id=expected_memory_id,
                timestamp=timestamp
            )

            payload = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Unknown owner test",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": "agent-test",
                        "memory_id": expected_memory_id,
                        "timestamp": timestamp,
                        "signature": signature
                    }
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "en"
                }
            }

            with caplog.at_level("INFO"):
                response = client.post("/api/v1/memory-commons/publish", json=payload)
                assert response.status_code == 200

        # Check skip log
        skip_logs = [rec for rec in caplog.records if rec.levelname == "INFO" and hasattr(rec, 'reason')]
        unknown_logs = [rec for rec in skip_logs if rec.reason == "owner_key_unknown"]
        assert len(unknown_logs) == 1, f"Should have one owner_key_unknown log, got {len(unknown_logs)}"

        # Verify not in active_preferences
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.language_preference")
            )
            assert cursor.fetchone()[0] == 0


def test_expired_timestamp_skips_with_owner_binding_expired(client, caplog):
    """Test: binding timestamp too old (>5min before shared_at) skips with owner_binding_expired."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # NOTE: client fixture already registered kobus owner key
        # Use the same test owner key for signing
        import base64
        private_key, _ = _ensure_test_owner_key()

        # Create timestamp 10 minutes in the past (expired)
        from datetime import timedelta
        now = datetime.utcnow()
        old_timestamp = (now - timedelta(minutes=10)).isoformat()
        shared_at = now.isoformat()

        with patch('secrets.token_hex', return_value='expiredtest12345'):
            expected_memory_id = "shmem-expiredtest12345"

            signature = _sign_owner_binding(
                private_key,
                owner_id="kobus",
                agent_id="agent-test",
                memory_id=expected_memory_id,
                timestamp=old_timestamp  # Expired timestamp
            )

            payload = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Expired timestamp test",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": "agent-test",
                        "memory_id": expected_memory_id,
                        "timestamp": old_timestamp,
                        "signature": signature
                    }
                },
                "preference": {
                    "field": "user.response_verbosity",
                    "value": "terse"
                }
            }

            with caplog.at_level("INFO"):
                response = client.post("/api/v1/memory-commons/publish", json=payload)
                assert response.status_code == 200

        # Check skip log
        skip_logs = [rec for rec in caplog.records if rec.levelname == "INFO" and hasattr(rec, 'reason')]
        expired_logs = [rec for rec in skip_logs if rec.reason == "owner_binding_expired"]
        assert len(expired_logs) == 1, f"Should have one owner_binding_expired log, got {len(expired_logs)}"


def test_future_timestamp_skips_with_owner_binding_future_timestamp(client, caplog):
    """Test: binding timestamp too far ahead (>5min after shared_at) skips with owner_binding_future_timestamp."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # NOTE: client fixture already registered kobus owner key
        # Use the same test owner key for signing
        import base64
        private_key, _ = _ensure_test_owner_key()

        # Create timestamp 10 minutes in the future
        from datetime import timedelta
        now = datetime.utcnow()
        future_timestamp = (now + timedelta(minutes=10)).isoformat()

        with patch('secrets.token_hex', return_value='futuretest123456'):
            expected_memory_id = "shmem-futuretest123456"

            signature = _sign_owner_binding(
                private_key,
                owner_id="kobus",
                agent_id="agent-test",
                memory_id=expected_memory_id,
                timestamp=future_timestamp  # Future timestamp
            )

            payload = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Future timestamp test",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": "agent-test",
                        "memory_id": expected_memory_id,
                        "timestamp": future_timestamp,
                        "signature": signature
                    }
                },
                "preference": {
                    "field": "user.tone_preference",
                    "value": "formal"
                }
            }

            with caplog.at_level("INFO"):
                response = client.post("/api/v1/memory-commons/publish", json=payload)
                assert response.status_code == 200

        # Check skip log
        skip_logs = [rec for rec in caplog.records if rec.levelname == "INFO" and hasattr(rec, 'reason')]
        future_logs = [rec for rec in skip_logs if rec.reason == "owner_binding_future_timestamp"]
        assert len(future_logs) == 1, f"Should have one owner_binding_future_timestamp log, got {len(future_logs)}"


def test_valid_signature_activates_preference(client):
    """Test: valid owner signature → preference lands in active_preferences (happy path)."""
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # NOTE: client fixture already registered kobus owner key
        # Use the same test owner key for signing
        import base64
        private_key, _ = _ensure_test_owner_key()

        # Create valid signature
        now = datetime.utcnow()
        timestamp = now.isoformat()

        with patch('secrets.token_hex', return_value='validtest1234567'):
            expected_memory_id = "shmem-validtest1234567"

            signature = _sign_owner_binding(
                private_key,
                owner_id="kobus",
                agent_id="agent-test",
                memory_id=expected_memory_id,
                timestamp=timestamp
            )

            payload = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Valid signature test",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "owner_binding": {
                        "agent_id": "agent-test",
                        "memory_id": expected_memory_id,
                        "timestamp": timestamp,
                        "signature": signature
                    }
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "af"
                }
            }

            response = client.post("/api/v1/memory-commons/publish", json=payload)
            assert response.status_code == 200
            data = response.json()
            assert data.get("preference_activated") is True

        # Verify preference in active_preferences
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT field_name, value FROM active_preferences WHERE owner_id = ?",
                ("kobus",)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "user.language_preference"
            assert row[1] == "af"
