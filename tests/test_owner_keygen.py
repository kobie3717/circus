"""Tests for owner keypair management (Week 5, sub-step 5.1)."""

import base64
import stat
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from circus.cli import CircusCLI
from circus.config import settings
from circus.database import init_database, get_db


@pytest.fixture
def test_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    original_path = settings.database_path
    settings.database_path = db_path
    init_database(db_path)

    yield db_path

    settings.database_path = original_path
    db_path.unlink()


def test_owner_keygen_creates_keypair_and_db_row(tmp_path, test_db):
    """Test that owner-keygen creates keypair files and DB row."""
    # Mock args for owner-keygen
    class Args:
        owner = "test-owner"
        output = str(tmp_path / "test-owner.key")
        description = "Test owner keypair"
        force = False

    cli = CircusCLI()
    cli.owner_keygen(Args())

    # Assert private key file exists with 600 permissions
    private_key_path = Path(Args.output)
    assert private_key_path.exists(), "Private key file should exist"
    file_mode = private_key_path.stat().st_mode & 0o777
    assert file_mode == 0o600, f"Private key should have 600 perms, got {oct(file_mode)}"

    # Assert public key file exists with 644 permissions
    public_key_path = private_key_path.with_suffix('.pub')
    assert public_key_path.exists(), "Public key file should exist"
    file_mode = public_key_path.stat().st_mode & 0o777
    assert file_mode == 0o644, f"Public key should have 644 perms, got {oct(file_mode)}"

    # Assert DB row exists
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT public_key FROM owner_keys WHERE owner_id = ?", ("test-owner",))
        row = cursor.fetchone()
        assert row is not None, "DB row should exist for test-owner"

        db_public_key = row[0]

        # Assert public key in DB matches file content
        file_public_key = public_key_path.read_text().strip()
        assert db_public_key == file_public_key, "Public key in DB should match file"


def test_public_key_roundtrips_through_base64():
    """Test that public key roundtrips correctly through base64 encoding/decoding."""
    from circus.services.signing import generate_keypair, encode_public_key, decode_public_key

    # Generate keypair
    private_bytes, public_bytes = generate_keypair()

    # Encode to base64
    public_b64 = encode_public_key(public_bytes)

    # Decode back to bytes
    decoded_public_bytes = decode_public_key(public_b64)

    # Assert bytes match
    assert decoded_public_bytes == public_bytes, "Public key should roundtrip through base64"

    # Assert we can use it for Ed25519 operations (load as public key)
    public_key = Ed25519PublicKey.from_public_bytes(decoded_public_bytes)
    assert public_key is not None, "Decoded bytes should be valid Ed25519 public key"

    # Sign with private key and verify with public key
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    message = b"test message"
    signature = private_key.sign(message)

    # Should not raise
    public_key.verify(signature, message)


def test_private_key_file_has_600_permissions(tmp_path, test_db):
    """Test that private key file has 600 permissions."""
    class Args:
        owner = "test-perms"
        output = str(tmp_path / "test-perms.key")
        description = None
        force = False

    cli = CircusCLI()
    cli.owner_keygen(Args())

    private_key_path = Path(Args.output)
    file_stat = private_key_path.stat()
    file_mode = file_stat.st_mode & 0o777

    assert file_mode == 0o600, f"Private key should have mode 0o600, got {oct(file_mode)}"
