"""Troupe membership management endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Header
from typing import Optional
from datetime import datetime
import sqlite3

from circus.database import get_db

router = APIRouter(prefix="/api/v1/troupes", tags=["troupes"])


def _get_agent_from_token(authorization: Optional[str], conn) -> Optional[str]:
    """Extract agent_id from Bearer token. Returns None if invalid."""
    if not authorization or not authorization.startswith('Bearer '):
        return None
    token = authorization.removeprefix('Bearer ')
    cursor = conn.cursor()
    cursor.execute(
        'SELECT agent_id FROM active_tokens WHERE token = ? AND expires_at > ?',
        (token, datetime.utcnow().isoformat())
    )
    row = cursor.fetchone()
    return row[0] if row else None


@router.post("/{troupe_id}/join")
async def join_troupe(
    troupe_id: str,
    authorization: Optional[str] = Header(None)
):
    """Join a troupe for scoped memory sharing. Requires valid agent token."""
    with get_db() as conn:
        agent_id = _get_agent_from_token(authorization, conn)
        if not agent_id:
            raise HTTPException(status_code=401, detail="Valid agent token required")

        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        try:
            cursor.execute(
                "INSERT INTO troupe_members (troupe_id, agent_id, joined_at) VALUES (?, ?, ?)",
                (troupe_id, agent_id, now)
            )
            conn.commit()
            return {"status": "joined", "troupe_id": troupe_id, "agent_id": agent_id}
        except sqlite3.IntegrityError:
            # UNIQUE constraint — already a member
            return {"status": "already_member", "troupe_id": troupe_id, "agent_id": agent_id}


@router.delete("/{troupe_id}/leave")
async def leave_troupe(
    troupe_id: str,
    authorization: Optional[str] = Header(None)
):
    """Leave a troupe."""
    with get_db() as conn:
        agent_id = _get_agent_from_token(authorization, conn)
        if not agent_id:
            raise HTTPException(status_code=401, detail="Valid agent token required")

        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM troupe_members WHERE troupe_id = ? AND agent_id = ?",
            (troupe_id, agent_id)
        )
        conn.commit()
        return {"status": "left", "troupe_id": troupe_id}


@router.get("/{troupe_id}/members")
async def list_troupe_members(
    troupe_id: str,
    authorization: Optional[str] = Header(None)
):
    """List members of a troupe. Only members can list."""
    with get_db() as conn:
        agent_id = _get_agent_from_token(authorization, conn)
        if not agent_id:
            raise HTTPException(status_code=401, detail="Valid agent token required")

        cursor = conn.cursor()
        # Check requester is a member
        cursor.execute(
            "SELECT 1 FROM troupe_members WHERE troupe_id = ? AND agent_id = ?",
            (troupe_id, agent_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail="Not a member of this troupe")

        cursor.execute(
            "SELECT agent_id, joined_at FROM troupe_members WHERE troupe_id = ? ORDER BY joined_at",
            (troupe_id,)
        )
        members = [{"agent_id": r[0], "joined_at": r[1]} for r in cursor.fetchall()]
        return {"troupe_id": troupe_id, "members": members}
