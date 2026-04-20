"""Quarantine service for borderline preferences (W11).

Memories that pass most gates but have borderline confidence (0.5-0.69) are
quarantined for operator review instead of being silently discarded.
"""

import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class QuarantineEntry:
    """Quarantined memory entry."""
    id: str
    memory_id: str
    owner_id: str
    reason: str
    quarantined_at: str
    released_at: Optional[str] = None
    released_by: Optional[str] = None
    release_reason: Optional[str] = None
    auto_release_at: Optional[str] = None


def generate_quarantine_id() -> str:
    """Generate quarantine ID: quar-<hex16>."""
    return f"quar-{secrets.token_hex(8)}"


def quarantine_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    owner_id: str,
    reason: str,
    auto_release_at: Optional[str] = None,
) -> str:
    """Add memory to quarantine.

    Args:
        conn: Database connection
        memory_id: ID of shared_memory
        owner_id: Owner ID from provenance
        reason: Quarantine reason code
        auto_release_at: Optional auto-release timestamp

    Returns:
        Quarantine entry ID
    """
    cursor = conn.cursor()
    quar_id = generate_quarantine_id()
    now = datetime.utcnow().isoformat() + "Z"

    cursor.execute(
        """
        INSERT INTO quarantine (id, memory_id, owner_id, reason, quarantined_at, auto_release_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (quar_id, memory_id, owner_id, reason, now, auto_release_at)
    )

    # Write to governance audit log
    write_audit_event(
        conn,
        event_type="quarantine_created",
        actor="system",
        owner_id=owner_id,
        detail=json.dumps({
            "quarantine_id": quar_id,
            "memory_id": memory_id,
            "reason": reason,
        })
    )

    logger.info(
        "memory_quarantined",
        extra={
            "quarantine_id": quar_id,
            "memory_id": memory_id,
            "owner_id": owner_id,
            "reason": reason,
        }
    )

    return quar_id


def list_quarantined(
    conn: sqlite3.Connection,
    owner_id: Optional[str] = None,
    include_released: bool = False,
) -> list[QuarantineEntry]:
    """List quarantined memories.

    Args:
        conn: Database connection
        owner_id: Filter by owner (optional)
        include_released: Include already-released entries

    Returns:
        List of quarantine entries
    """
    cursor = conn.cursor()

    query = "SELECT id, memory_id, owner_id, reason, quarantined_at, released_at, released_by, release_reason, auto_release_at FROM quarantine"
    params = []

    conditions = []
    if owner_id:
        conditions.append("owner_id = ?")
        params.append(owner_id)

    if not include_released:
        conditions.append("released_at IS NULL")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY quarantined_at DESC"

    cursor.execute(query, params)
    rows = cursor.fetchall()

    return [
        QuarantineEntry(
            id=row[0],
            memory_id=row[1],
            owner_id=row[2],
            reason=row[3],
            quarantined_at=row[4],
            released_at=row[5],
            released_by=row[6],
            release_reason=row[7],
            auto_release_at=row[8],
        )
        for row in rows
    ]


def release_from_quarantine(
    conn: sqlite3.Connection,
    quarantine_id: str,
    released_by: str,
    release_reason: str,
) -> bool:
    """Release a memory from quarantine.

    Args:
        conn: Database connection
        quarantine_id: Quarantine entry ID
        released_by: Agent ID or "operator"
        release_reason: Human-readable reason

    Returns:
        True if released, False if not found
    """
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat() + "Z"

    cursor.execute(
        """
        UPDATE quarantine
        SET released_at = ?, released_by = ?, release_reason = ?
        WHERE id = ? AND released_at IS NULL
        """,
        (now, released_by, release_reason, quarantine_id)
    )

    if cursor.rowcount == 0:
        return False

    # Get owner_id for audit log
    cursor.execute("SELECT owner_id, memory_id FROM quarantine WHERE id = ?", (quarantine_id,))
    row = cursor.fetchone()
    owner_id = row[0] if row else None
    memory_id = row[1] if row else None

    # Write to governance audit log
    write_audit_event(
        conn,
        event_type="quarantine_released",
        actor=released_by,
        owner_id=owner_id,
        detail=json.dumps({
            "quarantine_id": quarantine_id,
            "memory_id": memory_id,
            "reason": release_reason,
        })
    )

    logger.info(
        "quarantine_released",
        extra={
            "quarantine_id": quarantine_id,
            "released_by": released_by,
            "reason": release_reason,
        }
    )

    return True


def discard_from_quarantine(
    conn: sqlite3.Connection,
    quarantine_id: str,
    discarded_by: str,
) -> bool:
    """Discard a quarantined memory (mark as released without admission).

    Args:
        conn: Database connection
        quarantine_id: Quarantine entry ID
        discarded_by: Agent ID or "operator"

    Returns:
        True if discarded, False if not found
    """
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat() + "Z"

    cursor.execute(
        """
        UPDATE quarantine
        SET released_at = ?, released_by = ?, release_reason = ?
        WHERE id = ? AND released_at IS NULL
        """,
        (now, discarded_by, "discarded_by_operator", quarantine_id)
    )

    if cursor.rowcount == 0:
        return False

    # Get owner_id for audit log
    cursor.execute("SELECT owner_id, memory_id FROM quarantine WHERE id = ?", (quarantine_id,))
    row = cursor.fetchone()
    owner_id = row[0] if row else None
    memory_id = row[1] if row else None

    # Write to governance audit log
    write_audit_event(
        conn,
        event_type="quarantine_discarded",
        actor=discarded_by,
        owner_id=owner_id,
        detail=json.dumps({
            "quarantine_id": quarantine_id,
            "memory_id": memory_id,
        })
    )

    logger.info(
        "quarantine_discarded",
        extra={
            "quarantine_id": quarantine_id,
            "discarded_by": discarded_by,
        }
    )

    return True


def write_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    actor: Optional[str],
    owner_id: Optional[str],
    detail: str,
) -> str:
    """Write governance audit event.

    Args:
        conn: Database connection
        event_type: Event type code
        actor: Agent ID or "operator"
        owner_id: Owner affected
        detail: JSON-encoded detail object

    Returns:
        Audit event ID
    """
    cursor = conn.cursor()
    audit_id = f"audt-{secrets.token_hex(8)}"
    now = datetime.utcnow().isoformat() + "Z"

    cursor.execute(
        """
        INSERT INTO governance_audit (id, event_type, actor, owner_id, detail, happened_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (audit_id, event_type, actor, owner_id, detail, now)
    )

    return audit_id


def get_audit_log(
    conn: sqlite3.Connection,
    owner_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Get governance audit log.

    Args:
        conn: Database connection
        owner_id: Filter by owner (optional)
        limit: Max entries to return

    Returns:
        List of audit events (most recent first)
    """
    cursor = conn.cursor()

    query = """
        SELECT id, event_type, actor, owner_id, detail, happened_at
        FROM governance_audit
    """

    params = []
    if owner_id:
        query += " WHERE owner_id = ?"
        params.append(owner_id)

    query += " ORDER BY happened_at DESC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()

    return [
        {
            "id": row[0],
            "event_type": row[1],
            "actor": row[2],
            "owner_id": row[3],
            "detail": json.loads(row[4]) if row[4] else None,
            "happened_at": row[5],
        }
        for row in rows
    ]
