"""Tests for federation admission pipeline (Sub-step 3.3)."""

import base64
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from circus.database import init_database, run_v2_migration, run_v3_migration
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
                "id": "shmem-xyz",
                "content": "Test memory",
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

def test_admit_valid_bundle(test_db, registered_peer, signed_bundle):
    """Valid bundle with all verifications passing should be admitted."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    result = admit_bundle(signed_bundle, now=now)

    assert result.admitted is True
    assert result.decision == "admitted"
    assert result.reason is None
    assert result.stage_reached == "admitted"
    assert result.peer_id == peer_id
    assert result.bundle_id == "bundle-test-123"
    assert result.quarantine_id is None  # No quarantine row
    assert result.audit_id is not None  # Audit row written
    assert result.detail == "all verifications passed"

    # Verify audit row written
    cursor = test_db.cursor()
    cursor.execute("SELECT * FROM federation_audit WHERE id = ?", (result.audit_id,))
    audit_row = cursor.fetchone()
    assert audit_row is not None
    assert audit_row["action"] == "bundle_admitted"
    assert audit_row["actor_passport"] == peer_id
    assert audit_row["target_id"] == "bundle-test-123"
    assert audit_row["reason"] is None

    # Verify no quarantine row
    cursor.execute("SELECT COUNT(*) FROM federation_quarantine")
    assert cursor.fetchone()[0] == 0


# Hard-reject paths (audit only, no quarantine)

def test_admit_signature_malformed_missing(test_db, registered_peer, valid_bundle):
    """Bundle with missing signature field should be hard-rejected."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # No signature field
    result = admit_bundle(valid_bundle, now=now)

    assert result.admitted is False
    assert result.decision == "rejected"
    assert result.reason == "signature_malformed"
    assert result.stage_reached == "verify_signature"
    assert result.detail == "signature field missing"

    # Verify audit row written
    cursor = test_db.cursor()
    cursor.execute("SELECT * FROM federation_audit WHERE id = ?", (result.audit_id,))
    audit_row = cursor.fetchone()
    assert audit_row is not None
    assert audit_row["action"] == "bundle_rejected"

    # Verify no quarantine row
    cursor.execute("SELECT COUNT(*) FROM federation_quarantine")
    assert cursor.fetchone()[0] == 0


def test_admit_signature_malformed_wrong_type(test_db, registered_peer, valid_bundle):
    """Bundle with non-string signature should be hard-rejected."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    bundle = {**valid_bundle, "signature": 12345}
    result = admit_bundle(bundle, now=now)

    assert result.admitted is False
    assert result.decision == "rejected"
    assert result.reason == "signature_malformed"
    assert "wrong type" in result.detail


def test_admit_signature_invalid(test_db, registered_peer, valid_bundle):
    """Bundle with invalid signature should be hard-rejected."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # Add bogus signature
    bundle = {**valid_bundle, "signature": base64.b64encode(b"wrong" * 16).decode('ascii')}
    result = admit_bundle(bundle, now=now)

    assert result.admitted is False
    assert result.decision == "rejected"
    assert result.reason == "signature_invalid"
    assert result.stage_reached == "verify_signature"


def test_admit_malformed_bundle_missing_peer_id(test_db):
    """Bundle missing peer_id should be hard-rejected."""
    now = datetime.utcnow()
    bundle = {"bundle_id": "test-123"}  # No peer_id

    result = admit_bundle(bundle, now=now)

    assert result.admitted is False
    assert result.decision == "rejected"
    assert result.reason == "malformed_bundle"
    assert result.stage_reached == "peer_lookup"
    assert "peer_id" in result.detail


