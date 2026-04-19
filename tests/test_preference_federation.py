"""Federation integration tests for preference admission (Week 4 sub-step 4.5).

This test module proves that:
1. Federated preference memories above threshold activate after hop_count decay
2. Federated preference memories below threshold (post-decay) are skipped
3. Same-owner enforcement works for federated preferences
4. Replay/re-federation is idempotent (no duplicate active_preferences rows)

All tests use fresh connections for assertions (4.4 discipline).
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from circus.database import init_database, run_v2_migration, run_v3_migration, run_v7_migration, get_db
from circus.services.federation_wiring import admit_and_merge


@pytest.fixture
def test_db():
    """Create temporary database for testing with federation + preference tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    init_database(db_path)
    run_v2_migration(db_path)
    run_v3_migration(db_path)
    run_v7_migration(db_path)  # Week 4: active_preferences table

    # Override settings.database_path for get_db() calls
    from circus.config import settings
    original_db_path = settings.database_path
    settings.database_path = db_path

    yield db_path

    settings.database_path = original_db_path
    db_path.unlink(missing_ok=True)


@pytest.fixture
def reset_server_owner():
    """Reset cached server owner between tests."""
    import circus.services.preference_admission as admission_module
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False
    yield
    admission_module._SERVER_OWNER = None
    admission_module._WARN_LOGGED = False


