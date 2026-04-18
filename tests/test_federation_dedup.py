"""Tests for federation dedup (Sub-step 3.4)."""

import base64
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from circus.database import init_database, run_v2_migration, run_v3_migration, run_v4_migration
from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.federation_admission import AdmissionResult, admit_bundle
from circus.services.signing import generate_keypair
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


# Test fixtures

@pytest.fixture
def test_db():
    """Create temporary database for testing with federation tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    init_database(db_path)
    run_v2_migration(db_path)
    run_v3_migration(db_path)
    run_v4_migration(db_path)

    # Override settings.database_path for get_db() calls
    from circus.config import settings
    original_db_path = settings.database_path
    settings.database_path = db_path

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn

    conn.close()
    settings.database_path = original_db_path
    db_path.unlink(missing_ok=True)


@pytest.fixture
def valid_keypair():
    """Generate Ed25519 keypair for tests."""
    return generate_keypair()


@pytest.fixture
def registered_peer(test_db, valid_keypair):
    """Register a test peer in federation_peers."""
    _, public_key_bytes = valid_keypair
    peer_id = "peer-test-001"
    now = datetime.utcnow().isoformat()

    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_peers (
            id, name, url, public_key, trust_score, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        peer_id,
        "Test Peer",
        "https://test-peer.example.com",
        public_key_bytes,
        50.0,  # Established tier
        1,
        now,
    ))
    test_db.commit()

    return peer_id, public_key_bytes


@pytest.fixture
def valid_passport():
    """Create a valid AI-IQ passport."""
    return {
        "identity": {
            "name": "peer-test-001",  # Must match peer_id for identity check
            "role": "agent",
        },
        "score": {
            "total": 7.5,
        },
        "generated_at": datetime.utcnow().isoformat(),
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.2, "graph_connections": 15},
    }


@pytest.fixture
def valid_bundle(valid_passport):
    """Create a valid bundle for admission tests."""
    return {
        "bundle_id": "bundle-test-123",
        "peer_id": "peer-test-001",
        "passport": valid_passport,
        "memories": [
            {
                "id": "shmem-xyz-001",
                "content": "Test memory 1",
                "category": "testing",
                "domain": "test-domain",
                "tags": ["test"],
                "provenance": {
                    "hop_count": 1,
                    "original_author": "peer-test-001",
                    "original_timestamp": "2026-04-18T10:00:00Z",
                    "confidence": 0.9,
                },
                "privacy_tier": "public",
                "shared_at": "2026-04-18T10:05:00Z",
            },
            {
                "id": "shmem-xyz-002",
                "content": "Test memory 2",
                "category": "testing",
                "domain": "test-domain",
                "tags": ["test"],
                "provenance": {
                    "hop_count": 1,
                    "original_author": "peer-test-001",
                    "original_timestamp": "2026-04-18T10:00:00Z",
                    "confidence": 0.9,
                },
                "privacy_tier": "public",
                "shared_at": "2026-04-18T10:05:00Z",
            },
            {
                "id": "shmem-xyz-003",
                "content": "Test memory 3",
                "category": "testing",
                "domain": "test-domain",
                "tags": ["test"],
                "provenance": {
                    "hop_count": 1,
                    "original_author": "peer-test-001",
                    "original_timestamp": "2026-04-18T10:00:00Z",
                    "confidence": 0.9,
                },
                "privacy_tier": "public",
                "shared_at": "2026-04-18T10:05:00Z",
            }
        ],
        "timestamp": "2026-04-18T10:05:30Z",
    }


@pytest.fixture
def signed_bundle(valid_bundle, valid_keypair):
    """Create a signed bundle."""
    private_key_bytes, public_key_bytes = valid_keypair

    # Canonicalize and sign
    canonical_bytes = canonicalize_for_signing(valid_bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    signature_b64 = base64.b64encode(signature_bytes).decode('ascii')

    # Add signature to bundle
    bundle_with_sig = {**valid_bundle, "signature": signature_b64}
    return bundle_with_sig


# Happy path tests

def test_admit_new_bundle_all_new_memories(test_db, registered_peer, signed_bundle):
    """Bundle with 3 memories, none seen before."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    result = admit_bundle(signed_bundle, now=now)

    assert result.admitted is True
    assert result.decision == "admitted"
    assert result.memories_total == 3
    assert result.memories_new == 3
    assert result.memories_skipped == 0

    # Verify database state
    cursor = test_db.cursor()

    # 1 row in federation_bundles_seen
    cursor.execute("SELECT COUNT(*) FROM federation_bundles_seen WHERE bundle_id = ?", (signed_bundle["bundle_id"],))
    assert cursor.fetchone()[0] == 1

    # 3 rows in federation_seen
    cursor.execute("SELECT COUNT(*) FROM federation_seen")
    assert cursor.fetchone()[0] == 3


