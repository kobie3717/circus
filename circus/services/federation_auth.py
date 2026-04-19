"""Challenge-based peer authentication for federation PULL endpoint.

Authentication protocol:
- Peer signs a time-bucketed challenge string with their Ed25519 private key
- Challenge format: "pull:{peer_id}:{minute_bucket}"
- Minute buckets provide replay resistance (±1 minute clock skew tolerance)
- Public key verified against federation_peers table
- Peer must be registered and active

This module handles ONLY authentication logic. Bundle construction and
query logic are separate (federation_pull module).
"""

import base64
import sqlite3
import time
from typing import Optional

from circus.database import get_db
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class AuthError(Exception):
    """Raised when authentication fails.

    Carries HTTP status code for error mapping at the route layer.
    """
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


def verify_pull_challenge(
    peer_id: str,
    signature_b64: str,
    *,
    now: Optional[float] = None
) -> tuple[bool, Optional[str]]:
    """Verify peer's signature over time-bucketed pull challenge.

    Args:
        peer_id: Peer identifier from X-Peer-Id header
        signature_b64: Base64-encoded Ed25519 signature from X-Peer-Signature header
        now: Unix timestamp for testing (defaults to current time)

    Returns:
        Tuple of (success, peer_id_if_valid)

    Raises:
        AuthError with appropriate status_code:
            - 401: Malformed/invalid signature or stale timestamp
            - 403: Peer not registered or inactive
    """
    if now is None:
        now = time.time()

    # Compute current minute bucket and ±1 for clock skew tolerance
    current_bucket = int(now // 60)
    valid_buckets = [current_bucket - 1, current_bucket, current_bucket + 1]

    # Lookup peer in federation_peers
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT public_key, is_active
            FROM federation_peers
            WHERE id = ?
        """, (peer_id,))
        row = cursor.fetchone()

    if not row:
        raise AuthError(
            f"Peer not registered: {peer_id}",
            status_code=403
        )

    if not row["is_active"]:
        raise AuthError(
            f"Peer inactive: {peer_id}",
            status_code=403
        )

    public_key_bytes = row["public_key"]

    # Validate signature is a string
    if not isinstance(signature_b64, str):
        raise AuthError(
            f"Signature must be string, got {type(signature_b64).__name__}",
            status_code=401
        )

    # Decode signature
    try:
        signature_bytes = base64.b64decode(signature_b64)
    except Exception as exc:
        raise AuthError(
            f"Signature base64 decode failed: {exc}",
            status_code=401
        )

    # Verify signature against all valid challenge buckets
    public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)

    for bucket in valid_buckets:
        challenge = f"pull:{peer_id}:{bucket}"
        challenge_bytes = challenge.encode('utf-8')

        try:
            public_key.verify(signature_bytes, challenge_bytes)
            # Success — signature valid for this bucket
            return (True, peer_id)
        except Exception:
            # Signature invalid for this bucket — try next
            continue

    # No valid bucket found — signature expired or invalid
    raise AuthError(
        f"Invalid signature or expired timestamp (tried buckets {valid_buckets})",
        status_code=401
    )
