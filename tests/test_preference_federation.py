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
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from circus.database import init_database, run_v2_migration, run_v3_migration, run_v7_migration, get_db
from circus.services.federation_wiring import admit_and_merge


# ── W5 Helper: Generate test owner keypair for federation tests ──

_FEDERATION_TEST_OWNER_KEY = None


def _get_or_create_federation_test_owner_key():
    """Get or create a test owner keypair (cached for performance across tests)."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    global _FEDERATION_TEST_OWNER_KEY

    if _FEDERATION_TEST_OWNER_KEY is None:
        private_key = Ed25519PrivateKey.generate()
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')
        _FEDERATION_TEST_OWNER_KEY = (private_key, public_key_b64)

    return _FEDERATION_TEST_OWNER_KEY


def _register_test_owner_key(conn, owner_id="kobus"):
    """Register test owner public key in the database."""
    _, public_key_b64 = _get_or_create_federation_test_owner_key()
    conn.execute(
        "INSERT OR REPLACE INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
        (owner_id, public_key_b64, datetime.utcnow().isoformat())
    )
    conn.commit()


def _sign_owner_binding(memory_id, agent_id="agent-kobus-001", owner_id="kobus", timestamp=None):
    """Create a valid signed owner_binding for testing."""
    import base64
    from circus.services.bundle_signing import canonicalize_for_signing

    if timestamp is None:
        timestamp = datetime.utcnow().isoformat()

    private_key, _ = _get_or_create_federation_test_owner_key()

    payload = {
        "agent_id": agent_id,
        "memory_id": memory_id,
        "owner_id": owner_id,
        "timestamp": timestamp,
    }
    canonical_bytes = canonicalize_for_signing(payload)
    signature = private_key.sign(canonical_bytes)

    return {
        "agent_id": agent_id,
        "memory_id": memory_id,
        "timestamp": timestamp,
        "signature": base64.b64encode(signature).decode('ascii')
    }


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
    W5: Updated to include valid owner signature.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        # Reset cached owner AFTER patching env
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        # W5: Register owner public key
        with get_db() as conn:
            _register_test_owner_key(conn, "kobus")

        now = datetime.utcnow()
        peer_id = "peer-remote-002"
        memory_id = "mem-pref-fed-001"

        # Construct federated bundle with user_preference memory
        # Confidence high enough that after hop_count decay (1→2), effective_confidence >= 0.7
        federated_bundle = {
            "bundle_id": "bundle-pref-001",
            "peer_id": peer_id,
            "memories": [
                {
                    "id": memory_id,
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
                        "owner_binding": _sign_owner_binding(memory_id, "agent-kobus-001", "kobus")
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
    W5: Updated to include valid owner signature.
    """
    import logging
    caplog.set_level(logging.INFO, logger="circus.services.preference_admission")

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        # W5: Register owner public key
        with get_db() as conn:
            _register_test_owner_key(conn, "kobus")

        now = datetime.utcnow()
        peer_id = "peer-remote-003"
        memory_id = "mem-pref-fed-low"

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
                    "id": memory_id,
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
                        "owner_binding": _sign_owner_binding(memory_id, "agent-kobus-002", "kobus")
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

        # Assert structured log emitted — W11: borderline confidence may be quarantined instead of skipped
        assert ("preference_skipped" in caplog.text or "preference_quarantined" in caplog.text)
        skip_logs = [r for r in caplog.records if r.message in ("preference_skipped", "preference_quarantined")]
        assert len(skip_logs) >= 1, "Should log skip or quarantine for confidence_below_threshold"

        # Verify log includes reason (4.3's locked log shape; quarantine uses same extra fields)
        log_extra = skip_logs[-1].__dict__
        assert log_extra.get("reason") in ("confidence_below_threshold", "confidence_borderline"), "Log should have correct reason"
        assert "effective_confidence" in log_extra, "Log should include effective_confidence"
        assert "threshold" in log_extra, "Log should include threshold"


