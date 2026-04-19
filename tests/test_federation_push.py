"""Tests for federation PUSH endpoint (Sub-step 3.5b)."""

import base64
import hashlib
import json
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from circus.database import init_database, run_v2_migration, run_v3_migration, run_v4_migration, run_v5_migration, run_v6_migration
from circus.app import app
from circus.services.bundle_signing import canonicalize_for_signing
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
    run_v6_migration(db_path)

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
def client(test_db):
    """FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def valid_keypair():
    """Generate Ed25519 keypair for tests."""
    return generate_keypair()


@pytest.fixture
def registered_peer(test_db, valid_keypair):
    """Register a test peer in federation_peers."""
    _, public_key_bytes = valid_keypair
    peer_id = "peer-test-push-001"
    now = datetime.utcnow().isoformat()

    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_peers (
            id, name, url, public_key, trust_score, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        peer_id,
        "Test Push Peer",
        "https://test-push-peer.example.com",
        public_key_bytes,
        50.0,  # Established tier
        1,
        now,
    ))
    test_db.commit()

    return peer_id, public_key_bytes


def sign_push_challenge(peer_id: str, private_key_bytes: bytes, bucket: int) -> str:
    """Sign push challenge string."""
    challenge = f"push:{peer_id}:{bucket}"
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(challenge.encode('utf-8'))
    return base64.b64encode(signature_bytes).decode('ascii')


def build_test_bundle(peer_id: str, private_key_bytes: bytes, memories: list) -> dict:
    """Build a signed bundle for testing.

    Follows the same logic as build_outgoing_bundle in federation_pull.py:
    1. Build bundle without bundle_id or signature
    2. Derive bundle_id from SHA256 of canonical bytes
    3. Add bundle_id to bundle
    4. Sign bundle WITH bundle_id
    """
    now = datetime.utcnow()
    passport = {
        "identity": {
            "name": peer_id,  # Must match peer_id
            "role": "agent",
        },
        "score": {
            "total": 7.5,
        },
        "generated_at": now.isoformat(),
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.2, "graph_connections": 15},
    }

    bundle = {
        "peer_id": peer_id,
        "passport": passport,
        "memories": memories,
        "timestamp": now.isoformat(),
    }

    # Derive bundle_id from SHA256 of canonical bytes (WITHOUT bundle_id)
    canonical_bytes = canonicalize_for_signing(bundle)
    bundle_id = hashlib.sha256(canonical_bytes).hexdigest()[:16]
    bundle["bundle_id"] = bundle_id

    # Sign bundle (WITH bundle_id)
    canonical_bytes_with_id = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes_with_id)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    return bundle


# Happy path tests

def test_push_single_memory_admitted(client, test_db, registered_peer, valid_keypair):
    """Valid auth + single new memory → 200 admitted with counters."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    # Build bundle with one memory
    memory_id = "shmem-push-test-001"
    now = datetime.utcnow()
    memory = {
        "id": memory_id,
        "content": "Test memory from push",
        "category": "testing",
        "domain": "test-domain",
        "tags": ["push", "test"],
        "provenance": {
            "hop_count": 1,
            "original_author": peer_id,
            "original_timestamp": now.isoformat(),
            "confidence": 0.9,
        },
        "privacy_tier": "public",
        "shared_at": now.isoformat(),
    }

    bundle = build_test_bundle(peer_id, private_key_bytes, [memory])
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    # Push
    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "admitted"
    assert data["bundle_id"] == bundle["bundle_id"]
    assert "audit_id" in data
    assert data["memories_total"] == 1
    assert data["memories_new"] == 1
    assert data["memories_skipped"] == 0