def test_admit_mixed_bundle(test_db, registered_peer, signed_bundle):
    """Bundle with 3 memories: 2 new + 1 already in federation_seen."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # Pre-populate federation_seen with one memory
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_seen (memory_id, first_seen_at, source_instance)
        VALUES (?, ?, ?)
    """, ("shmem-xyz-002", now.isoformat(), peer_id))
    test_db.commit()

    result = admit_bundle(signed_bundle, now=now)

    assert result.admitted is True
    assert result.decision == "admitted"
    assert result.memories_total == 3
    assert result.memories_new == 2
    assert result.memories_skipped == 1

    # Verify: only 2 NEW rows in federation_seen (total 3)
    cursor.execute("SELECT COUNT(*) FROM federation_seen")
    assert cursor.fetchone()[0] == 3


def test_boomerang_backfill(test_db, registered_peer, signed_bundle):
    """Memory exists in shared_memories but NOT in federation_seen."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # Pre-populate shared_memories with one memory
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO shared_memories (
            id, room_id, from_agent_id, content, category, domain, tags, shared_at,
            privacy_tier, hop_count, original_author, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "shmem-xyz-002",
        "room-memory-commons",  # Default room for shared memories
        peer_id,
        "Test memory 2",
        "testing",
        "test-domain",
        json.dumps(["test"]),
        now.isoformat(),
        "public",
        1,
        peer_id,
        0.9,
    ))
    test_db.commit()

    result = admit_bundle(signed_bundle, now=now)

    assert result.admitted is True
    assert result.decision == "admitted"
    assert result.memories_total == 3
    assert result.memories_new == 2
    assert result.memories_skipped == 1  # boomerang detected

    # Verify: backfilled into federation_seen (total 3 rows)
    cursor.execute("SELECT COUNT(*) FROM federation_seen")
    assert cursor.fetchone()[0] == 3

    # Verify: the boomerang memory is in federation_seen
    cursor.execute("SELECT 1 FROM federation_seen WHERE memory_id = ?", ("shmem-xyz-002",))
    assert cursor.fetchone() is not None


# Transport dedup tests

def test_bundle_replay_clean(test_db, registered_peer, signed_bundle):
    """Deliver identical bundle twice."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # First delivery
    result1 = admit_bundle(signed_bundle, now=now)
    assert result1.admitted is True
    assert result1.decision == "admitted"
    assert result1.memories_new == 3

    # Second delivery (clean replay)
    result2 = admit_bundle(signed_bundle, now=now)
    assert result2.admitted is False
    assert result2.decision == "skipped"
    assert result2.reason == "bundle_replay"

    # Verify: federation_seen unchanged (still 3 rows)
    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM federation_seen")
    assert cursor.fetchone()[0] == 3