def test_federated_preference_same_owner_mismatch_skips(test_db, reset_server_owner, caplog):
    """Test: Federated preference with different owner_id is skipped.

    Fresh-connection discipline enforced.
    W5: Updated to include valid owner signature (but for jaco, not kobus).
    """
    import logging
    caplog.set_level(logging.INFO, logger="circus.services.preference_admission")

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        # W5: Register kobus's owner key (but NOT jaco's)
        with get_db() as conn:
            _register_test_owner_key(conn, "kobus")

        now = datetime.utcnow()
        peer_id = "peer-remote-004"
        memory_id = "mem-pref-fed-jaco"

        # Federated preference has owner_id=jaco (NOT kobus)
        # Even with valid signature for jaco, it should be skipped (same-owner gate)
        federated_bundle = {
            "bundle_id": "bundle-pref-003",
            "peer_id": peer_id,
            "memories": [
                {
                    "id": memory_id,
                    "content": "User prefers tone preference",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "tags": ["tone"],
                    "privacy_tier": "public",
                    "provenance": {
                        "hop_count": 1,
                        "original_author": "agent-jaco-001",
                        "original_timestamp": now.isoformat(),
                        "confidence": 0.95,  # High confidence (would pass threshold)
                        "owner_id": "jaco",  # Different owner
                        "owner_binding": _sign_owner_binding(memory_id, "agent-jaco-001", "jaco")
                    },
                    "preference": {
                        "field": "user.tone_preference",
                        "value": "formal",
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
                ("kobus", "user.tone_preference")
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
    W5: Updated to include valid owner signature.
    """
    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        # W5: Register owner public key
        with get_db() as conn:
            _register_test_owner_key(conn, "kobus")

        now = datetime.utcnow()
        peer_id = "peer-remote-005"
        memory_id = "mem-pref-fed-replay"

        federated_bundle = {
            "bundle_id": "bundle-pref-004",
            "peer_id": peer_id,
            "memories": [
                {
                    "id": memory_id,
                    "content": "User prefers plain format output",
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
                        "owner_binding": _sign_owner_binding(memory_id, "agent-kobus-003", "kobus")
                    },
                    "preference": {
                        "field": "user.format_preference",
                        "value": "plain",
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
                ("kobus", "user.format_preference")
            )
            count = cursor.fetchone()[0]
            assert count == 1, "Should have exactly ONE row (idempotent on replay)"

            # Verify value and confidence are correct (not corrupted)
            cursor = read_conn.execute(
                "SELECT value, effective_confidence FROM active_preferences WHERE owner_id = ? AND field_name = ?",
                ("kobus", "user.format_preference")
            )
            row = cursor.fetchone()
            assert row[0] == "plain", "Value should be preserved"
            # Confidence should be the decayed value (not doubled or corrupted)
            # With hop_count=2, confidence=0.9, trust=50.0, age=0 → effective ≈ 0.81
            assert row[1] > 0.7, "Effective confidence should still be above threshold"


# ── W5 Ship Gate Tests: Federated Signed-Only Behavior Change ──


def test_w5_scenario_3_valid_federated_signature_changes_behavior(test_db, reset_server_owner):
    """W5 Scenario 3: Valid federated flow with owner signature activates preference.

    Node A (Friday) signs + publishes preference.
    Node B (Claw) pulls federated bundle.
    AFTER: Claw's behavior reflects the preference.

    This proves signed preferences activate across federated nodes.
    """
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from circus.services.bundle_signing import canonicalize_for_signing
    from circus.services.preference_application import get_active_preferences

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        # ── SETUP: Generate owner keypair and register public key on Node B ──
        private_key = Ed25519PrivateKey.generate()
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

        with get_db() as conn:
            conn.execute(
                "INSERT INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
                ("kobus", public_key_b64, datetime.utcnow().isoformat())
            )
            conn.commit()

        # ── BEFORE: No active preferences on Node B ──
        with get_db() as conn:
            prefs_before = get_active_preferences(conn, "kobus")
            assert prefs_before == {}, "BEFORE: no active preferences"

        # ── TRIGGER: Node A publishes signed preference, Node B pulls via federation ──
        now = datetime.utcnow()
        memory_id = "mem-pref-fed-w5s3-valid"
        timestamp = now.isoformat()

        # Node A signs the preference
        payload = {
            "agent_id": "agent-friday",
            "memory_id": memory_id,
            "owner_id": "kobus",
            "timestamp": timestamp,
        }
        canonical_bytes = canonicalize_for_signing(payload)
        signature = private_key.sign(canonical_bytes)
        signature_b64 = base64.b64encode(signature).decode('ascii')

        federated_bundle = {
            "bundle_id": "bundle-w5s3-valid",
            "peer_id": "peer-friday-node-a",
            "memories": [
                {
                    "id": memory_id,
                    "content": "User prefers terse responses",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "tags": ["verbosity", "preference"],
                    "privacy_tier": "public",
                    "provenance": {
                        "hop_count": 1,
                        "original_author": "agent-friday",
                        "original_timestamp": now.isoformat(),
                        "confidence": 0.9,
                        "owner_id": "kobus",
                        "owner_binding": {
                            "agent_id": "agent-friday",
                            "memory_id": memory_id,
                            "timestamp": timestamp,
                            "signature": signature_b64
                        }
                    },
                    "preference": {
                        "field": "user.response_verbosity",
                        "value": "terse",
                    },
                }
            ],
        }

        # Node B admits federated bundle
        with get_db() as write_conn:
            admit_and_merge(bundle=federated_bundle, peer_id="peer-friday-node-a", now=now)

        # ── AFTER: Preference is activated on Node B ──
        with get_db() as read_conn:
            prefs_after = get_active_preferences(read_conn, "kobus")
            assert prefs_after.get("user.response_verbosity") == "terse", "AFTER: federated preference activated"

        # ── BEHAVIORAL DELTA ──
        assert prefs_before != prefs_after, "Behavior changed: federated signed preference activated"


def test_w5_scenario_4_federated_attack_preserved_prior_state(test_db, reset_server_owner, caplog):
    """W5 Scenario 4: Federated attack with invalid signature → prior state preserved.

    BEFORE: Valid signed preference (verbosity=terse) is active on Node B
    TRIGGER: Malicious Node C publishes preference with bad signature (claims verbosity=verbose)
    AFTER: Node B STILL has verbosity=terse (prior valid preference unchanged)

    THE CRITICAL W5 ASSERTION: Federated bad input cannot knock out good state.
    """
    import base64
    import logging
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from circus.services.bundle_signing import canonicalize_for_signing
    from circus.services.preference_application import get_active_preferences

    caplog.set_level(logging.INFO, logger="circus.services.preference_admission")

    with patch.dict(os.environ, {"CIRCUS_OWNER_ID": "kobus"}):
        import circus.services.preference_admission as admission_module
        admission_module._SERVER_OWNER = None

        # ── SETUP: Generate kobus's real keypair and register on Node B ──
        kobus_private_key = Ed25519PrivateKey.generate()
        kobus_public_bytes = kobus_private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        kobus_public_key_b64 = base64.b64encode(kobus_public_bytes).decode('ascii')

        with get_db() as conn:
            conn.execute(
                "INSERT INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
                ("kobus", kobus_public_key_b64, datetime.utcnow().isoformat())
            )
            conn.commit()

        # ── SEED: Node B has valid signed preference (verbosity=terse) ──
        now = datetime.utcnow()
        seed_memory_id = "mem-pref-fed-w5s4-seed"
        seed_timestamp = now.isoformat()

        seed_payload = {
            "agent_id": "agent-friday",
            "memory_id": seed_memory_id,
            "owner_id": "kobus",
            "timestamp": seed_timestamp,
        }
        seed_canonical_bytes = canonicalize_for_signing(seed_payload)
        seed_signature = kobus_private_key.sign(seed_canonical_bytes)
        seed_signature_b64 = base64.b64encode(seed_signature).decode('ascii')

        seed_bundle = {
            "bundle_id": "bundle-w5s4-seed",
            "peer_id": "peer-friday-node-a",
            "memories": [
                {
                    "id": seed_memory_id,
                    "content": "User prefers terse responses",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "tags": ["verbosity"],
                    "privacy_tier": "public",
                    "provenance": {
                        "hop_count": 1,
                        "original_author": "agent-friday",
                        "original_timestamp": now.isoformat(),
                        "confidence": 0.9,
                        "owner_id": "kobus",
                        "owner_binding": {
                            "agent_id": "agent-friday",
                            "memory_id": seed_memory_id,
                            "timestamp": seed_timestamp,
                            "signature": seed_signature_b64
                        }
                    },
                    "preference": {
                        "field": "user.response_verbosity",
                        "value": "terse",
                    },
                }
            ],
        }

        with get_db() as write_conn:
            admit_and_merge(bundle=seed_bundle, peer_id="peer-friday-node-a", now=now)

        # ── VERIFY: terse is active ──
        with get_db() as conn:
            prefs_before = get_active_preferences(conn, "kobus")
            assert prefs_before.get("user.response_verbosity") == "terse", "Seed preference should be active"

        # ── ATTACK: Malicious Node C sends preference with bad signature ──
        # Attacker generates their own throwaway key
        attacker_private_key = Ed25519PrivateKey.generate()

        attack_memory_id = "mem-pref-fed-w5s4-attack"
        attack_timestamp = (now + timedelta(seconds=30)).isoformat()

        attack_payload = {
            "agent_id": "agent-attacker",
            "memory_id": attack_memory_id,
            "owner_id": "kobus",  # Claims to be kobus
            "timestamp": attack_timestamp,
        }
        attack_canonical_bytes = canonicalize_for_signing(attack_payload)
        attack_signature = attacker_private_key.sign(attack_canonical_bytes)  # BAD SIGNATURE
        attack_signature_b64 = base64.b64encode(attack_signature).decode('ascii')

        attack_bundle = {
            "bundle_id": "bundle-w5s4-attack",
            "peer_id": "peer-malicious-node-c",
            "memories": [
                {
                    "id": attack_memory_id,
                    "content": "User prefers verbose responses (attacker claim)",
                    "category": "user_preference",
                    "domain": "preference.user",
                    "tags": ["verbosity"],
                    "privacy_tier": "public",
                    "provenance": {
                        "hop_count": 1,
                        "original_author": "agent-attacker",
                        "original_timestamp": attack_timestamp,
                        "confidence": 0.95,  # High confidence (would activate if signature valid)
                        "owner_id": "kobus",
                        "owner_binding": {
                            "agent_id": "agent-attacker",
                            "memory_id": attack_memory_id,
                            "timestamp": attack_timestamp,
                            "signature": attack_signature_b64  # Invalid signature
                        }
                    },
                    "preference": {
                        "field": "user.response_verbosity",
                        "value": "verbose",
                    },
                }
            ],
        }

        with get_db() as write_conn:
            admit_and_merge(bundle=attack_bundle, peer_id="peer-malicious-node-c", now=now)

        # ── AFTER: Prior valid preference STILL ACTIVE (unchanged) ──
        with get_db() as read_conn:
            prefs_after = get_active_preferences(read_conn, "kobus")
            assert prefs_after.get("user.response_verbosity") == "terse", "Prior valid preference MUST be preserved"

            # Verify attack memory is in shared_memories (audit trail)
            cursor = read_conn.execute(
                "SELECT id FROM shared_memories WHERE id = ?",
                (attack_memory_id,)
            )
            assert cursor.fetchone() is not None, "Attack memory should be in shared_memories (audit path)"

            # Verify active_preferences still has terse (not overwritten)
            cursor = read_conn.execute(
                "SELECT value FROM active_preferences WHERE owner_id = 'kobus' AND field_name = 'user.response_verbosity'"
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "terse", "Active preference value should NOT be overwritten by bad signature"

        # ── Verify skip log with reason code ──
        assert "preference_skipped" in caplog.text
        skip_logs = [r for r in caplog.records if r.message == "preference_skipped"]
        assert len(skip_logs) >= 1, "Should log skip for bad signature"
        log_extra = skip_logs[-1].__dict__
        assert log_extra.get("reason") == "owner_signature_invalid", f"Expected owner_signature_invalid, got {log_extra.get('reason')}"

        # ── BEHAVIORAL DELTA ──
        assert prefs_before == prefs_after, "Behavior UNCHANGED: federated bad signature did NOT flip state"