def test_push_bundle_skipped_replay(client, test_db, registered_peer, valid_keypair):
    """Push twice → second skipped (transport dedup)."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    memory = {
        "id": "shmem-replay-test",
        "content": "Replay test",
        "category": "testing",
        "domain": "test-domain",
        "tags": ["replay"],
        "provenance": {
            "hop_count": 1,
            "original_author": peer_id,
            "original_timestamp": datetime.utcnow().isoformat(),
            "confidence": 0.9,
        },
        "privacy_tier": "public",
    }

    bundle = build_test_bundle(peer_id, private_key_bytes, [memory])
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    headers = {
        "X-Peer-Id": peer_id,
        "X-Peer-Signature": signature,
    }

    # First push — admitted
    response1 = client.post("/api/v1/federation/push", json=bundle, headers=headers)
    assert response1.status_code == 200
    data1 = response1.json()
    assert data1["decision"] == "admitted"

    # Second push — skipped
    response2 = client.post("/api/v1/federation/push", json=bundle, headers=headers)
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["decision"] == "skipped"
    assert data2["reason"] == "bundle_replay"


def test_push_mixed_bundle(client, test_db, registered_peer, valid_keypair):
    """3 memories (1 new, 2 seen) → admitted with memories_new/skipped counters."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    now = datetime.utcnow().isoformat()

    # Pre-seed 2 memories
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'circus-system', 1, ?)
    """, (now,))

    for i in [1, 2]:
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, domain,
                tags, provenance, privacy_tier, shared_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"shmem-existing-{i}",
            "room-test",
            peer_id,
            f"Existing memory {i}",
            "testing",
            "test-domain",
            json.dumps(["test"]),
            json.dumps({
                "hop_count": 1,
                "original_author": peer_id,
                "original_timestamp": now,
                "confidence": 0.9,
            }),
            "public",
            now,
        ))

    test_db.commit()

    # Build bundle with 3 memories: 2 existing, 1 new
    memories = [
        {
            "id": "shmem-existing-1",
            "content": "Existing memory 1",
            "category": "testing",
            "domain": "test-domain",
            "tags": ["test"],
            "provenance": {
                "hop_count": 1,
                "original_author": peer_id,
                "original_timestamp": now,
                "confidence": 0.9,
            },
            "privacy_tier": "public",
        },
        {
            "id": "shmem-existing-2",
            "content": "Existing memory 2",
            "category": "testing",
            "domain": "test-domain",
            "tags": ["test"],
            "provenance": {
                "hop_count": 1,
                "original_author": peer_id,
                "original_timestamp": now,
                "confidence": 0.9,
            },
            "privacy_tier": "public",
        },
        {
            "id": "shmem-new-mixed",
            "content": "New memory",
            "category": "testing",
            "domain": "test-domain",
            "tags": ["new"],
            "provenance": {
                "hop_count": 1,
                "original_author": peer_id,
                "original_timestamp": now,
                "confidence": 0.9,
            },
            "privacy_tier": "public",
        },
    ]

    bundle = build_test_bundle(peer_id, private_key_bytes, memories)
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "admitted"
    assert data["memories_total"] == 3
    assert data["memories_new"] == 1
    assert data["memories_skipped"] == 2


# Auth tests

def test_push_missing_peer_id_header(client, test_db, registered_peer, valid_keypair):
    """Missing X-Peer-Id header → 401."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    bundle = build_test_bundle(peer_id, private_key_bytes, [])
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    # No X-Peer-Id header
    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 401
    assert "Missing X-Peer-Id header" in response.json()["detail"]


def test_push_missing_peer_signature_header(client, test_db, registered_peer, valid_keypair):
    """Missing X-Peer-Signature header → 401."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair

    bundle = build_test_bundle(peer_id, private_key_bytes, [])

    # No X-Peer-Signature header
    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
        }
    )

    assert response.status_code == 401
    assert "Missing X-Peer-Signature header" in response.json()["detail"]


def test_push_peer_id_header_body_mismatch(client, test_db, registered_peer, valid_keypair):
    """peer_id mismatch between header and body → 401 (auth-shape failure, not 400)."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    bundle = build_test_bundle(peer_id, private_key_bytes, [])
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    # Header has different peer_id
    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": "peer-different",
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 401
    assert "mismatch" in response.json()["detail"].lower()


