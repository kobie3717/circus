"""Preference admission service for behavior-delta memories (Week 4 sub-steps 4.2-4.3, Week 5 5.4).

This module controls the consume-side admission gate: when a valid preference memory
is published (or federated in), decide whether to activate it (write to active_preferences)
based on same-owner enforcement and confidence threshold.

Trust gates enforced here (W4 + W5):
- Same-owner check: provenance.owner_id == CIRCUS_OWNER_ID (consume-side, W4 gate 4)
- Owner signature verify: cryptographic proof of owner authorization (W5 gate 5)
- Confidence threshold: effective_confidence >= preference_activation_threshold (W4 gate 6)

Structured logging for operational visibility:
- Every skip emits INFO log with reason code (same_owner_failed, owner_signature_invalid, etc.)
"""

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from circus.config import settings
from circus.services.owner_verification import verify_owner_binding

logger = logging.getLogger(__name__)


@dataclass
class PreferenceDecision:
    """Result of preference admission with gate-by-gate trace."""
    admitted: bool
    gates: list[dict]
    reason: str | None = None  # skip reason if not admitted
    field: str | None = None
    value: str | None = None

# Module-level cached owner ID + warning state
_SERVER_OWNER: str | None = None
_WARN_LOGGED = False


def _get_server_owner() -> str:
    """Get server's owner ID from CIRCUS_OWNER_ID env var.

    Caches the result on first call. Logs WARNING once if unset.
    Returns empty string if unset (admission will skip all preferences).
    """
    global _SERVER_OWNER, _WARN_LOGGED

    if _SERVER_OWNER is None:
        _SERVER_OWNER = os.getenv("CIRCUS_OWNER_ID", "")
        if not _SERVER_OWNER and not _WARN_LOGGED:
            logger.warning(
                "CIRCUS_OWNER_ID not set — no preference memories will be activated. "
                "Set CIRCUS_OWNER_ID=<owner> env var to enable preference admission."
            )
            _WARN_LOGGED = True

    return _SERVER_OWNER


