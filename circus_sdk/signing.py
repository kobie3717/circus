"""Signing helpers for Circus SDK."""

import base64
import json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def sign_owner_binding(
    owner_id: str,
    agent_id: str,
    memory_id: str,
    timestamp: str,
    private_key_b64: str,
) -> str:
    """Sign an owner binding for preference publication.

    Args:
        owner_id: Owner identifier
        agent_id: Publishing agent ID
        memory_id: Memory ID being signed
        timestamp: ISO timestamp
        private_key_b64: Base64-encoded Ed25519 private key (64 bytes)

    Returns:
        Base64-encoded signature
    """
    # Decode private key
    private_key_bytes = base64.b64decode(private_key_b64)
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)

    # Create signing payload
    payload = {
        "owner_id": owner_id,
        "agent_id": agent_id,
        "memory_id": memory_id,
        "timestamp": timestamp,
    }
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")

    # Sign
    signature = private_key.sign(payload_bytes)

    return base64.b64encode(signature).decode("utf-8")


def generate_keypair() -> tuple[str, str]:
    """Generate Ed25519 keypair for owner signing.

    Returns:
        Tuple of (private_key_b64, public_key_b64)
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

    return (
        base64.b64encode(private_bytes).decode("utf-8"),
        base64.b64encode(public_bytes).decode("utf-8"),
    )
