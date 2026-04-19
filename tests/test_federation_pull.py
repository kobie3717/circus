"""Tests for federation PULL endpoint (Sub-step 3.5a)."""

import base64
import json
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from circus.database import init_database, run_v2_migration, run_v3_migration, run_v4_migration, run_v5_migration
from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.federation_admission import admit_bundle
from circus.services.federation_auth import verify_pull_challenge, AuthError
from circus.services.federation_pull import (
    encode_cursor, decode_cursor, CursorError,
    get_cached_passport, build_outgoing_bundle, pull_bundles
)
from circus.services.instance_identity import ensure_instance_keypair
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
    run_v5_migration(db_path)

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
def sample_memory(test_db):
    """Create a sample memory in shared_memories."""
    now = datetime.utcnow().isoformat()
    memory_id = "shmem-test-001"

    cursor = test_db.cursor()

    # Create room first
    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'circus-system', 1, ?)
    """, (now,))

    # Create memory
    cursor.execute("""
        INSERT INTO shared_memories (
            id, room_id, from_agent_id, content, category, domain,
            tags, provenance, privacy_tier, shared_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        memory_id,
        "room-test",
        "agent-test",
        "Test memory content",
        "testing",
        "test-domain",
        json.dumps(["test", "sample"]),
        json.dumps({
            "hop_count": 1,
            "original_author": "peer-other-001",
            "original_timestamp": now,
            "confidence": 0.9,
        }),
        "public",
        now,
    ))

    test_db.commit()
    return memory_id


# Authentication tests

def test_verify_pull_challenge_missing_peer(test_db):
    """Challenge verification fails if peer not in federation_peers."""
    peer_id = "peer-unknown"
    signature_b64 = base64.b64encode(b"fake-sig").decode('ascii')

    with pytest.raises(AuthError) as exc_info:
        verify_pull_challenge(peer_id, signature_b64)

    assert exc_info.value.status_code == 403
    assert "not registered" in str(exc_info.value).lower()


def test_verify_pull_challenge_inactive_peer(test_db, valid_keypair):
    """Challenge verification fails if peer is inactive."""
    _, public_key_bytes = valid_keypair
    peer_id = "peer-inactive"
    now = datetime.utcnow().isoformat()

    # Register inactive peer
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_peers (
            id, name, url, public_key, trust_score, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (peer_id, "Inactive Peer", "https://inactive.example.com",
          public_key_bytes, 50.0, 0, now))
    test_db.commit()

    signature_b64 = base64.b64encode(b"fake-sig").decode('ascii')

    with pytest.raises(AuthError) as exc_info:
        verify_pull_challenge(peer_id, signature_b64)

    assert exc_info.value.status_code == 403
    assert "inactive" in str(exc_info.value).lower()


