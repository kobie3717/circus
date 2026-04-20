"""Conflict detection and resolution service for W7.

This module implements confidence-weighted conflict resolution for preference memories.
When two preferences conflict (same owner + field), the one with higher confidence wins.

Resolution rules:
- No existing preference → no conflict, new wins
- Same value → no conflict (idempotent update, refresh timestamp)
- Different value, new_confidence > existing by >0.05 → new wins
- Different value, existing_confidence > new by >0.05 → existing wins
- Within 0.05 of each other → new wins (tie-break: recency)
"""

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ConflictResult:
    """Result of conflict detection and resolution."""
    has_conflict: bool
    existing_value: str | None
    existing_confidence: float | None
    existing_memory_id: str | None
    resolution: str  # "new_wins" | "existing_wins" | "no_conflict"
    reason: str  # human-readable explanation


def detect_and_resolve_conflict(
    conn: sqlite3.Connection,
    owner_id: str,
    field_name: str,
    new_value: str,
    new_confidence: float,
) -> ConflictResult:
    """Detect and resolve preference conflicts using confidence-weighted rules.

    Resolution logic:
    1. No existing preference → no_conflict, new_wins
    2. Existing preference with same value → no_conflict, new_wins (idempotent refresh)
    3. Different value:
       - new_confidence > existing + 0.05 → new_wins
       - existing_confidence > new + 0.05 → existing_wins
       - within 0.05 → new_wins (tie-break: recency)

    Args:
        conn: Database connection
        owner_id: Owner identifier
        field_name: Preference field name
        new_value: New preference value
        new_confidence: New preference confidence

    Returns:
        ConflictResult with resolution decision and metadata
    """
    cursor = conn.cursor()

    # Check for existing preference
    cursor.execute(
        """
        SELECT value, effective_confidence, source_memory_id
        FROM active_preferences
        WHERE owner_id = ? AND field_name = ?
        """,
        (owner_id, field_name)
    )

    row = cursor.fetchone()

    # Case 1: No existing preference
    if not row:
        return ConflictResult(
            has_conflict=False,
            existing_value=None,
            existing_confidence=None,
            existing_memory_id=None,
            resolution="new_wins",
            reason="no existing preference"
        )

    existing_value, existing_confidence, existing_memory_id = row

    # Case 2: Same value (idempotent update)
    if existing_value == new_value:
        return ConflictResult(
            has_conflict=False,
            existing_value=existing_value,
            existing_confidence=existing_confidence,
            existing_memory_id=existing_memory_id,
            resolution="new_wins",
            reason="idempotent update (same value, refreshing timestamp)"
        )

    # Case 3: Different values — confidence-weighted resolution
    CONFIDENCE_THRESHOLD = 0.05
    confidence_diff = new_confidence - existing_confidence

    if confidence_diff > CONFIDENCE_THRESHOLD:
        # New has significantly higher confidence
        return ConflictResult(
            has_conflict=True,
            existing_value=existing_value,
            existing_confidence=existing_confidence,
            existing_memory_id=existing_memory_id,
            resolution="new_wins",
            reason=f"new confidence ({new_confidence:.2f}) exceeds existing ({existing_confidence:.2f}) by {confidence_diff:.2f}"
        )
    elif confidence_diff < -CONFIDENCE_THRESHOLD:
        # Existing has significantly higher confidence
        return ConflictResult(
            has_conflict=True,
            existing_value=existing_value,
            existing_confidence=existing_confidence,
            existing_memory_id=existing_memory_id,
            resolution="existing_wins",
            reason=f"existing confidence ({existing_confidence:.2f}) exceeds new ({new_confidence:.2f}) by {-confidence_diff:.2f}"
        )
    else:
        # Within threshold — tie-break by recency
        return ConflictResult(
            has_conflict=True,
            existing_value=existing_value,
            existing_confidence=existing_confidence,
            existing_memory_id=existing_memory_id,
            resolution="new_wins",
            reason=f"confidence within threshold ({confidence_diff:+.2f}), recency wins"
        )