def test_admit_missing_bundle_id_auto_derives(test_db):
    """Bundle missing bundle_id is no longer malformed — it auto-derives.

    Per Sub-step 3.4 design §4.1: if bundle_id is absent, derive from
    sha256(canonicalize_for_signing(bundle))[:16]. Absence is NOT a hard reject.
    The bundle still flows through the pipeline and fails at the next
    applicable verifier (here: no peer registered → quarantined as passport_unknown
    or similar). The test only pins the contract: missing bundle_id ≠ malformed.
    """
    now = datetime.utcnow()
    bundle = {"peer_id": "peer-test-001"}  # No bundle_id, no passport, no signature

    result = admit_bundle(bundle, now=now)

    # bundle_id is derived (not None) even though the bundle didn't provide one
    assert result.bundle_id is not None
    assert len(result.bundle_id) == 16  # sha256 prefix per design §4.1
    # Admission still fails — but NOT for missing bundle_id
    assert result.admitted is False
    assert result.reason != "malformed_bundle"


def test_admit_passport_peer_mismatch(test_db, registered_peer, valid_bundle, valid_keypair):
    """Bundle where passport identity != peer_id should be hard-rejected."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Tamper with passport identity
    bundle = {**valid_bundle}
    bundle["passport"]["identity"]["name"] = "different-peer-id"

    # Re-sign the bundle with mismatched identity
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    assert result.admitted is False
    assert result.decision == "rejected"
    assert result.reason == "passport_peer_mismatch"
    assert result.stage_reached == "verify_passport_structure"
    assert "different-peer-id" in result.detail
    assert bundle["peer_id"] in result.detail

    # Verify no quarantine row (hard reject)
    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM federation_quarantine")
    assert cursor.fetchone()[0] == 0

    # Verify audit row written
    cursor.execute("SELECT * FROM federation_audit WHERE id = ?", (result.audit_id,))
    audit_row = cursor.fetchone()
    assert audit_row is not None
    assert audit_row["action"] == "bundle_rejected"


# Quarantine paths (quarantine + audit)

def test_admit_passport_unknown(test_db, signed_bundle):
    """Bundle from unknown peer should be quarantined."""
    # Don't register peer — no row in federation_peers
    now = datetime.utcnow()

    result = admit_bundle(signed_bundle, now=now)

    assert result.admitted is False
    assert result.decision == "quarantined"
    assert result.reason == "passport_unknown"
    assert result.stage_reached == "peer_lookup"
    assert result.quarantine_id is not None
    assert result.audit_id is not None

    # Verify quarantine row written
    cursor = test_db.cursor()
    cursor.execute("SELECT * FROM federation_quarantine WHERE id = ?", (result.quarantine_id,))
    q_row = cursor.fetchone()
    assert q_row is not None
    assert q_row["reason"] == "passport_unknown"
    assert q_row["source_instance"] == "peer-test-001"

    # Verify audit row written
    cursor.execute("SELECT * FROM federation_audit WHERE id = ?", (result.audit_id,))
    audit_row = cursor.fetchone()
    assert audit_row is not None
    assert audit_row["action"] == "bundle_quarantined"
    metadata = json.loads(audit_row["metadata"])
    assert metadata["quarantine_id"] == result.quarantine_id


def test_admit_passport_unknown_inactive(test_db, valid_keypair, signed_bundle):
    """Bundle from inactive peer should be quarantined."""
    _, public_key_bytes = valid_keypair
    peer_id = "peer-test-001"
    now = datetime.utcnow()

    # Register peer but set is_active=0
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_peers (
            id, name, url, public_key, trust_score, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (peer_id, "Inactive Peer", "https://inactive.example.com",
          public_key_bytes, 50.0, 0, now.isoformat()))
    test_db.commit()

    result = admit_bundle(signed_bundle, now=now)

    assert result.admitted is False
    assert result.decision == "quarantined"
    assert result.reason == "passport_unknown"
    assert "inactive" in result.detail


def test_admit_passport_malformed(test_db, registered_peer, valid_bundle, valid_keypair):
    """Bundle with malformed passport should be quarantined."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Create bundle with broken passport (missing identity entirely)
    bundle = {**valid_bundle}
    bundle["passport"] = {"score": {"total": 5.0}}  # Missing identity field

    # Re-sign the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    assert result.admitted is False
    assert result.decision == "quarantined"
    assert result.reason == "passport_malformed"
    assert result.stage_reached == "verify_passport_structure"