def test_federated_preference_above_threshold_activates(test_db, reset_server_owner):
    """Test: Federated preference with high confidence (post-decay >= 0.7) activates.

    Fresh-connection discipline: Write with one connection, assert with a fresh connection.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner AFTER patching env
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        now = datetime.utcnow()
        peer_id = "peer-remote-002"

        # Construct federated bundle with user_preference memory
        # Confidence high enough that after hop_count decay (1→2), effective_confidence >= 0.7
        federated_bundle = {
            "bundle_id": "bundle-pref-001",
            "peer_id": peer_id,
            "memories": [
                {
                    "id": "mem-pref-fed-001",
                    "content": "User prefers Afrikaans for bot responses",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "tags": ["language", "preference"],
                    "privacy_tier": "public",
                    "provenance": {
                        "hop_count": 1,  # Will be incremented to 2
                        "original_author": "agent-kobus-001",
                        "original_timestamp": now.isoformat(),
                        "confidence": 0.95,  # High confidence to survive decay
                        "owner_id": "kobus",  # Same owner as server
                    },
                    "preference": {
                        "field": "user.language_preference",
                        "value": "af",
                    },
                }
            ],
        }

        # Write path: admit_and_merge with connection
        with get_db() as write_conn:
            admit_and_merge(bundle=federated_bundle, peer_id=peer_id, now=now)
            # admit_and_merge commits internally (verified at line 99 of federation_wiring.py)

        # Assertion path: open FRESH connection to read
        with get_db() as read_conn:
            cursor = read_conn.execute(
                "SELECT id, category FROM shared_memories WHERE id = ?",
                ("mem-pref-fed-001",)
            )
            memory_row = cursor.fetchone()
            assert memory_row is not None, "Memory should be in shared_memories"
            assert memory_row[1] == "user_preference"

            # Assert row exists in active_preferences
            cursor = read_conn.execute(
                "SELECT owner_id, field_name, value FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.language_preference")
            )
            pref_row = cursor.fetchone()
            assert pref_row is not None, "Preference should be activated (above threshold after decay)"
            assert pref_row[0] == "kobus"
            assert pref_row[1] == "user.language_preference"
            assert pref_row[2] == "af"


def test_federated_preference_below_threshold_after_decay_skips(test_db, reset_server_owner, caplog):
    """Test: Federated preference with low confidence (post-decay < 0.7) is skipped.

    Fresh-connection discipline enforced.
    """
    import logging
    caplog.set_level(logging.INFO, logger="circus.services.preference_admission")

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        now = datetime.utcnow()
        peer_id = "peer-remote-003"

        # Low confidence that will fall below 0.7 after decay (hop_count 1→2, trust=50.0 default)
        # decay_confidence formula: base * hop_decay * age_decay * trust_multiplier
        # hop_decay for hop=2 is 0.9 (per provenance.py)
        # trust=50.0 → trust_multiplier ≈ 1.0 (no penalty)
        # So effective_conf ≈ 0.6 * 0.9 = 0.54 < 0.7
        federated_bundle = {
            "bundle_id": "bundle-pref-002",
            "peer_id": peer_id,
            "memories": [
                {
                    "id": "mem-pref-fed-low",
                    "content": "User prefers terse responses",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "tags": ["verbosity"],
                    "privacy_tier": "public",
                    "provenance": {
                        "hop_count": 1,
                        "original_author": "agent-kobus-002",
                        "original_timestamp": now.isoformat(),
                        "confidence": 0.6,  # Will decay to ~0.54 after hop_count increment
                        "owner_id": "kobus",
                    },
                    "preference": {
                        "field": "user.response_verbosity",
                        "value": "terse",
                    },
                }
            ],
        }

        # Write path
        with get_db() as write_conn:
            admit_and_merge(bundle=federated_bundle, peer_id=peer_id, now=now)

        # Assertion path: FRESH connection
        with get_db() as read_conn:
            # Memory should exist in shared_memories (audit preserved)
            cursor = read_conn.execute(
                "SELECT id FROM shared_memories WHERE id = ?",
                ("mem-pref-fed-low",)
            )
            assert cursor.fetchone() is not None, "Memory should be in shared_memories"

            # NO row in active_preferences
            cursor = read_conn.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.response_verbosity")
            )
            count = cursor.fetchone()[0]
            assert count == 0, "Preference should NOT be activated (below threshold after decay)"

        # Assert structured log emitted with reason=confidence_below_threshold
        assert "preference_skipped" in caplog.text
        skip_logs = [r for r in caplog.records if r.message == "preference_skipped"]
        assert len(skip_logs) >= 1, "Should log skip with reason=confidence_below_threshold"

        # Verify log includes effective_confidence and threshold (4.3's locked log shape)
        log_extra = skip_logs[-1].__dict__
        assert log_extra.get("reason") == "confidence_below_threshold", "Log should have correct reason"
        assert "effective_confidence" in log_extra, "Log should include effective_confidence"
        assert "threshold" in log_extra, "Log should include threshold"


def test_federated_preference_same_owner_mismatch_skips(test_db, reset_server_owner, caplog):
    """Test: Federated preference with different owner_id is skipped.

    Fresh-connection discipline enforced.
    """
    import logging
    caplog.set_level(logging.INFO, logger="circus.services.preference_admission")

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        now = datetime.utcnow()
        peer_id = "peer-remote-004"

        # Federated preference has owner_id=jaco (NOT kobus)
        federated_bundle = {
            "bundle_id": "bundle-pref-003",
            "peer_id": peer_id,
            "memories": [
                {
                    "id": "mem-pref-fed-jaco",
                    "content": "User prefers dark theme",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "tags": ["theme"],
                    "privacy_tier": "public",
                    "provenance": {
                        "hop_count": 1,
                        "original_author": "agent-jaco-001",
                        "original_timestamp": now.isoformat(),
                        "confidence": 0.95,  # High confidence (would pass threshold)
                        "owner_id": "jaco",  # Different owner
                    },
                    "preference": {
                        "field": "user.theme_preference",
                        "value": "dark",
                    },
                }
            ],
        }

        # Write path
        with get_db() as write_conn:
            admit_and_merge(bundle=federated_bundle, peer_id=peer_id, now=now)

        # Assertion path: FRESH connection
        with get_db() as read_conn:
            # Memory in shared_memories (audit preserved)
            cursor = read_conn.execute(
                "SELECT id FROM shared_memories WHERE id = ?",
                ("mem-pref-fed-jaco",)
            )
            assert cursor.fetchone() is not None

            # No row in active_preferences for jaco
            cursor = read_conn.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ?",
                ("jaco",)
            )
            count = cursor.fetchone()[0]
            assert count == 0, "Preference should NOT be activated for jaco (same-owner mismatch)"

            # Also verify no row written to kobus
            cursor = read_conn.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.theme_preference")
            )
            count = cursor.fetchone()[0]
            assert count == 0, "Preference should NOT leak to kobus"

        # Assert structured log with reason=same_owner_failed
        assert "preference_skipped" in caplog.text
        skip_logs = [r for r in caplog.records if r.message == "preference_skipped"]
        assert len(skip_logs) >= 1, "Should log skip with reason=same_owner_failed"
        log_extra = skip_logs[-1].__dict__
        assert log_extra.get("reason") == "same_owner_failed", "Log should have correct reason"


def test_federated_preference_replay_is_idempotent(test_db, reset_server_owner):
    """Test: Re-federating same bundle is idempotent (no duplicate active_preferences rows).

    Fresh-connection discipline enforced.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        now = datetime.utcnow()
        peer_id = "peer-remote-005"

        federated_bundle = {
            "bundle_id": "bundle-pref-004",
            "peer_id": peer_id,
            "memories": [
                {
                    "id": "mem-pref-fed-replay",
                    "content": "User prefers structured output",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "tags": ["format"],
                    "privacy_tier": "public",
                    "provenance": {
                        "hop_count": 1,
                        "original_author": "agent-kobus-003",
                        "original_timestamp": now.isoformat(),
                        "confidence": 0.9,
                        "owner_id": "kobus",
                    },
                    "preference": {
                        "field": "user.output_format_preference",
                        "value": "structured",
                    },
                }
            ],
        }

        # First admission
        with get_db() as write_conn:
            admit_and_merge(bundle=federated_bundle, peer_id=peer_id, now=now)

        # Second admission (replay)
        with get_db() as write_conn:
            admit_and_merge(bundle=federated_bundle, peer_id=peer_id, now=now)

        # Assertion path: FRESH connection
        with get_db() as read_conn:
            # Exactly ONE row in active_preferences
            cursor = read_conn.execute(
                "SELECT COUNT(*) FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.output_format_preference")
            )
            count = cursor.fetchone()[0]
            assert count == 1, "Should have exactly ONE row (idempotent on replay)"

            # Verify value and confidence are correct (not corrupted)
            cursor = read_conn.execute(
                "SELECT value, effective_confidence FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.output_format_preference")
            )
            row = cursor.fetchone()
            assert row[0] == "structured", "Value should be preserved"
            # Confidence should be the decayed value (not doubled or corrupted)
            # With hop_count=2, confidence=0.9, trust=50.0, age=0 → effective ≈ 0.81
            assert row[1] > 0.7, "Effective confidence should still be above threshold"
