"""Tests for instance identity bootstrap (Sub-step 3.5a-prereq)."""

import base64
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from circus.database import init_database, run_v2_migration, run_v3_migration, run_v4_migration, run_v5_migration
from circus.services.instance_identity import (
    InstanceIdentityError,
    ensure_instance_keypair,
    get_instance_identity
)


# Test fixtures

@pytest.fixture
def test_db():
    """Create temporary database with all migrations run."""
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
def empty_db_with_v5_table():
    """Create database with v5 migration run but no identity seeded."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    # Manually create table without running the full v5 migration seed
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE instance_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()

    yield conn

    conn.close()
    db_path.unlink(missing_ok=True)


# Tests

def test_v5_migration_creates_table(test_db):
    """Test 1: After run_v5_migration, instance_config table exists."""
    cursor = test_db.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='instance_config'")
    row = cursor.fetchone()
    assert row is not None, "instance_config table should exist after v5 migration"


def test_v5_migration_idempotent():
    """Test 2: Running run_v5_migration twice is a no-op."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        init_database(db_path)
        run_v2_migration(db_path)
        run_v3_migration(db_path)
        run_v4_migration(db_path)

        # Run v5 migration twice
        run_v5_migration(db_path)
        run_v5_migration(db_path)

        # Verify identity exists and is stable
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM instance_config WHERE key IN ('instance_id', 'private_key', 'public_key')")
        count = cursor.fetchone()[0]
        assert count == 3, "Should have exactly 3 identity rows after double migration"

        # Verify no duplicates
        cursor.execute("SELECT key FROM instance_config")
        keys = [row[0] for row in cursor.fetchall()]
        assert len(keys) == len(set(keys)), "No duplicate keys should exist"

        conn.close()
    finally:
        db_path.unlink(missing_ok=True)


def test_ensure_generates_on_first_call(empty_db_with_v5_table):
    """Test 3: First call on empty DB writes 3 rows (instance_id, private_key, public_key)."""
    conn = empty_db_with_v5_table

    # Verify empty before call
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM instance_config")
    assert cursor.fetchone()[0] == 0, "DB should start empty"

    # Call ensure_instance_keypair
    identity = ensure_instance_keypair(conn)
    conn.commit()

    # Verify 3 rows written
    cursor.execute("SELECT COUNT(*) FROM instance_config")
    count = cursor.fetchone()[0]
    assert count == 3, "Should write exactly 3 rows (instance_id, private_key, public_key)"

    # Verify all keys present
    cursor.execute("SELECT key FROM instance_config ORDER BY key")
    keys = sorted([row[0] for row in cursor.fetchall()])
    assert keys == ['instance_id', 'private_key', 'public_key'], "All identity keys should be present"

    # Verify identity structure
    assert identity.instance_id.startswith('circus-'), "instance_id should have circus- prefix"
    assert len(identity.private_key_bytes) == 32, "Private key should be 32 bytes"
    assert len(identity.public_key_bytes) == 32, "Public key should be 32 bytes"


def test_ensure_idempotent_returns_same_identity(empty_db_with_v5_table):
    """Test 4: Second call returns byte-identical keys as the first."""
    conn = empty_db_with_v5_table

    # First call
    identity1 = ensure_instance_keypair(conn)
    conn.commit()

    # Second call
    identity2 = ensure_instance_keypair(conn)

    # Verify byte-identical
    assert identity1.instance_id == identity2.instance_id, "instance_id should be identical"
    assert identity1.private_key_bytes == identity2.private_key_bytes, "private_key_bytes should be identical"
    assert identity1.public_key_bytes == identity2.public_key_bytes, "public_key_bytes should be identical"

    # Verify no extra rows created
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM instance_config")
    assert cursor.fetchone()[0] == 3, "Should still have exactly 3 rows"


def test_instance_id_format(empty_db_with_v5_table):
    """Test 5: Returned instance_id matches ^circus-[0-9a-f]{16}$."""
    conn = empty_db_with_v5_table

    identity = ensure_instance_keypair(conn)
    conn.commit()

    # Validate format
    pattern = r'^circus-[0-9a-f]{16}$'
    assert re.match(pattern, identity.instance_id), \
        f"instance_id '{identity.instance_id}' should match pattern {pattern}"


def test_keypair_is_valid_ed25519(empty_db_with_v5_table):
    """Test 6: Ed25519PrivateKey.from_private_bytes loads and derives matching public key."""
    conn = empty_db_with_v5_table

    identity = ensure_instance_keypair(conn)
    conn.commit()

    # Load private key
    private_key = Ed25519PrivateKey.from_private_bytes(identity.private_key_bytes)

    # Derive public key and compare
    from cryptography.hazmat.primitives import serialization
    derived_public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )

    assert derived_public_bytes == identity.public_key_bytes, \
        "Stored public key should match the key derived from private key"


def test_signing_roundtrip(empty_db_with_v5_table):
    """Test 7: Sign a test message with private key, verify with public key."""
    conn = empty_db_with_v5_table

    identity = ensure_instance_keypair(conn)
    conn.commit()

    # Load keys
    private_key = Ed25519PrivateKey.from_private_bytes(identity.private_key_bytes)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    public_key = Ed25519PublicKey.from_public_bytes(identity.public_key_bytes)

    # Sign a message
    message = b"The Circus Memory Commons Week 3 Sub-step 3.5a-prereq"
    signature = private_key.sign(message)

    # Verify signature
    try:
        public_key.verify(signature, message)
        verified = True
    except Exception:
        verified = False

    assert verified, "Signature should verify with the public key"


def test_get_identity_strict_raises_on_missing(empty_db_with_v5_table):
    """Test 8: get_instance_identity raises on fresh DB with no keys written."""
    conn = empty_db_with_v5_table

    with pytest.raises(InstanceIdentityError) as exc_info:
        get_instance_identity(conn)

    assert "missing keys" in str(exc_info.value).lower(), \
        "Error message should mention missing keys"


def test_ensure_raises_on_corrupt_state(empty_db_with_v5_table):
    """Test 9: Manually INSERT only private_key → ensure_instance_keypair raises."""
    conn = empty_db_with_v5_table
    cursor = conn.cursor()

    # Manually insert only private_key (simulating corrupt state)
    cursor.execute(
        "INSERT INTO instance_config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
        ('private_key', base64.b64encode(b'x' * 32).decode('ascii'))
    )
    conn.commit()

    # ensure_instance_keypair should raise
    with pytest.raises(InstanceIdentityError) as exc_info:
        ensure_instance_keypair(conn)

    error_message = str(exc_info.value)
    assert "corrupt" in error_message.lower(), "Error should mention corruption"
    assert "public_key" in error_message, "Error should identify missing public_key"


def test_migration_wiring_seeds_identity(test_db):
    """Test 10: After init_database + all migrations, get_instance_identity succeeds."""
    # test_db fixture already runs all migrations including v5
    # This proves v5 is wired in the right order and seed runs

    try:
        identity = get_instance_identity(test_db)
        success = True
    except InstanceIdentityError:
        success = False

    assert success, "get_instance_identity should succeed after full migration chain"
    assert identity.instance_id.startswith('circus-'), "Should have valid instance_id"
    assert len(identity.private_key_bytes) == 32, "Should have valid private key"
    assert len(identity.public_key_bytes) == 32, "Should have valid public key"