def test_admit_passport_expired(test_db, registered_peer, valid_bundle, valid_keypair):
    """Bundle with expired passport should be quarantined."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Set passport expiry in the past
    bundle = {**valid_bundle}
    bundle["passport"]["expires_at"] = (now - timedelta(days=1)).isoformat()

    # Re-sign the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    assert result.admitted is False
    assert result.decision == "quarantined"
    assert result.reason == "passport_expired"
    assert result.stage_reached == "verify_passport_expiry"


def test_admit_passport_generated_at_stale(test_db, registered_peer, valid_bundle, valid_keypair):
    """Bundle with stale passport (>90 days old) should be quarantined."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Set generated_at to 91 days ago (no expires_at field)
    bundle = {**valid_bundle}
    bundle["passport"]["generated_at"] = (now - timedelta(days=91)).isoformat()
    # Remove expires_at if present
    bundle["passport"].pop("expires_at", None)

    # Re-sign the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    assert result.admitted is False
    assert result.decision == "quarantined"
    assert result.reason == "passport_expired"
    assert "91 days ago" in result.detail


def test_admit_peer_untrusted(test_db, valid_keypair, signed_bundle):
    """Bundle from untrusted peer (low trust score) should be quarantined."""
    _, public_key_bytes = valid_keypair
    peer_id = "peer-test-001"
    now = datetime.utcnow()

    # Register peer with trust_score below Established tier (< 30.0)
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_peers (
            id, name, url, public_key, trust_score, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (peer_id, "Untrusted Peer", "https://untrusted.example.com",
          public_key_bytes, 20.0, 1, now.isoformat()))
    test_db.commit()

    result = admit_bundle(signed_bundle, now=now)

    assert result.admitted is False
    assert result.decision == "quarantined"
    assert result.reason == "peer_untrusted"
    assert result.stage_reached == "verify_peer_trusted"
    assert "20.0" in result.detail


# Persistence guarantees