def admit_preference(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    owner_id: str,
    preference_field: str,
    preference_value: str,
    effective_confidence: float,
    now: datetime,
    agent_id: str,
    shared_at: str,
    owner_binding: Optional[dict] = None,
) -> PreferenceDecision:
    """Admit a preference memory to active_preferences (if gates pass).

    This is the consume-side trust gate. Checks (W4 + W5):
    1. Server owner configured (CIRCUS_OWNER_ID env var)
    2. Same-owner match (owner_id == server's CIRCUS_OWNER_ID) — W4 gate 4
    3. Owner signature valid (verify_owner_binding) — W5 gate 5
    4. Confidence threshold (effective_confidence >= preference_activation_threshold) — W4 gate 6

    Args:
        conn: Database connection (transaction-scoped, reused from publish route)
        memory_id: ID of the memory in shared_memories (for audit trail)
        owner_id: Owner ID from provenance.owner_id
        preference_field: Field name (e.g., "user.language_preference")
        preference_value: Preference value (e.g., "af")
        effective_confidence: Post-decay, post-trust-adjustment confidence
        now: Current timestamp (for updated_at)
        agent_id: Publishing agent ID (from memory envelope)
        shared_at: ISO8601 timestamp when memory was shared
        owner_binding: Owner signature binding from provenance (W5)

    Returns:
        PreferenceDecision with gates trace and admission result

    Side effects:
        - On success: upserts row in active_preferences
        - On skip: logs INFO with structured reason code
    """
    gates = []
    threshold = settings.preference_activation_threshold

    # Gate 1: Server owner configured
    server_owner = _get_server_owner()
    gates.append({
        "gate": "server_owner_configured",
        "passed": bool(server_owner)
    })

    if not server_owner:
        logger.info(
            "preference_skipped",
            extra={
                "reason": "same_owner_failed",
                "memory_id": memory_id,
                "owner_id": owner_id,
                "field": preference_field,
                "effective_confidence": effective_confidence,
            },
        )
        return PreferenceDecision(
            admitted=False,
            gates=gates,
            reason="same_owner_failed",
            field=preference_field,
            value=preference_value
        )

    # Gate 2: Same-owner check
    owner_match = owner_id == server_owner
    gates.append({
        "gate": "same_owner_match",
        "passed": owner_match,
        "owner_id": owner_id if owner_match else None
    })

    if not owner_match:
        logger.info(
            "preference_skipped",
            extra={
                "reason": "same_owner_failed",
                "memory_id": memory_id,
                "owner_id": owner_id,
                "field": preference_field,
                "effective_confidence": effective_confidence,
            },
        )
        # Mark remaining gates as not evaluated
        gates.append({"gate": "owner_signature_valid", "passed": None})
        gates.append({"gate": "confidence_threshold", "passed": None})
        return PreferenceDecision(
            admitted=False,
            gates=gates,
            reason="same_owner_failed",
            field=preference_field,
            value=preference_value
        )

    # Gate 3 (W5): Owner signature verification
    # Defense in depth: check if owner_binding exists (should be validated at publish, but federation path might bypass)
    if not owner_binding:
        gates.append({"gate": "owner_signature_valid", "passed": False})
        logger.info(
            "preference_skipped",
            extra={
                "reason": "owner_signature_missing",
                "memory_id": memory_id,
                "owner_id": owner_id,
                "field": preference_field,
                "effective_confidence": effective_confidence,
            },
        )
        gates.append({"gate": "confidence_threshold", "passed": None})
        return PreferenceDecision(
            admitted=False,
            gates=gates,
            reason="owner_signature_missing",
            field=preference_field,
            value=preference_value
        )

    # Verify owner signature (cryptographic gate)
    result = verify_owner_binding(
        claimed_owner_id=owner_id,
        claimed_agent_id=owner_binding.get("agent_id", ""),
        claimed_memory_id=owner_binding.get("memory_id", ""),
        claimed_timestamp=owner_binding.get("timestamp", ""),
        signature_b64=owner_binding.get("signature", ""),
        shared_at=shared_at,
        conn=conn,
    )

    gates.append({"gate": "owner_signature_valid", "passed": result.valid})

    if not result.valid:
        logger.info(
            "preference_skipped",
            extra={
                "reason": result.reason,  # Propagate verifier reason code directly
                "memory_id": memory_id,
                "owner_id": owner_id,
                "field": preference_field,
                "effective_confidence": effective_confidence,
            },
        )
        gates.append({"gate": "confidence_threshold", "passed": None})
        return PreferenceDecision(
            admitted=False,
            gates=gates,
            reason=result.reason,
            field=preference_field,
            value=preference_value
        )

    # Gate 4: Confidence threshold check
    confidence_pass = effective_confidence >= threshold
    gates.append({
        "gate": "confidence_threshold",
        "passed": confidence_pass,
        "value": float(effective_confidence),
        "threshold": float(threshold)
    })

    if not confidence_pass:
        logger.info(
            "preference_skipped",
            extra={
                "reason": "confidence_below_threshold",
                "memory_id": memory_id,
                "owner_id": owner_id,
                "field": preference_field,
                "effective_confidence": float(effective_confidence),
                "threshold": float(threshold),
            },
        )
        return PreferenceDecision(
            admitted=False,
            gates=gates,
            reason="confidence_below_threshold",
            field=preference_field,
            value=preference_value
        )

    # All gates passed — upsert to active_preferences
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO active_preferences (owner_id, field_name, value, source_memory_id, effective_confidence, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner_id, field_name)
        DO UPDATE SET
            value = excluded.value,
            source_memory_id = excluded.source_memory_id,
            effective_confidence = excluded.effective_confidence,
            updated_at = excluded.updated_at
        """,
        (owner_id, preference_field, preference_value, memory_id, effective_confidence, now.isoformat()),
    )

    return PreferenceDecision(
        admitted=True,
        gates=gates,
        reason=None,
        field=preference_field,
        value=preference_value
    )
