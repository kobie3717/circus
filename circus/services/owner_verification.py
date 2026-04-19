"""Owner signature verification for preference memories (Week 5, sub-step 5.2).

Pure cryptographic verification primitive. No admission logic, no publish
validation — just the gate itself. Callers (5.4) will wire this into
preference_admission.py after same-owner string check.

TRUST MODEL (locked by design doc Q2, Q4):
- Owner is the trust anchor (Kobus), not the agent (Friday/Claw)
- Public key lookup is by owner_id, NEVER by agent_id
- agent_id is in the payload for audit trail, not for key resolution
- This allows multiple agents to share the same owner key

PAYLOAD BINDING (locked by design doc Q2, R1):
- Signed payload: {owner_id, agent_id, memory_id, timestamp} (exactly 4 fields)
- memory_id binding prevents signature replay across different memories
- timestamp binding prevents indefinite replay (±5min window)
- Verification fails if ANY field mismatches caller's assertion

TIMESTAMP WINDOW (locked by design doc Q2, R3):
- Bidirectional check: reject if too old OR too future
- Window: ±5 minutes relative to shared_at
- Distinct reason codes for expired vs future (ops clarity)

FAIL-CLOSED (locked by design doc R4):
- Never raises for bad input (missing fields, unknown owner, bad sig)
- Only raises for true infra errors (DB connection drop, SQLite corruption)
- Returns structured VerificationResult with reason code

CANONICALIZATION (locked by design doc R5):
- Reuses bundle_signing.canonicalize_for_signing (W3 helper)
- One canonical form across codebase, zero hand-rolled JSON serialization

FUNCTION SIGNATURE (locked by design doc R6):
- Caller passes raw fields (owner_id, agent_id, memory_id, timestamp, signature)
- Function reconstructs canonical payload internally from raw fields
- Never accepts pre-built payload blob (ensures signed surface = verified surface)
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import base64
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from circus.services.bundle_signing import canonicalize_for_signing


# Timestamp tolerance window (bidirectional, ±5 minutes)
OWNER_BINDING_TIMESTAMP_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class VerificationResult:
    """Result of owner signature verification.

    Reuses the same pattern as federation_verify.py for consistency.
    Fail-closed: valid=False with reason code on any verification failure.
    """
    valid: bool
    reason: Optional[str] = None  # None on success; reason code on failure


def verify_owner_binding(
    *,
    claimed_owner_id: str,
    claimed_agent_id: str,
    claimed_memory_id: str,
    claimed_timestamp: str,  # ISO8601 from owner_binding
    signature_b64: str,      # base64 signature from owner_binding
    shared_at: str,          # ISO8601 from the memory envelope
    conn: sqlite3.Connection,
) -> VerificationResult:
    """Verify owner signature on preference memory binding.

    This is a pure verification primitive. It does NOT:
    - Check if owner_id matches server's CIRCUS_OWNER_ID (that's gate 4, already done)
    - Check if domain is "preference.user" (that's publish validation, 5.3)
    - Check confidence threshold (that's gate 6, after this)

    It ONLY verifies:
    1. Owner's public key exists in owner_keys table (lookup by owner_id)
    2. Signature is cryptographically valid against that public key
    3. Signed payload matches claimed fields (owner_id, agent_id, memory_id, timestamp)
    4. Timestamp is within ±5min of shared_at (bidirectional window)

    ORDERING (efficiency — cheap checks first, crypto last):
    1. Look up public key (DB query) → owner_key_unknown if missing
    2. Check timestamp window (arithmetic) → expired/future if out of bounds
    3. Verify signature (crypto) → owner_signature_invalid if bad

    This means memory_id mismatches surface as owner_signature_invalid
    (the reconstructed payload won't match what was signed), which is correct:
    the memory_id binding is enforced BY CRYPTOGRAPHY, not string equality.

    Args:
        claimed_owner_id: Owner ID from provenance.owner_id (used for key lookup)
        claimed_agent_id: Agent ID from owner_binding.agent_id (audit trail)
        claimed_memory_id: Memory ID from owner_binding.memory_id (replay prevention)
        claimed_timestamp: ISO8601 timestamp from owner_binding.timestamp
        signature_b64: Base64 Ed25519 signature from owner_binding.signature
        shared_at: ISO8601 timestamp from memory envelope (for window check)
        conn: SQLite connection for owner_keys lookup

    Returns:
        VerificationResult with valid=True on success, valid=False + reason on failure

    Reason codes:
        - owner_key_unknown: claimed_owner_id not in owner_keys table
        - owner_binding_expired: timestamp too old (>5min before shared_at)
        - owner_binding_future_timestamp: timestamp too far ahead (>5min after shared_at)
        - owner_signature_invalid: signature verification failed (bad sig OR field mismatch)

    Raises:
        Only on true infrastructure errors (DB connection drop, SQLite corruption).
        Bad input (malformed binding, missing fields, etc.) returns VerificationResult
        with valid=False, never raises.
    """
    # Step 1: Fetch owner's public key from owner_keys table
    # KEY FETCH RULE (design doc Q2): use owner_id as lookup key, NOT agent_id
    # Owner is the trust anchor; agents are replaceable and share the owner's key
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT public_key FROM owner_keys WHERE owner_id = ?",
            (claimed_owner_id,)
        )
        row = cursor.fetchone()

        if row is None:
            return VerificationResult(
                valid=False,
                reason="owner_key_unknown"
            )

        public_key_b64 = row[0]
        public_key_bytes = base64.b64decode(public_key_b64)

    except sqlite3.Error:
        # True infra error (DB connection drop, corruption) — re-raise
        raise

    # Step 2: Check timestamp window (bidirectional, ±5min)
    # Parse timestamps to datetime objects for comparison
    try:
        binding_time = datetime.fromisoformat(claimed_timestamp.replace('Z', '+00:00'))
        shared_time = datetime.fromisoformat(shared_at.replace('Z', '+00:00'))
    except ValueError:
        # Malformed ISO8601 timestamp — fail closed
        return VerificationResult(
            valid=False,
            reason="owner_signature_invalid"  # treat as bad input
        )

    # Calculate time delta (absolute difference)
    time_delta_seconds = abs((shared_time - binding_time).total_seconds())

    # Check if binding is too old
    if (shared_time - binding_time).total_seconds() > OWNER_BINDING_TIMESTAMP_WINDOW_SECONDS:
        return VerificationResult(
            valid=False,
            reason="owner_binding_expired"
        )

    # Check if binding is too far in the future
    if (binding_time - shared_time).total_seconds() > OWNER_BINDING_TIMESTAMP_WINDOW_SECONDS:
        return VerificationResult(
            valid=False,
            reason="owner_binding_future_timestamp"
        )

    # Step 3: Reconstruct canonical payload and verify signature
    # PAYLOAD RECONSTRUCTION (design doc R6): build payload from raw fields internally
    # This ensures the signed surface and verified surface are bit-identical
    payload = {
        "agent_id": claimed_agent_id,
        "memory_id": claimed_memory_id,
        "owner_id": claimed_owner_id,
        "timestamp": claimed_timestamp,
    }

    # Canonicalize payload using the same helper as W3 bundle signing (design doc R5)
    try:
        canonical_bytes = canonicalize_for_signing(payload)
    except Exception:
        # Canonicalization failure (non-JSON-native types, etc.) — fail closed
        return VerificationResult(
            valid=False,
            reason="owner_signature_invalid"
        )

    # Decode signature from base64
    try:
        signature_bytes = base64.b64decode(signature_b64)
    except Exception:
        # Malformed base64 signature — fail closed
        return VerificationResult(
            valid=False,
            reason="owner_signature_invalid"
        )

    # Verify signature
    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature_bytes, canonical_bytes)
        # Signature is valid — all checks passed
        return VerificationResult(valid=True, reason=None)
    except Exception:
        # Signature verification failed (bad sig, wrong key, field mismatch via payload)
        # This includes the case where memory_id/agent_id/owner_id don't match what
        # was signed — the reconstructed payload will differ, so verification fails.
        # Memory ID binding is enforced BY CRYPTOGRAPHY, not explicit string checks.
        return VerificationResult(
            valid=False,
            reason="owner_signature_invalid"
        )
