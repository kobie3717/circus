"""Preference application service for consuming bots (Week 4 sub-step 4.2).

This module provides the read path for bots to fetch their active preferences.

Read path contract:
- Query active_preferences for a given owner_id
- Return {field_name: value} dict
- Re-check allowlist on consume-side (defense in depth)
"""

import logging
import os
import sqlite3

from circus.services.preference_constants import ALLOWLISTED_PREFERENCE_FIELDS

logger = logging.getLogger(__name__)


def get_active_preferences(conn: sqlite3.Connection, owner_id: str) -> dict[str, str]:
    """Get active preferences for a given owner.

    Queries active_preferences table and returns {field_name: value} dict.
    Re-checks allowlist on consume-side (defense in depth against malicious federation).

    Args:
        conn: Database connection
        owner_id: Owner identifier (e.g., "kobus")

    Returns:
        Dict mapping field_name to value (e.g., {"user.language_preference": "af"})
        Empty dict if no active preferences found.

    Side effects:
        - Logs WARNING if any field_name is not in allowlist (indicates schema drift or attack)
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT field_name, value
        FROM active_preferences
        WHERE owner_id = ?
        """,
        (owner_id,),
    )

    rows = cursor.fetchall()
    result = {}

    for field_name, value in rows:
        # Defense in depth: re-check allowlist
        if field_name not in ALLOWLISTED_PREFERENCE_FIELDS:
            logger.warning(
                "Consume-side allowlist violation detected",
                extra={
                    "owner_id": owner_id,
                    "field_name": field_name,
                    "value": value,
                    "message": "Field not in ALLOWLISTED_PREFERENCE_FIELDS — skipping",
                },
            )
            continue

        result[field_name] = value

    return result


def get_active_preferences_for_env(conn: sqlite3.Connection) -> dict[str, str]:
    """Convenience wrapper: get active preferences for CIRCUS_OWNER_ID from env.

    Reads owner_id from CIRCUS_OWNER_ID env var and delegates to get_active_preferences.

    Args:
        conn: Database connection

    Returns:
        Dict mapping field_name to value (same as get_active_preferences)
        Empty dict if CIRCUS_OWNER_ID not set or no preferences found.
    """
    owner_id = os.getenv("CIRCUS_OWNER_ID", "")
    if not owner_id:
        logger.warning("CIRCUS_OWNER_ID not set — returning empty preferences dict")
        return {}

    return get_active_preferences(conn, owner_id)