def test_quarantine_and_audit_atomic(test_db, registered_peer, valid_bundle, valid_keypair):
    """Quarantine and audit rows must be written in same transaction."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Force a quarantine outcome (expired passport)
    bundle = {**valid_bundle}
    bundle["passport"]["expires_at"] = (now - timedelta(days=1)).isoformat()

    # Re-sign the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    assert result.decision == "quarantined"
    assert result.quarantine_id is not None
    assert result.audit_id is not None

    # Verify both rows exist
    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM federation_quarantine WHERE id = ?", (result.quarantine_id,))
    assert cursor.fetchone()[0] == 1

    cursor.execute("SELECT COUNT(*) FROM federation_audit WHERE id = ?", (result.audit_id,))
    assert cursor.fetchone()[0] == 1

    # Verify audit row references quarantine_id in metadata
    cursor.execute("SELECT metadata FROM federation_audit WHERE id = ?", (result.audit_id,))
    metadata = json.loads(cursor.fetchone()["metadata"])
    assert metadata["quarantine_id"] == result.quarantine_id


def test_audit_written_on_non_skip_decisions(test_db, registered_peer, valid_bundle, valid_keypair):
    """Every terminal decision EXCEPT 'skipped' must produce exactly one audit row.

    Per Sub-step 3.4 design §6.1: transport-dedup skip returns early before
    persistence — no audit row for replay events (would flood audit log).
    Admitted / rejected / quarantined all write audit as in 3.3.

    Each sub-case uses a distinct bundle (different canonical bytes) so transport
    dedup does not collide across sub-cases within a single test run.
    """
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    now = datetime.utcnow()

    def _sign(bundle):
        canonical = canonicalize_for_signing(bundle)
        sig = base64.b64encode(private_key.sign(canonical)).decode("ascii")
        return {**bundle, "signature": sig}

    # Sub-case 1: admitted — use valid_bundle, signed
    admitted_bundle = _sign({**valid_bundle, "bundle_id": "bundle-audit-admitted"})
    result1 = admit_bundle(admitted_bundle, now=now)
    assert result1.decision == "admitted"
    assert result1.audit_id is not None

    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM federation_audit WHERE id = ?", (result1.audit_id,))
    assert cursor.fetchone()[0] == 1

    # Sub-case 2: rejected — distinct bundle_id, no signature → signature_malformed
    bundle_no_sig = {**valid_bundle, "bundle_id": "bundle-audit-rejected"}
    # No signature attached
    result2 = admit_bundle(bundle_no_sig, now=now)
    assert result2.decision == "rejected"
    assert result2.audit_id is not None

    cursor.execute("SELECT COUNT(*) FROM federation_audit WHERE id = ?", (result2.audit_id,))
    assert cursor.fetchone()[0] == 1

    # Sub-case 3: quarantined — distinct bundle_id, expired passport
    quarantined_raw = {**valid_bundle, "bundle_id": "bundle-audit-quarantined"}
    quarantined_raw["passport"] = {**valid_bundle["passport"]}
    quarantined_raw["passport"]["expires_at"] = (now - timedelta(days=1)).isoformat()
    quarantined_bundle = _sign(quarantined_raw)
    result3 = admit_bundle(quarantined_bundle, now=now)
    assert result3.decision == "quarantined"
    assert result3.audit_id is not None

    cursor.execute("SELECT COUNT(*) FROM federation_audit WHERE id = ?", (result3.audit_id,))
    assert cursor.fetchone()[0] == 1

    # Total audit rows from the three terminal decisions: 3
    cursor.execute("SELECT COUNT(*) FROM federation_audit")
    assert cursor.fetchone()[0] == 3

    # Sub-case 4: skipped — re-send the admitted bundle identically → skipped, NO audit.
    result4 = admit_bundle(admitted_bundle, now=now)
    assert result4.decision == "skipped"
    assert result4.reason == "bundle_replay"
    assert result4.audit_id is None

    # Audit row count unchanged — skipped did not audit
    cursor.execute("SELECT COUNT(*) FROM federation_audit")
    assert cursor.fetchone()[0] == 3


def test_no_quarantine_row_on_hard_reject(test_db, registered_peer, signed_bundle):
    """Hard-reject codes should not write quarantine rows."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # Test signature_malformed (hard reject)
    bundle = {**signed_bundle}
    del bundle["signature"]
    result = admit_bundle(bundle, now=now)

    assert result.decision == "rejected"
    assert result.quarantine_id is None

    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) FROM federation_quarantine")
    assert cursor.fetchone()[0] == 0

    # But audit row should exist
    cursor.execute("SELECT COUNT(*) FROM federation_audit WHERE id = ?", (result.audit_id,))
    assert cursor.fetchone()[0] == 1


