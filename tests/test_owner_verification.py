"""Tests for owner signature verification service (Week 5, sub-step 5.2)."""

import base64
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from circus.config import settings
from circus.database import init_database, get_db
from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.owner_verification import (
    verify_owner_binding,
    OWNER_BINDING_TIMESTAMP_WINDOW_SECONDS,
)


@pytest.fixture
def test_db():
    """Create temporary database with owner_keys table."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    original_path = settings.database_path
    settings.database_path = db_path
    init_database(db_path)

    yield db_path

    settings.database_path = original_path
    db_path.unlink()


def generate_test_keypair():
    """Generate Ed25519 keypair for testing."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    # Extract raw bytes
    from cryptography.hazmat.primitives import serialization
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


def insert_owner_key(conn, owner_id: str, public_key_b64: str):
    """Helper to insert owner key into DB."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
        (owner_id, public_key_b64, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def sign_owner_binding(private_key, owner_id: str, agent_id: str, memory_id: str, timestamp: str) -> str:
    """Sign owner binding payload and return base64 signature."""
    payload = {
        "agent_id": agent_id,
        "memory_id": memory_id,
        "owner_id": owner_id,
        "timestamp": timestamp,
    }
    canonical_bytes = canonicalize_for_signing(payload)
    signature = private_key.sign(canonical_bytes)
    return base64.b64encode(signature).decode('ascii')


def test_valid_signature_verifies(test_db):
    """Test that a valid owner signature verifies successfully."""
    # Generate keypair and insert public key into DB
    private_key, _, public_bytes = generate_test_keypair()
    public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

    with get_db() as conn:
        insert_owner_key(conn, "kobus", public_key_b64)

        # Create binding with current timestamp
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        shared_at = now.isoformat()

        # Sign the binding
        signature = sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id="agent-friday",
            memory_id="mem-abc123",
            timestamp=timestamp
        )

        # Verify
        result = verify_owner_binding(
            claimed_owner_id="kobus",
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-abc123",
            claimed_timestamp=timestamp,
            signature_b64=signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is True, "Valid signature should verify"
        assert result.reason is None, "Valid signature should have no reason code"


def test_invalid_signature_wrong_key(test_db):
    """Test that signature verification fails when using wrong public key."""
    # Generate TWO keypairs
    private_key_a, _, _ = generate_test_keypair()
    _, _, public_bytes_b = generate_test_keypair()

    # Store key B in DB
    public_key_b64 = base64.b64encode(public_bytes_b).decode('ascii')

    with get_db() as conn:
        insert_owner_key(conn, "kobus", public_key_b64)

        # Sign with key A
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        shared_at = now.isoformat()

        signature = sign_owner_binding(
            private_key_a,  # Sign with key A
            owner_id="kobus",
            agent_id="agent-friday",
            memory_id="mem-abc123",
            timestamp=timestamp
        )

        # Verify against key B (stored in DB)
        result = verify_owner_binding(
            claimed_owner_id="kobus",
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-abc123",
            claimed_timestamp=timestamp,
            signature_b64=signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is False, "Signature with wrong key should fail"
        assert result.reason == "owner_signature_invalid", f"Expected owner_signature_invalid, got {result.reason}"


def test_missing_owner_in_db(test_db):
    """Test that verification fails when owner_id is not in owner_keys table."""
    private_key, _, _ = generate_test_keypair()

    with get_db() as conn:
        # DO NOT insert owner key into DB

        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        shared_at = now.isoformat()

        signature = sign_owner_binding(
            private_key,
            owner_id="unknown-owner",
            agent_id="agent-friday",
            memory_id="mem-abc123",
            timestamp=timestamp
        )

        result = verify_owner_binding(
            claimed_owner_id="unknown-owner",
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-abc123",
            claimed_timestamp=timestamp,
            signature_b64=signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is False, "Unknown owner should fail verification"
        assert result.reason == "owner_key_unknown", f"Expected owner_key_unknown, got {result.reason}"


def test_timestamp_too_old(test_db):
    """Test that verification fails when binding timestamp is too old (>5min before shared_at)."""
    private_key, _, public_bytes = generate_test_keypair()
    public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

    with get_db() as conn:
        insert_owner_key(conn, "kobus", public_key_b64)

        # Create binding timestamp 10 minutes in the past
        now = datetime.now(timezone.utc)
        old_timestamp = (now - timedelta(minutes=10)).isoformat()
        shared_at = now.isoformat()

        signature = sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id="agent-friday",
            memory_id="mem-abc123",
            timestamp=old_timestamp
        )

        result = verify_owner_binding(
            claimed_owner_id="kobus",
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-abc123",
            claimed_timestamp=old_timestamp,
            signature_b64=signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is False, "Expired binding should fail"
        assert result.reason == "owner_binding_expired", f"Expected owner_binding_expired, got {result.reason}"


def test_timestamp_too_future(test_db):
    """Test that verification fails when binding timestamp is too far in future (>5min ahead of shared_at)."""
    private_key, _, public_bytes = generate_test_keypair()
    public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

    with get_db() as conn:
        insert_owner_key(conn, "kobus", public_key_b64)

        # Create binding timestamp 10 minutes in the future
        now = datetime.now(timezone.utc)
        future_timestamp = (now + timedelta(minutes=10)).isoformat()
        shared_at = now.isoformat()

        signature = sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id="agent-friday",
            memory_id="mem-abc123",
            timestamp=future_timestamp
        )

        result = verify_owner_binding(
            claimed_owner_id="kobus",
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-abc123",
            claimed_timestamp=future_timestamp,
            signature_b64=signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is False, "Future-dated binding should fail"
        assert result.reason == "owner_binding_future_timestamp", f"Expected owner_binding_future_timestamp, got {result.reason}"


def test_memory_id_mismatch_fails(test_db):
    """Test that signature verification fails when memory_id doesn't match (replay prevention).

    This proves the memory_id binding property: a valid signature on memory-A
    cannot be replayed to claim memory-B. The memory_id is part of the signed
    payload, so any mismatch causes verification to fail.
    """
    private_key, _, public_bytes = generate_test_keypair()
    public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

    with get_db() as conn:
        insert_owner_key(conn, "kobus", public_key_b64)

        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        shared_at = now.isoformat()

        # Sign binding for memory-A
        signature = sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id="agent-friday",
            memory_id="mem-A",
            timestamp=timestamp
        )

        # Try to verify with memory-B (replay attack)
        result = verify_owner_binding(
            claimed_owner_id="kobus",
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-B",  # Different memory_id
            claimed_timestamp=timestamp,
            signature_b64=signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is False, "Memory ID mismatch should fail verification"
        assert result.reason == "owner_signature_invalid", (
            f"Memory ID binding enforced by crypto, got {result.reason}"
        )


def test_agent_id_mismatch_fails(test_db):
    """Test that signature verification fails when agent_id doesn't match."""
    private_key, _, public_bytes = generate_test_keypair()
    public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

    with get_db() as conn:
        insert_owner_key(conn, "kobus", public_key_b64)

        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        shared_at = now.isoformat()

        # Sign with agent_id="friday"
        signature = sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id="agent-friday",
            memory_id="mem-abc123",
            timestamp=timestamp
        )

        # Try to verify with agent_id="claw"
        result = verify_owner_binding(
            claimed_owner_id="kobus",
            claimed_agent_id="agent-claw",  # Different agent_id
            claimed_memory_id="mem-abc123",
            claimed_timestamp=timestamp,
            signature_b64=signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is False, "Agent ID mismatch should fail verification"
        assert result.reason == "owner_signature_invalid", (
            f"Agent ID binding enforced by crypto, got {result.reason}"
        )


def test_owner_id_mismatch_fails(test_db):
    """Test that signature verification fails when owner_id in payload doesn't match lookup key.

    This test verifies the owner-id-in-payload vs owner-id-for-lookup consistency.
    If Alice signs a payload claiming "owner_id: alice" but we try to verify it
    against Kobus's stored public key, verification fails because Kobus's key
    can't verify a signature made with Alice's private key.
    """
    # Generate two keypairs (alice and kobus)
    private_key_alice, _, public_bytes_alice = generate_test_keypair()
    _, _, public_bytes_kobus = generate_test_keypair()

    public_key_alice_b64 = base64.b64encode(public_bytes_alice).decode('ascii')
    public_key_kobus_b64 = base64.b64encode(public_bytes_kobus).decode('ascii')

    with get_db() as conn:
        # Insert both keys into DB
        insert_owner_key(conn, "alice", public_key_alice_b64)
        insert_owner_key(conn, "kobus", public_key_kobus_b64)

        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        shared_at = now.isoformat()

        # Alice signs a payload claiming owner_id="alice"
        signature_alice = sign_owner_binding(
            private_key_alice,
            owner_id="alice",
            agent_id="agent-friday",
            memory_id="mem-abc123",
            timestamp=timestamp
        )

        # Try to verify it against kobus's key (claimed_owner_id="kobus")
        # This should fail because:
        # 1. We reconstruct payload with owner_id="kobus" (doesn't match signed payload)
        # 2. Even if we used "alice", kobus's pubkey can't verify alice's signature
        result = verify_owner_binding(
            claimed_owner_id="kobus",  # Different owner
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-abc123",
            claimed_timestamp=timestamp,
            signature_b64=signature_alice,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is False, "Owner ID mismatch should fail verification"
        assert result.reason == "owner_signature_invalid", (
            f"Owner ID mismatch enforced by crypto, got {result.reason}"
        )


def test_timestamp_within_window_passes(test_db):
    """Test that timestamps within ±5min window pass verification."""
    private_key, _, public_bytes = generate_test_keypair()
    public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

    with get_db() as conn:
        insert_owner_key(conn, "kobus", public_key_b64)

        now = datetime.now(timezone.utc)

        # Test edge of window: 4.5 minutes old (within 5min)
        old_timestamp = (now - timedelta(minutes=4.5)).isoformat()
        shared_at = now.isoformat()

        signature = sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id="agent-friday",
            memory_id="mem-abc123",
            timestamp=old_timestamp
        )

        result = verify_owner_binding(
            claimed_owner_id="kobus",
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-abc123",
            claimed_timestamp=old_timestamp,
            signature_b64=signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is True, "Timestamp within 5min should pass"
        assert result.reason is None


def test_malformed_base64_signature_fails(test_db):
    """Test that malformed base64 signature fails gracefully (fail-closed)."""
    _, _, public_bytes = generate_test_keypair()
    public_key_b64 = base64.b64encode(public_bytes).decode('ascii')

    with get_db() as conn:
        insert_owner_key(conn, "kobus", public_key_b64)

        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        shared_at = now.isoformat()

        # Use malformed base64 signature
        malformed_signature = "not-valid-base64!@#$"

        result = verify_owner_binding(
            claimed_owner_id="kobus",
            claimed_agent_id="agent-friday",
            claimed_memory_id="mem-abc123",
            claimed_timestamp=timestamp,
            signature_b64=malformed_signature,
            shared_at=shared_at,
            conn=conn
        )

        assert result.valid is False, "Malformed signature should fail gracefully"
        assert result.reason == "owner_signature_invalid"
