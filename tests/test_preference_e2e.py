"""End-to-End Preference Memory Tests — The Week 4 Ship Gate.

This test suite proves the entire Week 4 stack works by demonstrating actual behavior changes:

**The Product Moment:**
One agent learns a user preference → federates the truth → every agent serving that
owner immediately behaves differently.

**What This Tests:**
- Full publish → admit → consume → behavior-delta flow
- Owner isolation: different owners don't see each other's preferences
- Latest-wins control plane: preferences are live, not static

**What This Does NOT Test:**
- Real cross-process orchestration (4.7 polish step, optional)
- Actual language translation (bot outputs are mocked/canned)
- Federation network simulation (4.5 already proved federation works)

If these three tests pass, Week 4 is DONE.

Sub-step: 4.6 (ship gate)
Branch: feat/memory-commons-w4
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from circus.app import app
from circus.database import init_database, get_db
from circus.config import settings
from circus.services.preference_application import get_active_preferences
from circus.services.bundle_signing import canonicalize_for_signing


# ── Bot Harness: Mock Bot That Consumes Preferences ──

def mock_bot_response(conn, owner_id: str, user_message: str) -> dict:
    """Mock bot that consults active_preferences and produces observable output.

    This simulates what Claw, Friday, or any Circus-aware bot does on every turn:
    read the user's active preferences and adjust response construction.

    In production, this would be the bot's message handler consulting preferences
    before generating a response. Here we mock the output so tests can assert on
    observable behavior changes without running a real LLM or translator.

    Args:
        conn: Database connection
        owner_id: Owner identifier (e.g., "kobus")
        user_message: User's message (unused in mock, but present for realism)

    Returns:
        Dict with observable behavior knobs:
        - text: The response text (changes based on preferences)
        - language: Language code from preferences
        - verbosity: Verbosity level from preferences
        - tone: Tone setting from preferences
        - format: Format preference from preferences
    """
    prefs = get_active_preferences(conn, owner_id)

    language = prefs.get("user.language_preference", "en")
    verbosity = prefs.get("user.response_verbosity", "normal")
    tone = prefs.get("user.tone_preference", "neutral")
    format_pref = prefs.get("user.format_preference", "plain")

    # Canned output based on settings — not actually translating
    # These outputs are chosen to be VISUALLY DIFFERENT so assertions are readable
    if language == "af" and verbosity == "terse":
        text = "Ja, reg so."
    elif language == "af":
        text = "Ja, dit is reg — hier is die antwoord."
    elif verbosity == "terse":
        text = "Yes, done."
    else:
        text = "Yes, here's the answer you requested."

    return {
        "text": text,
        "language": language,
        "verbosity": verbosity,
        "tone": tone,
        "format": format_pref,
    }


# ── Test Fixtures ──

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
def kobus_private_key(temp_db):
    """Generate Ed25519 keypair and seed owner_keys table for 'kobus' in temp DB."""
    from datetime import datetime
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


@pytest.fixture
def client(temp_db, reset_server_owner, kobus_private_key):
    """Test client with fresh database and registered agent."""
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


def _make_owner_binding_for_memory_id(memory_id: str, private_key: Ed25519PrivateKey) -> dict:
    """Create a valid signed owner_binding for testing."""
    from datetime import datetime
    timestamp = datetime.utcnow().isoformat()
    payload = {
        "agent_id": "agent-test-123",
        "memory_id": memory_id,
        "owner_id": "kobus",
        "timestamp": timestamp,
    }
    canonical_bytes = canonicalize_for_signing(payload)
    signature = private_key.sign(canonical_bytes)
    return {
        "agent_id": "agent-test-123",
        "memory_id": memory_id,
        "timestamp": timestamp,
        "signature": base64.b64encode(signature).decode('ascii')
    }


# ── Test 1: The Happy Path — THE Core Product Moment ──

def test_friday_publishes_preference_then_claw_behavior_changes(client, kobus_private_key):
    """The Week 4 product moment.

    Friday learns a preference for Kobus. Circus federates the truth.
    Claw, serving the same owner, immediately behaves differently on its next turn.

    This test proves:
    - Publish → admit → consume pipeline works end-to-end
    - Behavior actually changes (not just DB state)
    - Fresh connections read admitted preferences correctly
    """
    # ── SETUP: Both bots serve Kobus ──
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner AFTER patching env
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # ── BEFORE: Claw responds in default English, normal verbosity ──
        with get_db() as conn:
            before = mock_bot_response(conn, owner_id="kobus", user_message="Help asseblief")

        assert before["language"] == "en", "Before: should default to English"
        assert before["verbosity"] == "normal", "Before: should default to normal verbosity"
        assert before["text"] == "Yes, here's the answer you requested.", "Before: default English response"

        # ── FRIDAY LEARNS: Kobus prefers Afrikaans, terse responses ──
        # Friday publishes two preference memories through the real publish path.
        # In production this would fire after Friday's pattern-learning loop triggers.
        # W5: Mock memory_id generation to enable owner_binding.memory_id match

        # Publish first preference: language
        with patch('secrets.token_hex', return_value='e2elang123456789'):
            memory_id_1 = "shmem-e2elang123456789"
            pref_1 = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Kobus prefers Afrikaans for bot responses",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "reasoning": "User explicitly requested Afrikaans in multiple sessions",
                    "owner_binding": _make_owner_binding_for_memory_id(memory_id_1, kobus_private_key)
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "af"
                },
            }
            resp = client.post("/api/v1/memory-commons/publish", json=pref_1)
            assert resp.status_code == 200, f"Publish failed: {resp.json()}"
            assert resp.json()["preference_activated"] is True, "Preference should be activated"

        # Publish second preference: verbosity
        with patch('secrets.token_hex', return_value='e2everb987654321'):
            memory_id_2 = "shmem-e2everb987654321"
            pref_2 = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Kobus prefers terse responses",
                "confidence": 0.9,
                "provenance": {
                    "owner_id": "kobus",
                    "reasoning": "User frequently says 'just tell me' and 'short answer please'",
                    "owner_binding": _make_owner_binding_for_memory_id(memory_id_2, kobus_private_key)
                },
                "preference": {
                    "field": "user.response_verbosity",
                    "value": "terse"
                },
            }
            resp = client.post("/api/v1/memory-commons/publish", json=pref_2)
            assert resp.status_code == 200, f"Publish failed: {resp.json()}"
            assert resp.json()["preference_activated"] is True, "Preference should be activated"

        # ── AFTER: Claw's next turn now speaks Afrikaans, terse ──
        # Critical: use a fresh connection — Claw would be a separate process in production.
        # This proves the preference was actually admitted to active_preferences, not just
        # held in transaction state.
        with get_db() as fresh_conn:
            after = mock_bot_response(fresh_conn, owner_id="kobus", user_message="Help asseblief")

        assert after["language"] == "af", "After: should use Afrikaans from preference"
        assert after["verbosity"] == "terse", "After: should use terse from preference"
        assert after["text"] == "Ja, reg so.", "After: Afrikaans terse response"

        # ── THE BEHAVIORAL DELTA: This is what Week 4 is about ──
        assert before["text"] != after["text"], "Behavior MUST change: text differs"
        assert before["language"] != after["language"], "Behavior MUST change: language differs"
        assert before["verbosity"] != after["verbosity"], "Behavior MUST change: verbosity differs"


# ── Test 2: The Negative Path — Different Owner Sees Nothing ──

def test_007_serves_different_owner_ignores_kobus_preferences(client, kobus_private_key):
    """Owner isolation test: Jaco's bot ignores Kobus's preferences.

    Proves:
    - Preferences are scoped to the owner they were published for
    - shared_memories has the preference (audit trail)
    - get_active_preferences(different_owner) returns empty
    - Bot behavior remains default when serving different owner

    This is the safety gate: preferences don't leak across owner boundaries.
    """
    # ── SETUP: Kobus's Circus admits his preference ──
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # Kobus's preference: Afrikaans
        # W5: Mock memory_id generation to enable owner_binding.memory_id match
        with patch('secrets.token_hex', return_value='owner007test12345'):
            memory_id = "shmem-owner007test12345"
            kobus_pref = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Kobus prefers Afrikaans",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "reasoning": "User requested Afrikaans",
                    "owner_binding": _make_owner_binding_for_memory_id(memory_id, kobus_private_key)
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "af"
                },
            }

            resp = client.post("/api/v1/memory-commons/publish", json=kobus_pref)
            assert resp.status_code == 200, f"Publish failed: {resp.json()}"
            assert resp.json()["preference_activated"] is True

        # ── VERIFY: Audit trail exists in shared_memories ──
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM shared_memories WHERE category = 'user_preference'"
            )
            count = cursor.fetchone()[0]
            assert count > 0, "Preference should be in shared_memories for audit"

        # ── JACO'S BOT: Queries for Jaco's owner_id (different owner) ──
        # Note: We don't change CIRCUS_OWNER_ID env here — that controls admission.
        # The read path takes an explicit owner_id param, so we can query any owner.
        with get_db() as fresh_conn:
            jaco_prefs = get_active_preferences(fresh_conn, "jaco")
            assert jaco_prefs == {}, "Jaco should have no active preferences (different owner)"

            # Jaco's bot produces default behavior
            jaco_response = mock_bot_response(fresh_conn, owner_id="jaco", user_message="Help please")

        assert jaco_response["language"] == "en", "Jaco's bot should default to English"
        assert jaco_response["verbosity"] == "normal", "Jaco's bot should default to normal"
        assert jaco_response["text"] == "Yes, here's the answer you requested.", "Default response"

        # ── KOBUS'S BOT: Still sees his preference ──
        with get_db() as fresh_conn:
            kobus_prefs = get_active_preferences(fresh_conn, "kobus")
            assert kobus_prefs == {"user.language_preference": "af"}, "Kobus should still have his preference"

            kobus_response = mock_bot_response(fresh_conn, owner_id="kobus", user_message="Help asseblief")

        assert kobus_response["language"] == "af", "Kobus's bot should use Afrikaans"
        assert kobus_response["text"] != jaco_response["text"], "Kobus and Jaco get different responses"


# ── Test 3: Latest-Wins Control Plane — Live Updates ──

def test_second_preference_publish_updates_behavior_again(client, kobus_private_key):
    """Latest-wins test: control plane is live, not static.

    Proves:
    - Second publish for same field updates behavior
    - active_preferences has exactly ONE row per (owner, field) — upsert works
    - shared_memories preserves BOTH versions — audit trail intact
    - Behavior changes again when preference updates

    This proves the control plane responds to real-time changes.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None
        admission_module._WARN_LOGGED = False

        # ── PUBLISH V1: Afrikaans (confidence 0.85) ──
        # W5: Mock memory_id generation to enable owner_binding.memory_id match
        with patch('secrets.token_hex', return_value='secondv1test12345'):
            v1_memory_id = "shmem-secondv1test12345"
            pref_v1 = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Kobus prefers Afrikaans for bot responses",
                "confidence": 0.85,
                "provenance": {
                    "owner_id": "kobus",
                    "reasoning": "User requested Afrikaans",
                    "owner_binding": _make_owner_binding_for_memory_id(v1_memory_id, kobus_private_key)
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "af"
                },
            }

            resp = client.post("/api/v1/memory-commons/publish", json=pref_v1)
            assert resp.status_code == 200
            assert resp.json()["memory_id"] == v1_memory_id

        # ── AFTER V1: Behavior is Afrikaans ──
        with get_db() as fresh_conn:
            after_v1 = mock_bot_response(fresh_conn, owner_id="kobus", user_message="Help")

        assert after_v1["language"] == "af", "After v1: should be Afrikaans"
        assert after_v1["text"] == "Ja, dit is reg — hier is die antwoord.", "After v1: Afrikaans response"

        # ── PUBLISH V2: Back to English (confidence 0.9, higher) ──
        with patch('secrets.token_hex', return_value='secondv2test67890'):
            v2_memory_id = "shmem-secondv2test67890"
            pref_v2 = {
                "category": "user_preference",
                "domain": "preference.user",
                "content": "Kobus changed his mind, prefers English now",
                "confidence": 0.9,
                "provenance": {
                    "owner_id": "kobus",
                    "reasoning": "User explicitly said 'speak English please' in latest session",
                    "owner_binding": _make_owner_binding_for_memory_id(v2_memory_id, kobus_private_key)
                },
                "preference": {
                    "field": "user.language_preference",
                    "value": "en"
                },
            }

            resp = client.post("/api/v1/memory-commons/publish", json=pref_v2)
            assert resp.status_code == 200
            assert resp.json()["memory_id"] == v2_memory_id

        # ── AFTER V2: Behavior is back to English (latest wins) ──
        with get_db() as fresh_conn:
            after_v2 = mock_bot_response(fresh_conn, owner_id="kobus", user_message="Help")

        assert after_v2["language"] == "en", "After v2: should be English again"
        assert after_v2["text"] == "Yes, here's the answer you requested.", "After v2: English response"

        # ── VERIFY: active_preferences has exactly ONE row for this field ──
        with get_db() as fresh_conn:
            cursor = fresh_conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*), value, source_memory_id, effective_confidence
                FROM active_preferences
                WHERE owner_id = 'kobus' AND field_name = 'user.language_preference'
                GROUP BY value, source_memory_id, effective_confidence
                """
            )
            rows = cursor.fetchall()
            assert len(rows) == 1, "Should have exactly ONE active preference row (upsert works)"
            count, value, source_memory_id, effective_confidence = rows[0]
            assert count == 1
            assert value == "en", "Active preference should have latest value (en)"
            assert source_memory_id == v2_memory_id, "Should point to v2 memory"
            # Note: effective_confidence may be boosted by trust score (e.g., 0.9 * 1.1 = 0.99 for Trusted tier)
            assert effective_confidence >= 0.9, f"Should have v2 confidence >= 0.9 (got {effective_confidence})"

        # ── VERIFY: shared_memories has BOTH versions (audit trail) ──
        with get_db() as fresh_conn:
            cursor = fresh_conn.cursor()
            cursor.execute(
                """
                SELECT id FROM shared_memories
                WHERE category = 'user_preference'
                ORDER BY shared_at
                """
            )
            memory_ids = [row[0] for row in cursor.fetchall()]
            assert v1_memory_id in memory_ids, "v1 memory should be in audit trail"
            assert v2_memory_id in memory_ids, "v2 memory should be in audit trail"
            assert len(memory_ids) >= 2, "Should have at least 2 memories (both versions)"

        # ── THE BEHAVIORAL DELTA: Behavior changed TWICE ──
        assert after_v1["text"] != after_v2["text"], "Behavior MUST change: v1 vs v2"
        assert after_v1["language"] != after_v2["language"], "Language changed: af → en"