def test_push_invalid_signature(client, test_db, registered_peer, valid_keypair):
    """Invalid signature → 401."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair

    bundle = build_test_bundle(peer_id, private_key_bytes, [])
    bad_signature = base64.b64encode(b"invalid-signature-bytes").decode('ascii')

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": bad_signature,
        }
    )

    assert response.status_code == 401


def test_push_peer_not_registered(client, test_db, valid_keypair):
    """Peer not in federation_peers → 403."""
    private_key_bytes, _ = valid_keypair
    peer_id = "peer-unknown"
    bucket = int(time.time() / 60)

    bundle = build_test_bundle(peer_id, private_key_bytes, [])
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 403
    assert "not registered" in response.json()["detail"].lower()


def test_push_peer_inactive(client, test_db, valid_keypair):
    """Peer inactive → 403."""
    private_key_bytes, public_key_bytes = valid_keypair
    peer_id = "peer-inactive"
    now = datetime.utcnow().isoformat()

    # Register inactive peer
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_peers (
            id, name, url, public_key, trust_score, is_active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (peer_id, "Inactive Peer", "https://inactive.example.com", public_key_bytes, 50.0, 0, now))
    test_db.commit()

    bucket = int(time.time() / 60)
    bundle = build_test_bundle(peer_id, private_key_bytes, [])
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 403
    assert "inactive" in response.json()["detail"].lower()


# Rate limit tests

def test_push_rate_limit_under_quota(client, test_db, registered_peer, valid_keypair):
    """Push 50 bundles → all 200."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair

    for i in range(50):
        bucket = int(time.time() / 60)
        memory = {
            "id": f"shmem-rate-test-{i}",
            "content": f"Rate test {i}",
            "category": "testing",
            "domain": "test-domain",
            "tags": ["rate"],
            "provenance": {
                "hop_count": 1,
                "original_author": peer_id,
                "original_timestamp": datetime.utcnow().isoformat(),
                "confidence": 0.9,
            },
            "privacy_tier": "public",
        }

        bundle = build_test_bundle(peer_id, private_key_bytes, [memory])
        signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

        response = client.post(
            "/api/v1/federation/push",
            json=bundle,
            headers={
                "X-Peer-Id": peer_id,
                "X-Peer-Signature": signature,
            }
        )

        assert response.status_code == 200


def test_push_rate_limit_exceeded(client, test_db, registered_peer, valid_keypair):
    """Push 101 bundles → first 100 OK, 101st → 429 with Retry-After: 60."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair

    # Push 100 bundles
    for i in range(100):
        bucket = int(time.time() / 60)
        memory = {
            "id": f"shmem-limit-test-{i}",
            "content": f"Limit test {i}",
            "category": "testing",
            "domain": "test-domain",
            "tags": ["limit"],
            "provenance": {
                "hop_count": 1,
                "original_author": peer_id,
                "original_timestamp": datetime.utcnow().isoformat(),
                "confidence": 0.9,
            },
            "privacy_tier": "public",
        }

        bundle = build_test_bundle(peer_id, private_key_bytes, [memory])
        signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

        response = client.post(
            "/api/v1/federation/push",
            json=bundle,
            headers={
                "X-Peer-Id": peer_id,
                "X-Peer-Signature": signature,
            }
        )

        assert response.status_code == 200

    # 101st push → rate limited
    bucket = int(time.time() / 60)
    memory = {
        "id": "shmem-limit-101",
        "content": "Should be rate limited",
        "category": "testing",
        "domain": "test-domain",
        "tags": ["limit"],
        "provenance": {
            "hop_count": 1,
            "original_author": peer_id,
            "original_timestamp": datetime.utcnow().isoformat(),
            "confidence": 0.9,
        },
        "privacy_tier": "public",
    }

    bundle = build_test_bundle(peer_id, private_key_bytes, [memory])
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert response.headers["Retry-After"] == "60"


def test_push_rate_limit_resets_next_window(client, test_db, registered_peer, valid_keypair):
    """Rate-limit a peer, mock time forward 1 min, next push → 200."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair

    current_time = time.time()
    bucket = int(current_time / 60)

    # Manually insert 100 requests in current bucket
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO federation_rate_limits (peer_id, window_start, request_count)
        VALUES (?, ?, 100)
    """, (peer_id, bucket))
    test_db.commit()

    # Push now → rate limited
    memory = {
        "id": "shmem-reset-test-1",
        "content": "Should be rate limited",
        "category": "testing",
        "domain": "test-domain",
        "tags": ["reset"],
        "provenance": {
            "hop_count": 1,
            "original_author": peer_id,
            "original_timestamp": datetime.utcnow().isoformat(),
            "confidence": 0.9,
        },
        "privacy_tier": "public",
    }

    bundle = build_test_bundle(peer_id, private_key_bytes, [memory])
    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 429

    # Mock time forward 61 seconds (next bucket)
    next_time = current_time + 61
    next_bucket = int(next_time / 60)

    with patch('time.time', return_value=next_time):
        memory2 = {
            "id": "shmem-reset-test-2",
            "content": "Should succeed in next window",
            "category": "testing",
            "domain": "test-domain",
            "tags": ["reset"],
            "provenance": {
                "hop_count": 1,
                "original_author": peer_id,
                "original_timestamp": datetime.utcnow().isoformat(),
                "confidence": 0.9,
            },
            "privacy_tier": "public",
        }

        bundle2 = build_test_bundle(peer_id, private_key_bytes, [memory2])
        signature2 = sign_push_challenge(peer_id, private_key_bytes, next_bucket)

        response2 = client.post(
            "/api/v1/federation/push",
            json=bundle2,
            headers={
                "X-Peer-Id": peer_id,
                "X-Peer-Signature": signature2,
            }
        )

        assert response2.status_code == 200


# Admission tests

def test_push_quarantined_expired_passport(client, test_db, registered_peer, valid_keypair):
    """Expired passport → 200 with decision=quarantined."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    # Build bundle with expired passport
    now = datetime.utcnow()

    passport = {
        "identity": {
            "name": peer_id,
            "role": "agent",
        },
        "score": {
            "total": 7.5,
        },
        "generated_at": now.isoformat(),
        "expires_at": (now - timedelta(days=1)).isoformat(),  # Expired
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.2, "graph_connections": 15},
    }

    bundle = {
        "peer_id": peer_id,
        "passport": passport,
        "memories": [],
        "timestamp": now.isoformat(),
    }

    # Derive bundle_id from SHA256
    canonical = canonicalize_for_signing(bundle)
    bundle_id = hashlib.sha256(canonical).hexdigest()[:16]
    bundle["bundle_id"] = bundle_id

    # Sign bundle WITH bundle_id
    canonical_with_id = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_with_id)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "quarantined"
    assert data["reason"] == "passport_expired"
    assert "quarantine_id" in data