def test_verify_pull_challenge_invalid_signature(test_db, registered_peer, valid_keypair):
    """Challenge verification fails with wrong signature."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair

    # Sign with wrong key
    wrong_private_key = Ed25519PrivateKey.generate()
    bucket = int(time.time() // 60)
    challenge = f"pull:{peer_id}:{bucket}"
    signature_bytes = wrong_private_key.sign(challenge.encode('utf-8'))
    signature_b64 = base64.b64encode(signature_bytes).decode('ascii')

    with pytest.raises(AuthError) as exc_info:
        verify_pull_challenge(peer_id, signature_b64)

    assert exc_info.value.status_code == 401
    assert "invalid" in str(exc_info.value).lower()


def test_verify_pull_challenge_expired_timestamp(test_db, registered_peer, valid_keypair):
    """Challenge verification fails if timestamp is >1 minute old."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair

    # Sign with bucket from 3 minutes ago
    old_bucket = int(time.time() // 60) - 3
    challenge = f"pull:{peer_id}:{old_bucket}"
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(challenge.encode('utf-8'))
    signature_b64 = base64.b64encode(signature_bytes).decode('ascii')

    with pytest.raises(AuthError) as exc_info:
        verify_pull_challenge(peer_id, signature_b64)

    assert exc_info.value.status_code == 401
    assert "invalid" in str(exc_info.value).lower() or "expired" in str(exc_info.value).lower()


def test_verify_pull_challenge_valid(test_db, registered_peer, valid_keypair):
    """Valid challenge signature should verify successfully."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair

    # Sign current bucket
    bucket = int(time.time() // 60)
    challenge = f"pull:{peer_id}:{bucket}"
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(challenge.encode('utf-8'))
    signature_b64 = base64.b64encode(signature_bytes).decode('ascii')

    success, verified_peer_id = verify_pull_challenge(peer_id, signature_b64)

    assert success is True
    assert verified_peer_id == peer_id


def test_verify_pull_challenge_clock_skew_tolerance(test_db, registered_peer, valid_keypair):
    """Challenge verification accepts ±1 minute bucket skew."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)

    # Current bucket
    current_bucket = int(time.time() // 60)

    # Test -1 minute skew (sign with previous bucket, verify with current time)
    challenge_prev = f"pull:{peer_id}:{current_bucket - 1}"
    sig_prev = private_key.sign(challenge_prev.encode('utf-8'))
    sig_prev_b64 = base64.b64encode(sig_prev).decode('ascii')

    success, _ = verify_pull_challenge(peer_id, sig_prev_b64)
    assert success is True

    # Test +1 minute skew (sign with next bucket, verify with current time)
    challenge_next = f"pull:{peer_id}:{current_bucket + 1}"
    sig_next = private_key.sign(challenge_next.encode('utf-8'))
    sig_next_b64 = base64.b64encode(sig_next).decode('ascii')

    success, _ = verify_pull_challenge(peer_id, sig_next_b64)
    assert success is True


# Cursor tests

def test_encode_decode_cursor():
    """Cursor encoding/decoding round-trip should preserve data."""
    shared_at = "2026-04-19T12:00:00Z"
    memory_id = "shmem-xyz-123"

    cursor = encode_cursor(shared_at, memory_id)
    decoded_shared_at, decoded_id = decode_cursor(cursor)

    assert decoded_shared_at == shared_at
    assert decoded_id == memory_id


def test_decode_cursor_invalid_base64():
    """Malformed base64 cursor should raise CursorError."""
    with pytest.raises(CursorError):
        decode_cursor("not-valid-base64!@#")


def test_decode_cursor_missing_fields():
    """Cursor missing required fields should raise CursorError."""
    # Encode cursor with only one field
    invalid = {"shared_at": "2026-04-19T12:00:00Z"}
    cursor_json = json.dumps(invalid, sort_keys=True)
    cursor_b64 = base64.urlsafe_b64encode(cursor_json.encode('utf-8')).decode('ascii')

    with pytest.raises(CursorError):
        decode_cursor(cursor_b64)


def test_cursor_deterministic():
    """Same inputs should produce identical cursor."""
    shared_at = "2026-04-19T12:00:00Z"
    memory_id = "shmem-xyz"

    cursor1 = encode_cursor(shared_at, memory_id)
    cursor2 = encode_cursor(shared_at, memory_id)

    assert cursor1 == cursor2


# Passport caching tests

def test_get_cached_passport_generates_on_first_call(test_db):
    """First passport call should generate and cache."""
    passport = get_cached_passport(test_db)

    assert passport is not None
    assert "identity" in passport
    assert "score" in passport
    assert "generated_at" in passport

    # Verify cache written
    cursor = test_db.cursor()
    cursor.execute("SELECT value FROM instance_config WHERE key = 'passport_cache'")
    row = cursor.fetchone()
    assert row is not None


def test_get_cached_passport_reuses_valid_cache(test_db):
    """Valid cached passport should be reused without regeneration."""
    # First call generates
    passport1 = get_cached_passport(test_db)
    generated_at1 = passport1["generated_at"]

    # Immediate second call should reuse cache
    passport2 = get_cached_passport(test_db)
    generated_at2 = passport2["generated_at"]

    assert generated_at1 == generated_at2  # Same passport, not regenerated


def test_get_cached_passport_expires_after_one_hour(test_db):
    """Expired passport cache should regenerate."""
    # Generate initial passport
    passport1 = get_cached_passport(test_db)

    # Manually expire cache by setting expires_at to past
    cursor = test_db.cursor()
    past_time = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    cursor.execute("""
        UPDATE instance_config SET value = ?
        WHERE key = 'passport_expires_at'
    """, (past_time,))
    test_db.commit()

    # Next call should regenerate
    passport2 = get_cached_passport(test_db)

    assert passport2["generated_at"] != passport1["generated_at"]


# Bundle construction tests

def test_build_outgoing_bundle_signature_verifies(test_db, sample_memory):
    """Built bundle signature should verify with instance public key."""
    cursor = test_db.cursor()
    cursor.execute("SELECT * FROM shared_memories WHERE id = ?", (sample_memory,))
    memory_row = dict(cursor.fetchone())

    now = datetime.utcnow()
    bundle = build_outgoing_bundle(test_db, memory_row, now=now)

    # Verify signature
    signature_b64 = bundle["signature"]
    signature_bytes = base64.b64decode(signature_b64)

    # Get instance public key
    cursor.execute("SELECT value FROM instance_config WHERE key = 'public_key'")
    public_key_b64 = cursor.fetchone()["value"]
    public_key_bytes = base64.b64decode(public_key_b64)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)

    # Verify
    canonical_bytes = canonicalize_for_signing(bundle)
    public_key.verify(signature_bytes, canonical_bytes)  # Raises on failure


def test_build_outgoing_bundle_structure(test_db, sample_memory):
    """Built bundle should have all required fields."""
    cursor = test_db.cursor()
    cursor.execute("SELECT * FROM shared_memories WHERE id = ?", (sample_memory,))
    memory_row = dict(cursor.fetchone())

    now = datetime.utcnow()
    bundle = build_outgoing_bundle(test_db, memory_row, now=now)

    assert "bundle_id" in bundle
    assert "peer_id" in bundle
    assert "passport" in bundle
    assert "signature" in bundle
    assert "memories" in bundle
    assert "timestamp" in bundle

    assert len(bundle["memories"]) == 1
    assert bundle["memories"][0]["id"] == sample_memory


def test_build_outgoing_bundle_deterministic_bundle_id(test_db, sample_memory):
    """Same memory should produce same bundle_id with same timestamp."""
    cursor = test_db.cursor()
    cursor.execute("SELECT * FROM shared_memories WHERE id = ?", (sample_memory,))
    memory_row = dict(cursor.fetchone())

    # Use same timestamp for both
    now = datetime.utcnow()
    bundle1 = build_outgoing_bundle(test_db, memory_row, now=now)
    bundle2 = build_outgoing_bundle(test_db, memory_row, now=now)

    # bundle_id should be identical (deterministic) when timestamp is same
    assert bundle1["bundle_id"] == bundle2["bundle_id"]


# Pull query tests

def test_pull_bundles_empty_db(test_db, registered_peer):
    """Pull from empty DB should return empty array."""
    peer_id, _ = registered_peer

    bundles, next_cursor, has_more = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=None,
        limit=50
    )

    assert bundles == []
    assert next_cursor is None
    assert has_more is False


