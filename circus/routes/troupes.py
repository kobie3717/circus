"""Troupe membership management endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from datetime import datetime
import sqlite3

from circus.database import get_db
from circus.routes.agents import verify_token

router = APIRouter(prefix="/api/v1/troupes", tags=["troupes"])


@router.post("/{troupe_id}/join")
async def join_troupe(
    troupe_id: str,
    agent_id: str = Depends(verify_token)
):
    """Join a troupe for scoped memory sharing. Requires valid agent token."""
    with get_db() as conn:
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
            return {"status": "already_member", "troupe_id": troupe_id, "agent_id": agent_id}


@router.delete("/{troupe_id}/leave")
async def leave_troupe(
    troupe_id: str,
    agent_id: str = Depends(verify_token)
):
    """Leave a troupe."""
    with get_db() as conn:
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
    agent_id: str = Depends(verify_token)
):
    """List members of a troupe. Only members can list."""
    with get_db() as conn:
        cursor = conn.cursor()
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