def test_push_rejected_bad_signature(client, test_db, registered_peer, valid_keypair):
    """Bad bundle signature → 200 with decision=rejected."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    # Build bundle with invalid signature
    now = datetime.utcnow()
    passport = {
        "identity": {
            "name": peer_id,  # Must match peer_id
            "role": "agent",
        },
        "score": {
            "total": 7.5,
        },
        "generated_at": now.isoformat(),
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.2, "graph_connections": 15},
    }

    bundle = {
        "peer_id": peer_id,
        "passport": passport,
        "memories": [],
        "timestamp": now.isoformat(),
        "signature": base64.b64encode(b"bad-bundle-signature").decode('ascii'),
        "bundle_id": "fakebundleid",
    }

    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "rejected"
    assert data["reason"] == "signature_invalid"


def test_push_rejected_peer_mismatch(client, test_db, registered_peer, valid_keypair):
    """peer_id in bundle ≠ passport identity.name → 200 with decision=rejected."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    # Build bundle with mismatched passport identity
    now = datetime.utcnow()
    passport = {
        "identity": {
            "name": "peer-different-subject",  # Mismatch with peer_id
            "role": "agent",
        },
        "score": {
            "total": 7.5,
        },
        "generated_at": now.isoformat(),
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.2, "graph_connections": 15},
    }

    bundle = {
        "peer_id": peer_id,
        "passport": passport,
        "memories": [],
        "timestamp": now.isoformat(),
    }

    # Derive bundle_id from SHA256
    canonical = canonicalize_for_signing(bundle)
    bundle_id = hashlib.sha256(canonical).hexdigest()[:16]
    bundle["bundle_id"] = bundle_id

    # Sign bundle WITH bundle_id
    canonical_with_id = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature_bytes = private_key.sign(canonical_with_id)
    bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')

    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "rejected"
    assert data["reason"] == "passport_peer_mismatch"


# Malformed tests

def test_push_malformed_json(client, test_db, registered_peer, valid_keypair):
    """Invalid JSON → 400."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    response = client.post(
        "/api/v1/federation/push",
        data="not-json-{{{",
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
            "Content-Type": "application/json",
        }
    )

    assert response.status_code == 400
    assert "Invalid JSON" in response.json()["detail"]


def test_push_missing_peer_id_field(client, test_db, registered_peer, valid_keypair):
    """Missing peer_id in body (header present) → 400."""
    peer_id, _ = registered_peer
    private_key_bytes, _ = valid_keypair
    bucket = int(time.time() / 60)

    signature = sign_push_challenge(peer_id, private_key_bytes, bucket)

    # Bundle without peer_id field
    bundle = {
        "passport": {},
        "memories": [],
        "timestamp": datetime.utcnow().isoformat(),
    }

    response = client.post(
        "/api/v1/federation/push",
        json=bundle,
        headers={
            "X-Peer-Id": peer_id,
            "X-Peer-Signature": signature,
        }
    )

    assert response.status_code == 400
    assert "Missing peer_id in body" in response.json()["detail"]


# Integration test (push then pull dedup is covered by test_push_bundle_skipped_replay,
# but the design doc mentions test 17 separately — skipping as redundant per brief)