def test_pull_bundles_single_memory(test_db, registered_peer, sample_memory):
    """Pull with one memory should return one bundle."""
    peer_id, _ = registered_peer

    bundles, next_cursor, has_more = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=None,
        limit=50
    )

    assert len(bundles) == 1
    assert bundles[0]["memories"][0]["id"] == sample_memory
    assert next_cursor is not None  # Cursor present even at end
    assert has_more is False  # No more results


def test_pull_bundles_pagination(test_db, registered_peer):
    """Pagination with limit should return correct pages."""
    peer_id, _ = registered_peer

    # Create 3 memories
    cursor = test_db.cursor()
    now = datetime.utcnow()

    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'circus-system', 1, ?)
    """, (now.isoformat(),))

    for i in range(3):
        memory_time = (now + timedelta(seconds=i)).isoformat()
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, domain,
                tags, provenance, privacy_tier, shared_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"shmem-page-{i}",
            "room-test",
            "agent-test",
            f"Memory {i}",
            "testing",
            "test-domain",
            json.dumps([]),
            json.dumps({"original_author": "peer-other"}),
            "public",
            memory_time,
        ))
    test_db.commit()

    # First page: limit=2
    bundles1, cursor1, has_more1 = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=None,
        limit=2
    )

    assert len(bundles1) == 2
    assert has_more1 is True
    assert cursor1 is not None

    # Second page: since=cursor1
    bundles2, cursor2, has_more2 = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=cursor1,
        limit=2
    )

    assert len(bundles2) == 1  # Only 1 remaining
    assert has_more2 is False
    assert cursor2 is not None


def test_pull_bundles_cursor_exclusive(test_db, registered_peer):
    """Cursor pagination should be exclusive (not include cursor item)."""
    peer_id, _ = registered_peer

    # Create 2 memories
    cursor = test_db.cursor()
    now = datetime.utcnow()

    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'circus-system', 1, ?)
    """, (now.isoformat(),))

    for i in range(2):
        memory_time = (now + timedelta(seconds=i)).isoformat()
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, domain,
                tags, provenance, privacy_tier, shared_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"shmem-excl-{i}",
            "room-test",
            "agent-test",
            f"Memory {i}",
            "testing",
            "test-domain",
            json.dumps([]),
            json.dumps({"original_author": "peer-other"}),
            "public",
            memory_time,
        ))
    test_db.commit()

    # Get first item
    bundles1, _, _ = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=None,
        limit=1
    )

    first_memory = bundles1[0]["memories"][0]
    first_cursor = encode_cursor(first_memory["shared_at"], first_memory["id"])

    # Pull with cursor — should NOT include first item
    bundles2, _, _ = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=first_cursor,
        limit=10
    )

    assert len(bundles2) == 1
    assert bundles2[0]["memories"][0]["id"] == "shmem-excl-1"


