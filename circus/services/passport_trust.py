"""Passport trust multiplier for preference confidence adjustment."""

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


def get_passport_multiplier(conn: sqlite3.Connection, agent_id: str) -> float:
    """
    Look up agent's passport score and return a confidence multiplier.

    Returns:
        1.10 if passport_score >= 80
        1.00 if passport_score >= 50
        0.90 if passport_score >= 20
        0.85 if no passport or score < 20
    """
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT passport_score FROM passports WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
            (agent_id,)
        )
        row = cursor.fetchone()
        if not row:
            return 0.85  # No passport = lower trust

        score = float(row[0])
        if score >= 80:
            return 1.10
        elif score >= 50:
            return 1.00
        elif score >= 20:
            return 0.90
        else:
            return 0.85
    except Exception as e:
        logger.warning(f"Passport lookup error (non-fatal): {e}")
        return 1.0  # Fail open — don't penalize on error


def apply_passport_trust(conn: sqlite3.Connection, agent_id: str, confidence: float) -> float:
    """Apply passport multiplier to confidence. Always returns float in [0.0, 1.0]."""
    multiplier = get_passport_multiplier(conn, agent_id)
    adjusted = confidence * multiplier
    result = max(0.0, min(1.0, adjusted))
    if multiplier != 1.0:
        logger.info(
            "passport_trust_applied",
            extra={
                "agent_id": agent_id,
                "multiplier": multiplier,
                "original_confidence": confidence,
                "adjusted_confidence": result,
            }
        )
    return result