def test_infra_error_on_db_failure(test_db, registered_peer, signed_bundle):
    """DB error during persistence should return infra_error."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # Close the test db to simulate failure
    # (this is a simple approach; real code paths are covered by integration tests)
    from circus.config import settings
    original_db_path = settings.database_path
    settings.database_path = Path("/nonexistent/path/to/db.sqlite")

    try:
        result = admit_bundle(signed_bundle, now=now)

        assert result.admitted is False
        assert result.decision == "infra_error"
        assert result.reason == "infra_error"
        assert result.detail is not None
        # The failure happens during peer lookup now, not persist
        # So the test validates infra_error path exists
    finally:
        settings.database_path = original_db_path


def test_infra_error_best_effort_audit(test_db, valid_keypair, valid_bundle):
    """When primary transaction fails, best-effort audit write must still land.

    Setup: use an UNREGISTERED peer → step 1 lookup returns no row →
    admit_bundle tries to persist a quarantined decision (passport_unknown).
    Drop federation_quarantine mid-test so the primary transaction fails
    on quarantine insert. federation_audit remains intact so the best-effort
    second transaction should succeed, producing a bundle_infra_error row.
    """
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Sign the bundle so verify_signature would pass if we got that far
    canonical_bytes = canonicalize_for_signing(valid_bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle = {**valid_bundle, "signature": base64.b64encode(signature_bytes).decode("ascii")}

    # Break quarantine persistence: drop the table
    cursor = test_db.cursor()
    cursor.execute("DROP TABLE federation_quarantine")
    test_db.commit()

    # Peer is NOT registered → step 1 decides "quarantined / passport_unknown"
    # → persist tries to INSERT into federation_quarantine → fails → infra_error
    # → best-effort audit write into federation_audit → should succeed
    result = admit_bundle(bundle, now=now)

    assert result.decision == "infra_error"
    assert result.reason == "infra_error"
    assert result.stage_reached == "peer_lookup"
    assert result.metadata.get("original_decision_intent") == "quarantined"
    assert result.detail is not None and "db error" in result.detail.lower()

    # Verify best-effort audit row landed in federation_audit
    cursor.execute(
        "SELECT id, action, reason, metadata FROM federation_audit "
        "WHERE action = 'bundle_infra_error' AND target_id = ?",
        (bundle["bundle_id"],),
    )
    row = cursor.fetchone()
    assert row is not None, "best-effort audit row was not written"
    assert row["reason"] == "infra_error"
    audit_meta = json.loads(row["metadata"])
    assert audit_meta["original_decision_intent"] == "quarantined"
    assert audit_meta["stage_reached"] == "peer_lookup"
    assert "db_error" in audit_meta


def test_infra_error_best_effort_audit_also_fails_gracefully(test_db, valid_keypair, valid_bundle):
    """When BOTH primary AND best-effort audit fail, admit_bundle must not raise."""
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    canonical_bytes = canonicalize_for_signing(valid_bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle = {**valid_bundle, "signature": base64.b64encode(signature_bytes).decode("ascii")}

    # Break BOTH persistence targets
    cursor = test_db.cursor()
    cursor.execute("DROP TABLE federation_quarantine")
    cursor.execute("DROP TABLE federation_audit")
    test_db.commit()

    # Must return infra_error gracefully — no raise
    result = admit_bundle(bundle, now=now)

    assert result.decision == "infra_error"
    assert result.reason == "infra_error"
    # audit_id is still generated even though write failed silently
    assert result.audit_id is not None


# Ordering guarantees

def test_fixed_order_signature_before_passport(test_db, registered_peer, valid_bundle):
    """Bundle with BOTH bad signature AND broken passport should fail on signature first."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    # Add invalid signature AND break passport
    bundle = {**valid_bundle}
    bundle["signature"] = base64.b64encode(b"invalid" * 8).decode('ascii')
    bundle["passport"] = None  # Broken passport

    result = admit_bundle(bundle, now=now)

    # Must fail on signature (earlier stage), not passport
    assert result.decision == "rejected"
    assert result.reason == "signature_invalid"
    assert result.stage_reached == "verify_signature"
    # If passport check ran first, reason would be passport_malformed


def test_fixed_order_passport_structure_before_expiry(test_db, registered_peer, valid_bundle, valid_keypair):
    """Malformed + expired passport should fail on structure check first."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Create passport that is BOTH malformed (missing identity) AND expired
    bundle = {**valid_bundle}
    bundle["passport"] = {
        "score": {"total": 5.0},
        "expires_at": (now - timedelta(days=1)).isoformat(),
        # Missing "identity" field
    }

    # Re-sign the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    # Must fail on structure check (earlier), not expiry
    assert result.decision == "quarantined"
    assert result.reason == "passport_malformed"
    assert result.stage_reached == "verify_passport_structure"


def test_fixed_order_peer_known_before_trusted(test_db, valid_keypair, signed_bundle):
    """Inactive (unknown) peer with low trust should fail on peer_known first."""
    _, public_key_bytes = valid_keypair
    peer_id = "peer-test-001"
    now = datetime.utcnow()

    # Register peer as inactive with low trust score
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_peers (
            id, name, url, public_key, trust_score, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (peer_id, "Inactive Untrusted", "https://bad.example.com",
          public_key_bytes, 10.0, 0, now.isoformat()))
    test_db.commit()

    result = admit_bundle(signed_bundle, now=now)

    # Must fail on peer_known (step 1 peer lookup + step 5), not peer_trusted
    # peer_lookup happens first and catches inactive
    assert result.decision == "quarantined"
    assert result.reason == "passport_unknown"
    assert result.stage_reached == "peer_lookup"


# Shape assertions

def test_admission_result_is_frozen():
    """AdmissionResult should be immutable (frozen dataclass)."""
    result = AdmissionResult(
        admitted=True,
        decision="admitted",
        stage_reached="admitted",
    )

    with pytest.raises(Exception):  # FrozenInstanceError or similar
        result.admitted = False


def test_admission_result_fields_populated(test_db, registered_peer, signed_bundle):
    """bundle_id, peer_id, stage_reached should always be set when data available."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    result = admit_bundle(signed_bundle, now=now)

    assert result.bundle_id == "bundle-test-123"
    assert result.peer_id == peer_id
    assert result.stage_reached is not None
    assert result.detail is not None


