"""Preference admission service for behavior-delta memories (Week 4 sub-step 4.2).

This module controls the consume-side admission gate: when a valid preference memory
is published (or federated in), decide whether to activate it (write to active_preferences)
based on same-owner enforcement and confidence threshold (4.3).

Trust gates enforced here:
- Same-owner check: provenance.owner_id == CIRCUS_OWNER_ID (consume-side)
- (4.3 will add: effective_confidence >= 0.7)

Structured logging for operational visibility:
- Every skip emits INFO log with reason code (same_owner_failed, provenance_invalid, etc.)
"""

import logging
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

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
) -> bool:
    """Admit a preference memory to active_preferences (if gates pass).

    This is the consume-side trust gate. Checks:
    1. Server owner configured (CIRCUS_OWNER_ID env var)
    2. Same-owner match (owner_id == server's CIRCUS_OWNER_ID)
    3. TODO(4.3): effective_confidence >= 0.7 threshold

    Args:
        conn: Database connection (transaction-scoped, reused from publish route)
        memory_id: ID of the memory in shared_memories (for audit trail)
        owner_id: Owner ID from provenance.owner_id
        preference_field: Field name (e.g., "user.language_preference")
        preference_value: Preference value (e.g., "af")
        effective_confidence: Post-decay, post-trust-adjustment confidence
        now: Current timestamp (for updated_at)

    Returns:
        True if written to active_preferences, False if skipped

    Side effects:
        - On success: upserts row in active_preferences
        - On skip: logs INFO with structured reason code
    """
    # Gate 1: Server owner configured
    server_owner = _get_server_owner()
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
        return False

    # Gate 2: Same-owner check
    if owner_id != server_owner:
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
        return False

    # TODO(4.3): Add confidence threshold check here
    # if effective_confidence < settings.preference_activation_threshold:
    #     logger.info("preference_skipped", extra={"reason": "confidence_below_threshold", ...})
    #     return False

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

    return True
