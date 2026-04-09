"""Ed25519 cryptographic signing for agent cards."""

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey
)


def generate_keypair() -> tuple[bytes, bytes]:
    """
    Generate Ed25519 keypair.

    Returns:
        Tuple of (private_key_bytes, public_key_bytes)
    """
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption()
    )

    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )

    return private_bytes, public_bytes


def sign_agent_card(
    agent_data: dict[str, Any],
    private_key_bytes: bytes
) -> str:
    """
    Sign agent capability declaration.

    Args:
        agent_data: Agent profile data (capabilities, role, etc.)
        private_key_bytes: 32-byte Ed25519 private key

    Returns:
        Base64-encoded signature
    """
    # Canonical JSON for signing
    canonical_json = json.dumps(agent_data, sort_keys=True, separators=(',', ':'))
    message = canonical_json.encode('utf-8')

    # Load private key
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)

    # Sign
    signature = private_key.sign(message)

    return base64.b64encode(signature).decode('ascii')


def verify_signature(
    agent_data: dict[str, Any],
    signature_b64: str,
    public_key_bytes: bytes
) -> bool:
    """
    Verify agent card signature.

    Args:
        agent_data: Agent profile data
        signature_b64: Base64-encoded signature
        public_key_bytes: 32-byte Ed25519 public key

    Returns:
        True if signature is valid
    """
    try:
        # Reconstruct canonical JSON
        canonical_json = json.dumps(agent_data, sort_keys=True, separators=(',', ':'))
        message = canonical_json.encode('utf-8')

        # Decode signature
        signature = base64.b64decode(signature_b64)

        # Load public key
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)

        # Verify
        public_key.verify(signature, message)
        return True
    except Exception:
        return False


def encode_public_key(public_key_bytes: bytes) -> str:
    """Encode public key as base64."""
    return base64.b64encode(public_key_bytes).decode('ascii')


def decode_public_key(public_key_b64: str) -> bytes:
    """Decode base64 public key."""
    return base64.b64decode(public_key_b64)