# Edge cases

def test_admit_bundle_not_dict_raises_typeerror():
    """admit_bundle should raise TypeError if bundle is not a dict."""
    with pytest.raises(TypeError):
        admit_bundle("not a dict")

    with pytest.raises(TypeError):
        admit_bundle(None)


def test_passport_missing_identity_after_structure_check(test_db, registered_peer, valid_bundle, valid_keypair):
    """If passport passes structure check but has no identity.name, quarantine."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Create passport with identity dict but missing "name"
    bundle = {**valid_bundle}
    bundle["passport"]["identity"] = {}  # Has "identity" but no "name"

    # Re-sign the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    # Should fail on passport structure validation (validate_passport catches missing name)
    assert result.admitted is False
    assert result.decision == "quarantined"
    assert result.reason == "passport_malformed"
    # The detail message comes from validate_passport: "Passport identity must include name"
    assert "identity" in result.detail.lower() and "name" in result.detail.lower()


def test_quarantine_payload_includes_full_bundle(test_db, registered_peer, valid_bundle, valid_keypair):
    """Quarantine row should store full bundle including signature in payload."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Force quarantine
    bundle = {**valid_bundle}
    bundle["passport"]["expires_at"] = (now - timedelta(days=1)).isoformat()

    # Re-sign the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    # Retrieve quarantine row
    cursor = test_db.cursor()
    cursor.execute("SELECT payload FROM federation_quarantine WHERE id = ?", (result.quarantine_id,))
    payload_json = cursor.fetchone()["payload"]
    payload = json.loads(payload_json)

    # Verify full bundle stored
    assert payload["bundle_id"] == bundle["bundle_id"]
    assert payload["signature"] == bundle["signature"]
    assert payload["passport"] == bundle["passport"]
    assert payload["memories"] == bundle["memories"]


def test_quarantine_expires_at_is_7_days(test_db, registered_peer, valid_bundle, valid_keypair):
    """Quarantine row should have expires_at set to 7 days from now."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    now = datetime.utcnow()

    # Force quarantine
    bundle = {**valid_bundle}
    bundle["passport"]["expires_at"] = (now - timedelta(days=1)).isoformat()

    # Re-sign the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    result = admit_bundle(bundle, now=now)

    # Retrieve quarantine row
    cursor = test_db.cursor()
    cursor.execute("SELECT expires_at FROM federation_quarantine WHERE id = ?", (result.quarantine_id,))
    expires_at_str = cursor.fetchone()["expires_at"]
    expires_at = datetime.fromisoformat(expires_at_str)

    # Should be ~7 days from now (allow 1 second tolerance)
    expected_expires = now + timedelta(days=7)
    delta = abs((expires_at - expected_expires).total_seconds())
    assert delta < 1.0


def test_audit_metadata_includes_stage_and_detail(test_db, registered_peer, signed_bundle):
    """Audit row metadata should include stage_reached and detail."""
    peer_id, _ = registered_peer
    now = datetime.utcnow()

    result = admit_bundle(signed_bundle, now=now)

    cursor = test_db.cursor()
    cursor.execute("SELECT metadata FROM federation_audit WHERE id = ?", (result.audit_id,))
    metadata = json.loads(cursor.fetchone()["metadata"])

    assert metadata["stage_reached"] == "admitted"
    assert metadata["detail"] == "all verifications passed"
