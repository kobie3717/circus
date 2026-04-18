"""Tests for federation verification pipeline (Step 3.2)."""

import base64
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from circus.database import init_database, run_v2_migration, run_v3_migration
from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.federation_verify import (
    VerificationResult,
    verify_passport_expiry,
    verify_passport_structure,
    verify_peer_known,
    verify_peer_trusted,
    verify_signature,
)
from circus.services.signing import generate_keypair
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


# Test fixtures

@pytest.fixture
def test_db():
    """Create temporary database for testing with federation_peers table."""
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
def valid_bundle():
    """Create a valid bundle for signing tests."""
    return {
        "bundle_id": "bundle-test123",
        "peer_id": "peer-test456",
        "memories": [
            {
                "id": "shmem-xyz",
                "content": "Test memory",
                "category": "testing",
                "domain": "test-domain",
                "tags": ["test"],
                "provenance": {
                    "hop_count": 1,
                    "original_author": "test-agent",
                    "original_timestamp": "2026-04-18T10:00:00Z",
                    "confidence": 0.9
                },
                "privacy_tier": "public",
                "shared_at": "2026-04-18T10:05:00Z"
            }
        ],
        "timestamp": "2026-04-18T10:05:30Z"
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
    return bundle_with_sig, public_key_bytes


@pytest.fixture
def valid_passport():
    """Create a valid passport."""
    return {
        "identity": {
            "name": "Test Agent",
            "instance": "test-circus"
        },
        "score": {
            "total": 7.5
        },
        "predictions": {
            "confirmed": 10,
            "refuted": 2
        },
        "beliefs": {
            "total": 50,
            "contradictions": 1
        },
        "memory_stats": {
            "proof_count_avg": 3.2,
            "graph_connections": 15
        },
        "generated_at": datetime.utcnow().isoformat()
    }


# Test verify_signature

def test_verify_signature_valid(signed_bundle):
    """Valid signature on canonicalized bundle → valid=True."""
    bundle, public_key = signed_bundle
    result = verify_signature(bundle, public_key)

    assert result.valid is True
    assert result.reason is None
    assert result.detail == "signature valid"


def test_verify_signature_tampered_bundle(signed_bundle):
    """Signature over different bytes (bundle tampered) → signature_invalid."""
    bundle, public_key = signed_bundle

    # Tamper with bundle content after signing
    bundle["memories"][0]["content"] = "TAMPERED CONTENT"

    result = verify_signature(bundle, public_key)

    assert result.valid is False
    assert result.reason == "signature_invalid"
    assert "verification failed" in result.detail.lower()


def test_verify_signature_corrupted_signature(signed_bundle):
    """Signature bytes corrupted → signature_malformed or signature_invalid."""
    bundle, public_key = signed_bundle

    # Corrupt signature by changing one character
    bundle["signature"] = bundle["signature"][:-5] + "XXXXX"

    result = verify_signature(bundle, public_key)

    assert result.valid is False
    # Could be malformed (if base64 decode fails) or invalid (if decode succeeds but verify fails)
    assert result.reason in ("signature_malformed", "signature_invalid")


def test_verify_signature_wrong_public_key(signed_bundle):
    """Public key doesn't match signer → signature_invalid."""
    bundle, _ = signed_bundle

    # Generate different keypair
    _, wrong_public_key = generate_keypair()

    result = verify_signature(bundle, wrong_public_key)

    assert result.valid is False
    assert result.reason == "signature_invalid"


def test_verify_signature_missing_field(valid_bundle, valid_keypair):
    """Missing signature field → signature_malformed."""
    _, public_key = valid_keypair

    # Don't add signature
    result = verify_signature(valid_bundle, public_key)

    assert result.valid is False
    assert result.reason == "signature_malformed"
    assert "missing" in result.detail


def test_verify_signature_wrong_type(valid_bundle, valid_keypair):
    """Signature field wrong type → signature_malformed."""
    _, public_key = valid_keypair

    valid_bundle["signature"] = 12345  # Not a string

    result = verify_signature(valid_bundle, public_key)

    assert result.valid is False
    assert result.reason == "signature_malformed"
    assert "wrong type" in result.detail


def test_verify_signature_invalid_base64(valid_bundle, valid_keypair):
    """Signature not valid base64 → signature_malformed."""
    _, public_key = valid_keypair

    valid_bundle["signature"] = "not-valid-base64!@#$%"

    result = verify_signature(valid_bundle, public_key)

    assert result.valid is False
    assert result.reason == "signature_malformed"
    assert "base64 decode failed" in result.detail


# Test verify_passport_structure

def test_verify_passport_structure_valid(valid_passport):
    """Valid passport → valid=True."""
    result = verify_passport_structure(valid_passport)

    assert result.valid is True
    assert result.reason is None
    assert "valid" in result.detail


def test_verify_passport_structure_missing_identity():
    """Missing required field 'identity' → passport_malformed."""
    passport = {
        "score": {"total": 5.0},
        "predictions": {},
        "beliefs": {}
    }

    result = verify_passport_structure(passport)

    assert result.valid is False
    assert result.reason == "passport_malformed"
    assert "identity" in result.detail.lower()


def test_verify_passport_structure_missing_name():
    """Missing 'name' in identity → passport_malformed."""
    passport = {
        "identity": {},  # No 'name' field
        "score": {"total": 5.0}
    }

    result = verify_passport_structure(passport)

    assert result.valid is False
    assert result.reason == "passport_malformed"
    assert "name" in result.detail.lower()


def test_verify_passport_structure_missing_score():
    """Missing required field 'score' → passport_malformed."""
    passport = {
        "identity": {"name": "Test Agent"},
        # No 'score' field
    }

    result = verify_passport_structure(passport)

    assert result.valid is False
    assert result.reason == "passport_malformed"
    assert "score" in result.detail.lower()


def test_verify_passport_structure_extra_fields(valid_passport):
    """Extra fields present but required OK → valid=True (forward-compat)."""
    passport = {**valid_passport, "extra_field": "should be ignored"}

    result = verify_passport_structure(passport)

    assert result.valid is True


# Test verify_passport_expiry

def test_verify_passport_expiry_valid_window():
    """Passport within valid window → valid=True."""
    now = datetime.utcnow()
    passport = {
        "identity": {"name": "Test"},
        "score": {"total": 5.0},
        "generated_at": (now - timedelta(days=10)).isoformat(),
        "expires_at": (now + timedelta(days=20)).isoformat()
    }

    result = verify_passport_expiry(passport, now=now)

    assert result.valid is True
    assert result.reason is None


def test_verify_passport_expiry_expired():
    """Passport past expiry (now > expires_at) → passport_expired."""
    now = datetime.utcnow()
    passport = {
        "identity": {"name": "Test"},
        "score": {"total": 5.0},
        "generated_at": (now - timedelta(days=60)).isoformat(),
        "expires_at": (now - timedelta(days=1)).isoformat()  # Expired yesterday
    }

    result = verify_passport_expiry(passport, now=now)

    assert result.valid is False
    assert result.reason == "passport_expired"
    assert "expired" in result.detail


def test_verify_passport_expiry_not_yet_valid():
    """Passport before not_before (now < not_before) → passport_expired."""
    now = datetime.utcnow()
    passport = {
        "identity": {"name": "Test"},
        "score": {"total": 5.0},
        "not_before": (now + timedelta(days=1)).isoformat(),  # Valid tomorrow
        "expires_at": (now + timedelta(days=30)).isoformat()
    }

    result = verify_passport_expiry(passport, now=now)

    assert result.valid is False
    assert result.reason == "passport_expired"
    assert "not valid until" in result.detail


def test_verify_passport_expiry_no_expiry():
    """Passport with no expiry field → valid=True (forward-compat)."""
    passport = {
        "identity": {"name": "Test"},
        "score": {"total": 5.0},
        "generated_at": datetime.utcnow().isoformat()
        # No expires_at or not_before
    }

    result = verify_passport_expiry(passport)

    assert result.valid is True


def test_verify_passport_expiry_stale_generated_at():
    """Passport generated > 90 days ago (no explicit expiry) → passport_expired."""
    now = datetime.utcnow()
    passport = {
        "identity": {"name": "Test"},
        "score": {"total": 5.0},
        "generated_at": (now - timedelta(days=100)).isoformat()
        # No explicit expires_at
    }

    result = verify_passport_expiry(passport, now=now)

    assert result.valid is False
    assert result.reason == "passport_expired"
    assert "generated" in result.detail
    assert "100" in result.detail


def test_verify_passport_expiry_fresh_generated_at():
    """Passport generated recently (< 30 days, no expiry) → valid=True."""
    now = datetime.utcnow()
    passport = {
        "identity": {"name": "Test"},
        "score": {"total": 5.0},
        "generated_at": (now - timedelta(days=10)).isoformat()
    }

    result = verify_passport_expiry(passport, now=now)

    assert result.valid is True


# Test verify_peer_known

def test_verify_peer_known_found(test_db):
    """Peer registered in federation_peers → valid=True."""
    conn = test_db
    cursor = conn.cursor()

    # Insert test peer
    cursor.execute("""
        INSERT INTO federation_peers (id, name, url, public_key, trust_score, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("peer-test123", "Test Peer", "https://test.circus", b"fake_key_32bytes" * 2, 50.0, 1, datetime.utcnow().isoformat()))
    conn.commit()

    result = verify_peer_known("peer-test123")

    assert result.valid is True
    assert result.peer_id == "peer-test123"
    assert "registered and active" in result.detail


def test_verify_peer_known_not_found(test_db):
    """Peer not in federation_peers → passport_unknown."""
    result = verify_peer_known("peer-nonexistent")

    assert result.valid is False
    assert result.reason == "passport_unknown"
    assert "not found" in result.detail


def test_verify_peer_known_inactive(test_db):
    """Peer exists but is_active=0 → passport_unknown."""
    conn = test_db
    cursor = conn.cursor()

    # Insert inactive peer
    cursor.execute("""
        INSERT INTO federation_peers (id, name, url, public_key, trust_score, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("peer-inactive", "Inactive Peer", "https://test.circus", b"fake_key", 50.0, 0, datetime.utcnow().isoformat()))
    conn.commit()

    result = verify_peer_known("peer-inactive")

    assert result.valid is False
    assert result.reason == "passport_unknown"


def test_verify_peer_known_empty_db(test_db):
    """Empty federation_peers table → passport_unknown."""
    result = verify_peer_known("peer-any")

    assert result.valid is False
    assert result.reason == "passport_unknown"


# Test verify_peer_trusted

def test_verify_peer_trusted_meets_threshold(test_db):
    """Peer trust >= threshold → valid=True."""
    conn = test_db
    cursor = conn.cursor()

    # Insert peer with trust=50 (above default 30)
    cursor.execute("""
        INSERT INTO federation_peers (id, name, url, public_key, trust_score, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("peer-trusted", "Trusted Peer", "https://test.circus", b"key", 50.0, 1, datetime.utcnow().isoformat()))
    conn.commit()

    result = verify_peer_trusted("peer-trusted")

    assert result.valid is True
    assert result.peer_id == "peer-trusted"
    assert "50" in result.detail
    assert result.metadata["trust_score"] == 50.0


def test_verify_peer_trusted_below_threshold(test_db):
    """Peer trust < threshold → peer_untrusted."""
    conn = test_db
    cursor = conn.cursor()

    # Insert peer with trust=20 (below default 30)
    cursor.execute("""
        INSERT INTO federation_peers (id, name, url, public_key, trust_score, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("peer-untrusted", "Untrusted Peer", "https://test.circus", b"key", 20.0, 1, datetime.utcnow().isoformat()))
    conn.commit()

    result = verify_peer_trusted("peer-untrusted")

    assert result.valid is False
    assert result.reason == "peer_untrusted"
    assert "20" in result.detail
    assert "30" in result.detail  # Default threshold
    assert result.metadata["trust_score"] == 20.0


def test_verify_peer_trusted_custom_threshold(test_db):
    """Custom min_trust threshold works."""
    conn = test_db
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO federation_peers (id, name, url, public_key, trust_score, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("peer-custom", "Custom Peer", "https://test.circus", b"key", 45.0, 1, datetime.utcnow().isoformat()))
    conn.commit()

    # Require trust >= 50
    result = verify_peer_trusted("peer-custom", min_trust=50.0)

    assert result.valid is False
    assert result.reason == "peer_untrusted"
    assert result.metadata["min_trust"] == 50.0


def test_verify_peer_trusted_exact_threshold(test_db):
    """Peer trust exactly equals threshold → valid=True."""
    conn = test_db
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO federation_peers (id, name, url, public_key, trust_score, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ("peer-exact", "Exact Peer", "https://test.circus", b"key", 30.0, 1, datetime.utcnow().isoformat()))
    conn.commit()

    result = verify_peer_trusted("peer-exact", min_trust=30.0)

    assert result.valid is True


def test_verify_peer_trusted_not_found(test_db):
    """Peer not found → peer_untrusted (fail closed)."""
    result = verify_peer_trusted("peer-missing")

    assert result.valid is False
    assert result.reason == "peer_untrusted"
    assert "not found" in result.detail


# Test composition (golden path)

def test_composition_all_stages_pass(test_db, signed_bundle, valid_passport):
    """All 5 stages pass → all valid=True."""
    bundle, public_key = signed_bundle
    peer_id = bundle["peer_id"]

    # Insert peer into DB
    conn = test_db
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO federation_peers (id, name, url, public_key, trust_score, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (peer_id, "Test Peer", "https://test.circus", public_key, 50.0, 1, datetime.utcnow().isoformat()))
    conn.commit()

    # Stage 1: Signature
    r1 = verify_signature(bundle, public_key)
    assert r1.valid is True

    # Stage 2: Passport structure
    r2 = verify_passport_structure(valid_passport)
    assert r2.valid is True

    # Stage 3: Passport expiry
    r3 = verify_passport_expiry(valid_passport)
    assert r3.valid is True

    # Stage 4: Peer known
    r4 = verify_peer_known(peer_id)
    assert r4.valid is True

    # Stage 5: Peer trusted
    r5 = verify_peer_trusted(peer_id)
    assert r5.valid is True

    # All green — bundle would be admitted


def test_composition_stage3_fails(valid_passport):
    """Stage 3 (passport expiry) fails → reason=passport_expired."""
    # Expired passport
    passport = {
        **valid_passport,
        "expires_at": (datetime.utcnow() - timedelta(days=1)).isoformat()
    }

    r3 = verify_passport_expiry(passport)

    assert r3.valid is False
    assert r3.reason == "passport_expired"
    # Stages 1-2 would still pass, but caller stops at stage 3 failure