def test_pull_bundles_domain_filter(test_db, registered_peer):
    """Domain filter should narrow results to matching domain only."""
    peer_id, _ = registered_peer

    # Create memories with different domains
    cursor = test_db.cursor()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'circus-system', 1, ?)
    """, (now,))

    domains = ["domain-a", "domain-a", "domain-b"]
    for i, domain in enumerate(domains):
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, domain,
                tags, provenance, privacy_tier, shared_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"shmem-domain-{i}",
            "room-test",
            "agent-test",
            f"Memory {i}",
            "testing",
            domain,
            json.dumps([]),
            json.dumps({"original_author": "peer-other"}),
            "public",
            now,
        ))
    test_db.commit()

    # Pull with domain filter
    bundles, _, _ = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=None,
        limit=50,
        domain="domain-a"
    )

    assert len(bundles) == 2
    for bundle in bundles:
        assert bundle["memories"][0]["domain"] == "domain-a"


def test_pull_bundles_privacy_tier_filter(test_db, registered_peer):
    """Only public memories should be returned (team/private excluded)."""
    peer_id, _ = registered_peer

    # Create memories with different privacy tiers
    cursor = test_db.cursor()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'circus-system', 1, ?)
    """, (now,))

    tiers = ["public", "public", "team", "private"]
    for i, tier in enumerate(tiers):
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, domain,
                tags, provenance, privacy_tier, shared_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"shmem-privacy-{i}",
            "room-test",
            "agent-test",
            f"Memory {i}",
            "testing",
            "test-domain",
            json.dumps([]),
            json.dumps({"original_author": "peer-other"}),
            tier,
            now,
        ))
    test_db.commit()

    # Pull without domain filter
    bundles, _, _ = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=None,
        limit=50
    )

    # Only 2 public memories should be returned
    assert len(bundles) == 2
    for bundle in bundles:
        assert bundle["memories"][0]["privacy_tier"] == "public"


