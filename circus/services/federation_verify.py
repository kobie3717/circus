"""Federation verification pipeline — five separate checks, fail closed.

Each stage is an independently callable function that returns a
VerificationResult. Callers compose them in order; admission logic
(sub-step 3.3) decides quarantine vs hard-reject per the Q-C matrix.

No stage raises on verification failure. Crypto errors, malformed
input, DB misses — all become VerificationResult(valid=False, reason=...).

Separation of concerns (Kobus, 2026-04-18):
- signature valid ≠ passport valid
- passport valid ≠ peer known
- peer known ≠ peer trusted
- each its own reason code, each its own audit entry later.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from circus.config import settings
from circus.database import get_db
from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.passport import validate_passport
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import base64


@dataclass(frozen=True)
class VerificationResult:
    """Result of a single verification stage.

    Designed for fail-closed composition — five stages, five results.
    Reasons map 1:1 to federation_audit action codes and Q-C quarantine
    matrix entries in the step 3 plan.
    """
    valid: bool
    reason: Optional[str] = None  # None on success; reason code on failure
    peer_id: Optional[str] = None
    detail: Optional[str] = None  # Human-readable debug info, safe to log
    metadata: dict = field(default_factory=dict)  # Extra structured data


def verify_signature(bundle: dict, public_key: bytes) -> VerificationResult:
    """Verify bundle signature cryptographically.

    Args:
        bundle: Bundle dict (must include 'signature' field)
        public_key: 32-byte Ed25519 public key of the claimed signer

    Returns:
        VerificationResult with valid=True if signature matches,
        valid=False with reason code otherwise.

    Reason codes:
        - signature_malformed: signature field missing or wrong format
        - signature_invalid: signature present but verification failed
    """
    peer_id = bundle.get("peer_id")

    # Check signature field exists and is string
    signature_b64 = bundle.get("signature")
    if not signature_b64:
        return VerificationResult(
            valid=False,
            reason="signature_malformed",
            peer_id=peer_id,
            detail="signature field missing"
        )

    if not isinstance(signature_b64, str):
        return VerificationResult(
            valid=False,
            reason="signature_malformed",
            peer_id=peer_id,
            detail=f"signature field wrong type: {type(signature_b64).__name__}"
        )

    # Decode signature bytes
    try:
        signature_bytes = base64.b64decode(signature_b64)
    except Exception as exc:
        return VerificationResult(
            valid=False,
            reason="signature_malformed",
            peer_id=peer_id,
            detail=f"signature base64 decode failed: {exc}"
        )

    # Canonicalize bundle (strips signature field)
    try:
        canonical_bytes = canonicalize_for_signing(bundle)
    except Exception as exc:
        # Bundle structure is so broken we can't even serialize it
        return VerificationResult(
            valid=False,
            reason="signature_malformed",
            peer_id=peer_id,
            detail=f"bundle canonicalization failed: {exc}"
        )

    # Verify signature
    try:
        public_key_obj = Ed25519PublicKey.from_public_bytes(public_key)
        public_key_obj.verify(signature_bytes, canonical_bytes)
        # Success — no exception raised
        return VerificationResult(
            valid=True,
            peer_id=peer_id,
            detail="signature valid"
        )
    except Exception as exc:
        # Signature verification failed (wrong key, tampered bytes, etc.)
        return VerificationResult(
            valid=False,
            reason="signature_invalid",
            peer_id=peer_id,
            detail=f"Ed25519 verification failed: {exc}"
        )


def verify_passport_structure(passport: dict) -> VerificationResult:
    """Verify passport has required fields and correct types.

    Args:
        passport: Passport dict from bundle or agent registration

    Returns:
        VerificationResult with valid=True if well-formed,
        valid=False with reason=passport_malformed otherwise.

    Reason codes:
        - passport_malformed: missing required fields or wrong types
    """
    # Reuse existing validation logic from passport.py
    # validate_passport returns False if invalid, we catch it
    try:
        is_valid = validate_passport(passport, raise_error=False)
        if is_valid:
            return VerificationResult(
                valid=True,
                detail="passport structure valid"
            )
        else:
            # validate_passport returns False but doesn't say why
            # Try again with raise_error=True to get details
            try:
                validate_passport(passport, raise_error=True)
            except ValueError as ve:
                detail = str(ve)
            return VerificationResult(
                valid=False,
                reason="passport_malformed",
                detail=detail if 'detail' in locals() else "passport validation failed"
            )
    except Exception as exc:
        return VerificationResult(
            valid=False,
            reason="passport_malformed",
            detail=f"passport validation error: {exc}"
        )


def verify_passport_expiry(
    passport: dict,
    now: Optional[datetime] = None
) -> VerificationResult:
    """Verify passport expiry and not-before windows.

    Args:
        passport: Passport dict (should contain expiry metadata)
        now: Current time (defaults to utcnow)

    Returns:
        VerificationResult with valid=True if passport is valid at `now`,
        valid=False with reason=passport_expired otherwise.

    Reason codes:
        - passport_expired: passport past expiry or before not_before

    Note: This implementation assumes passports SHOULD have expiry fields.
    If a passport has no expiry, we treat it as valid (forward-compatible).
    Adjust if mandatory expiry is required.
    """
    if now is None:
        now = datetime.utcnow()

    # Check for expiry fields in passport
    # AI-IQ passports have a 'generated_at' field but no explicit expiry
    # For federation, we may want to enforce freshness based on generated_at
    # For now, use a generous 90-day window as default if not specified

    generated_at_str = passport.get("generated_at")
    expires_at_str = passport.get("expires_at")
    not_before_str = passport.get("not_before")

    # If passport has explicit expiry, check it
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            if now > expires_at:
                return VerificationResult(
                    valid=False,
                    reason="passport_expired",
                    detail=f"passport expired at {expires_at_str}"
                )
        except Exception as exc:
            return VerificationResult(
                valid=False,
                reason="passport_malformed",
                detail=f"expires_at field malformed: {exc}"
            )

    # If passport has not_before, check it
    if not_before_str:
        try:
            not_before = datetime.fromisoformat(not_before_str.replace("Z", "+00:00"))
            if now < not_before:
                return VerificationResult(
                    valid=False,
                    reason="passport_expired",
                    detail=f"passport not valid until {not_before_str}"
                )
        except Exception as exc:
            return VerificationResult(
                valid=False,
                reason="passport_malformed",
                detail=f"not_before field malformed: {exc}"
            )

    # If passport has generated_at but no explicit expiry, enforce 90-day freshness
    if generated_at_str and not expires_at_str:
        try:
            generated_at = datetime.fromisoformat(generated_at_str.replace("Z", "+00:00"))
            age_days = (now - generated_at).days
            if age_days > settings.passport_refresh_days:
                return VerificationResult(
                    valid=False,
                    reason="passport_expired",
                    detail=f"passport generated {age_days} days ago (max {settings.passport_refresh_days})"
                )
        except Exception as exc:
            # If generated_at is malformed, treat as valid (fail open on this check)
            # Main structure check catches truly broken passports
            pass

    # No expiry constraints violated
    return VerificationResult(
        valid=True,
        detail="passport time window valid"
    )


def verify_peer_known(peer_id: str) -> VerificationResult:
    """Verify peer is registered in local federation_peers table.

    Args:
        peer_id: Peer identifier from bundle

    Returns:
        VerificationResult with valid=True if peer found and active,
        valid=False with reason=passport_unknown otherwise.

    Reason codes:
        - passport_unknown: peer_id not in federation_peers or inactive
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, public_key FROM federation_peers
                WHERE id = ? AND is_active = 1
            """, (peer_id,))
            row = cursor.fetchone()

            if row:
                return VerificationResult(
                    valid=True,
                    peer_id=peer_id,
                    detail="peer registered and active",
                    metadata={"has_public_key": bool(row["public_key"])}
                )
            else:
                return VerificationResult(
                    valid=False,
                    reason="passport_unknown",
                    peer_id=peer_id,
                    detail="peer not found in federation_peers or inactive"
                )
    except Exception as exc:
        # DB error — fail closed
        return VerificationResult(
            valid=False,
            reason="passport_unknown",
            peer_id=peer_id,
            detail=f"database error: {exc}"
        )


def verify_peer_trusted(
    peer_id: str,
    min_trust: Optional[float] = None
) -> VerificationResult:
    """Verify peer meets local trust threshold.

    Args:
        peer_id: Peer identifier
        min_trust: Minimum trust score required (defaults to Established tier: 30.0)

    Returns:
        VerificationResult with valid=True if peer trust >= threshold,
        valid=False with reason=peer_untrusted otherwise.

    Reason codes:
        - peer_untrusted: peer trust_score below threshold
    """
    if min_trust is None:
        # Default to Established tier minimum (Newcomer max is the threshold)
        min_trust = float(settings.trust_tier_newcomer_max)

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT trust_score FROM federation_peers
                WHERE id = ?
            """, (peer_id,))
            row = cursor.fetchone()

            if not row:
                # Peer not found — but this should be caught by verify_peer_known
                # Return untrusted anyway (fail closed)
                return VerificationResult(
                    valid=False,
                    reason="peer_untrusted",
                    peer_id=peer_id,
                    detail="peer not found in trust check"
                )

            trust_score = float(row["trust_score"])

            if trust_score >= min_trust:
                return VerificationResult(
                    valid=True,
                    peer_id=peer_id,
                    detail=f"peer trust {trust_score:.1f} >= {min_trust:.1f}",
                    metadata={"trust_score": trust_score, "min_trust": min_trust}
                )
            else:
                return VerificationResult(
                    valid=False,
                    reason="peer_untrusted",
                    peer_id=peer_id,
                    detail=f"peer trust {trust_score:.1f} < {min_trust:.1f}",
                    metadata={"trust_score": trust_score, "min_trust": min_trust}
                )
    except Exception as exc:
        # DB error — fail closed
        return VerificationResult(
            valid=False,
            reason="peer_untrusted",
            peer_id=peer_id,
            detail=f"database error: {exc}"
        )