def test_bundle_tampered(test_db, registered_peer, signed_bundle, valid_keypair):
    """Same bundle_id, mutated contents."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # First delivery
    result1 = admit_bundle(signed_bundle, now=now)
    assert result1.admitted is True
    assert result1.decision == "admitted"

    # Create tampered bundle (same bundle_id, different content)
    tampered = {**signed_bundle}
    tampered["memories"][0]["content"] = "TAMPERED CONTENT"

    # Re-sign with new content
    canonical_bytes = canonicalize_for_signing(tampered)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    tampered["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    # Second delivery (tampered)
    result2 = admit_bundle(tampered, now=now)
    assert result2.admitted is False
    assert result2.decision == "rejected"
    assert result2.reason == "bundle_tampered"
    assert result2.stage_reached == "transport_dedup"

    # Verify: audit row for tamper
    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM federation_audit WHERE reason = 'bundle_tampered'")
    assert cursor.fetchone()[0] == 1

    # Verify: NO second bundles_seen row (original preserved)
    cursor.execute("SELECT COUNT(*) FROM federation_bundles_seen WHERE bundle_id = ?", (signed_bundle["bundle_id"],))
    assert cursor.fetchone()[0] == 1


# Bundle ID derivation tests

def test_auto_generated_bundle_id(test_db, registered_peer, valid_bundle, valid_keypair):
    """Bundle without bundle_id field."""
    import hashlib

    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Remove bundle_id
    bundle_no_id = {k: v for k, v in valid_bundle.items() if k != "bundle_id"}

    # Sign
    canonical_bytes = canonicalize_for_signing(bundle_no_id)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle_no_id["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle_no_id, now=now)

    assert result.admitted is True
    assert result.decision == "admitted"
    assert result.bundle_id is not None
    assert len(result.bundle_id) == 16  # SHA256[:16] = 16 hex chars

    # Verify: deterministic (same bundle → same derived ID)
    expected_id = hashlib.sha256(canonicalize_for_signing(bundle_no_id)).hexdigest()[:16]
    assert result.bundle_id == expected_id


def test_auto_generated_bundle_id_idempotent(test_db, registered_peer, valid_bundle, valid_keypair):
    """Same bundle (no bundle_id) delivered twice."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Remove bundle_id
    bundle_no_id = {k: v for k, v in valid_bundle.items() if k != "bundle_id"}

    # Sign
    canonical_bytes = canonicalize_for_signing(bundle_no_id)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle_no_id["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    # First delivery
    result1 = admit_bundle(bundle_no_id, now=now)
    assert result1.admitted is True
    derived_id = result1.bundle_id

    # Second delivery (same bundle)
    result2 = admit_bundle(bundle_no_id, now=now)
    assert result2.decision == "skipped"
    assert result2.bundle_id == derived_id  # same derived ID


# Quarantine path tests

def test_quarantined_bundle_redelivery_skipped(test_db, registered_peer, valid_bundle, valid_keypair):
    """Bundle with expired passport → quarantined, then re-delivered."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Create expired passport (explicitly expired 1 day ago)
    expired_passport = {
        "identity": {"name": "peer-test-001", "role": "agent"},
        "score": {"total": 5.0},
        "generated_at": (now - timedelta(days=2)).isoformat(),
        "expires_at": (now - timedelta(days=1)).isoformat(),  # Explicitly expired
        "predictions": {"confirmed": 3, "refuted": 1},
        "beliefs": {"total": 8, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 2.5, "graph_connections": 10},
    }

    bundle_expired = {**valid_bundle, "passport": expired_passport}

    # Sign
    canonical_bytes = canonicalize_for_signing(bundle_expired)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle_expired["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    # First delivery → quarantined (passport expired)
    result1 = admit_bundle(bundle_expired, now=now)
    assert result1.admitted is False
    assert result1.decision == "quarantined"
    assert result1.reason == "passport_expired"

    # Verify: 1 row in bundles_seen with decision="quarantined"
    cursor = test_db.cursor()
    cursor.execute("SELECT decision FROM federation_bundles_seen WHERE bundle_id = ?", (valid_bundle["bundle_id"],))
    row = cursor.fetchone()
    assert row is not None
    assert row["decision"] == "quarantined"

    # Second delivery (same bundle) → skipped
    result2 = admit_bundle(bundle_expired, now=now)
    assert result2.decision == "skipped"
    assert result2.reason == "bundle_replay"

    # Verify: still 1 quarantine row (no re-quarantine)
    cursor.execute("SELECT COUNT(*) FROM federation_quarantine")
    assert cursor.fetchone()[0] == 1


# Rejected path tests

def test_rejected_bundle_redelivery_rejected_again(test_db, registered_peer, signed_bundle, valid_keypair):
    """Bundle with invalid signature → rejected, then re-delivered."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # Tamper signature (invalid)
    invalid_bundle = {**signed_bundle, "signature": "INVALID_SIGNATURE_BASE64=="}

    # First delivery → rejected
    result1 = admit_bundle(invalid_bundle, now=now)
    assert result1.admitted is False
    assert result1.decision == "rejected"
    assert result1.reason in ("signature_malformed", "signature_invalid")

    # Verify: NO row in bundles_seen (rejected bundles don't write dedup cache)
    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM federation_bundles_seen")
    assert cursor.fetchone()[0] == 0

    # Second delivery → rejected again (not deduplicated)
    result2 = admit_bundle(invalid_bundle, now=now)
    assert result2.decision == "rejected"


# Persistence guarantees

def test_bundles_seen_only_for_admitted_or_quarantined(test_db, registered_peer, signed_bundle, valid_bundle, valid_keypair):
    """3 bundles: admitted, quarantined, rejected."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # 1. Admitted bundle
    result1 = admit_bundle(signed_bundle, now=now)
    assert result1.decision == "admitted"

    # 2. Quarantined bundle (expired passport)
    expired_passport = {
        "identity": {"name": "peer-test-001", "role": "agent"},
        "score": {"total": 5.0},
        "generated_at": (now - timedelta(days=2)).isoformat(),
        "expires_at": (now - timedelta(days=1)).isoformat(),  # Explicitly expired
        "predictions": {"confirmed": 3, "refuted": 1},
        "beliefs": {"total": 8, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 2.5, "graph_connections": 10},
    }
    bundle_expired = {**valid_bundle, "bundle_id": "bundle-quarantine-001", "passport": expired_passport}
    canonical_bytes = canonicalize_for_signing(bundle_expired)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle_expired["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result2 = admit_bundle(bundle_expired, now=now)
    assert result2.decision == "quarantined"

    # 3. Rejected bundle (invalid signature)
    bundle_rejected = {**valid_bundle, "bundle_id": "bundle-rejected-001", "signature": "INVALID=="}
    result3 = admit_bundle(bundle_rejected, now=now)
    assert result3.decision == "rejected"

    # Verify: bundles_seen has 2 rows (admitted + quarantined), NOT 3
    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM federation_bundles_seen")
    assert cursor.fetchone()[0] == 2

    cursor.execute("SELECT decision FROM federation_bundles_seen ORDER BY decision")
    decisions = [row["decision"] for row in cursor.fetchall()]
    assert set(decisions) == {"admitted", "quarantined"}


# Infrastructure error tests

def test_infra_error_on_bundles_seen_failure(test_db, registered_peer, signed_bundle):
    """Mock bundles_seen table DROP to force infra_error."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # Drop federation_bundles_seen table to force error
    cursor = test_db.cursor()
    cursor.execute("DROP TABLE federation_bundles_seen")
    test_db.commit()

    result = admit_bundle(signed_bundle, now=now)

    assert result.admitted is False
    assert result.decision == "infra_error"

    # Verify: best-effort audit still written
    cursor.execute("SELECT COUNT(*) FROM federation_audit WHERE action = 'bundle_infra_error'")
    assert cursor.fetchone()[0] == 1

    # Verify: NO row in federation_seen (transaction rolled back)
    cursor.execute("SELECT COUNT(*) FROM federation_seen")
    assert cursor.fetchone()[0] == 0


# Backward compatibility tests

def test_admission_result_new_fields_default():
    """Existing callers that construct AdmissionResult without new fields."""
    result = AdmissionResult(
        admitted=True,
        decision="admitted",
        reason=None,
        peer_id="peer-001",
        bundle_id="bundle-001",
    )

    # New fields should default to 0
    assert result.memories_total == 0
    assert result.memories_new == 0
    assert result.memories_skipped == 0


def test_existing_admission_tests_still_pass(test_db, registered_peer, signed_bundle):
    """Existing admission test pattern still works."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    result = admit_bundle(signed_bundle, now=now)

    # Old assertions
    assert result.admitted is True
    assert result.decision == "admitted"
    assert result.peer_id == peer_id
    assert result.bundle_id == signed_bundle["bundle_id"]

    # New fields exist
    assert hasattr(result, "memories_total")
    assert hasattr(result, "memories_new")
    assert hasattr(result, "memories_skipped")


# Hash determinism test

def test_bundle_hash_deterministic(valid_bundle):
    """Same bundle → same bundle_hash."""
    from circus.services.federation_admission import _compute_bundle_hash

    hash1 = _compute_bundle_hash(valid_bundle)
    hash2 = _compute_bundle_hash(valid_bundle)

    assert hash1 == hash2
    assert len(hash1) == 64  # SHA256 hex = 64 chars