def test_pull_bundles_boomerang_prevention(test_db, registered_peer):
    """Memories authored by puller should not be returned."""
    peer_id, _ = registered_peer

    # Create memory authored by the puller
    cursor = test_db.cursor()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'circus-system', 1, ?)
    """, (now,))

    cursor.execute("""
        INSERT INTO shared_memories (
            id, room_id, from_agent_id, content, category, domain,
            tags, provenance, privacy_tier, shared_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "shmem-boomerang",
        "room-test",
        "agent-test",
        "Boomerang memory",
        "testing",
        "test-domain",
        json.dumps([]),
        json.dumps({"original_author": peer_id}),  # Authored by puller
        "public",
        now,
    ))
    test_db.commit()

    # Pull — memory should NOT be returned
    bundles, _, _ = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=None,
        limit=50
    )

    assert len(bundles) == 0


def test_pull_bundles_null_provenance_not_boomerang(test_db, registered_peer):
    """Memories with NULL provenance should still be emitted."""
    peer_id, _ = registered_peer

    # Create memory with NULL provenance
    cursor = test_db.cursor()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'circus-system', 1, ?)
    """, (now,))

    cursor.execute("""
        INSERT INTO shared_memories (
            id, room_id, from_agent_id, content, category, domain,
            tags, provenance, privacy_tier, shared_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "shmem-null-prov",
        "room-test",
        "agent-test",
        "No provenance",
        "testing",
        "test-domain",
        json.dumps([]),
        None,  # NULL provenance
        "public",
        now,
    ))
    test_db.commit()

    # Pull — memory should be returned
    bundles, _, _ = pull_bundles(
        test_db,
        puller_peer_id=peer_id,
        since_cursor=None,
        limit=50
    )

    assert len(bundles) == 1


# End-to-end admission test

def test_pulled_bundle_passes_admission(test_db, registered_peer, sample_memory):
    """Critical test: pulled bundle should pass admit_bundle() on receiver.

    This validates the contract: bundles emitted by PULL are fully valid
    for admission on the receiving side.
    """
    peer_id, public_key_bytes = registered_peer

    # Pull a bundle
    bundles, _, _ = pull_bundles(
        test_db,
        puller_peer_id="peer-other",  # Different peer pulling
        since_cursor=None,
        limit=1
    )

    assert len(bundles) == 1
    bundle = bundles[0]

    # Update federation_peers with the emitter's public key
    # (In real federation, the receiver would have registered the emitter)
    cursor = test_db.cursor()

    # Get instance identity
    cursor.execute("SELECT value FROM instance_config WHERE key = 'instance_id'")
    instance_id = cursor.fetchone()["value"]

    cursor.execute("SELECT value FROM instance_config WHERE key = 'public_key'")
    instance_public_key_b64 = cursor.fetchone()["value"]
    instance_public_key = base64.b64decode(instance_public_key_b64)

    # Register the emitter as a peer
    cursor.execute("""
        INSERT OR REPLACE INTO federation_peers (
            id, name, url, public_key, trust_score, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        instance_id,
        "Local Instance",
        "https://local.example.com",
        instance_public_key,
        60.0,  # Established tier
        1,
        datetime.utcnow().isoformat(),
    ))
    test_db.commit()

    # Admit the bundle
    result = admit_bundle(bundle, now=datetime.utcnow())

    # Bundle should be admitted successfully
    assert result.admitted is True
    assert result.decision == "admitted"
    assert result.stage_reached == "admitted"
