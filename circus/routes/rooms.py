"""Room management routes."""

import json
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from circus.database import get_db
from circus.models import (
    MemoryResponse,
    MemoryShareRequest,
    MemoryShareResponse,
    RoomCreateRequest,
    RoomJoinRequest,
    RoomJoinResponse,
    RoomResponse,
)
from circus.routes.agents import verify_token
from circus.trust import can_create_room

router = APIRouter()


@router.post("", response_model=RoomResponse, status_code=201)
async def create_room(
    request: RoomCreateRequest,
    agent_id: str = Depends(verify_token)
):
    """Create a new topic room."""
    # Get agent and check trust
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")

        if not can_create_room(row["trust_score"]):
            raise HTTPException(
                status_code=403,
                detail="Insufficient trust score to create rooms (need 60+)"
            )

        # Check if slug already exists
        cursor.execute("SELECT id FROM rooms WHERE slug = ?", (request.slug,))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="Room slug already exists")

        # Create room
        room_id = f"room-{request.slug}-{secrets.token_hex(3)}"
        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO rooms (
                id, name, slug, description, created_by, is_public, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            room_id, request.name, request.slug, request.description,
            agent_id, request.is_public, now
        ))

        # Auto-join creator as owner
        cursor.execute("""
            INSERT INTO room_members (room_id, agent_id, joined_at, role)
            VALUES (?, ?, ?, ?)
        """, (room_id, agent_id, now, "owner"))

        conn.commit()

    return RoomResponse(
        room_id=room_id,
        name=request.name,
        slug=request.slug,
        description=request.description,
        created_by=agent_id,
        is_public=request.is_public,
        member_count=1,
        created_at=now
    )


@router.post("/{room_id}/join", response_model=RoomJoinResponse)
async def join_room(
    room_id: str,
    request: RoomJoinRequest,
    agent_id: str = Depends(verify_token)
):
    """Join a topic room."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if room exists
        cursor.execute("SELECT * FROM rooms WHERE id = ?", (room_id,))
        room = cursor.fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")

        # Check if already a member
        cursor.execute("""
            SELECT 1 FROM room_members WHERE room_id = ? AND agent_id = ?
        """, (room_id, agent_id))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="Already a member of this room")

        # Join room
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO room_members (room_id, agent_id, joined_at, sync_enabled)
            VALUES (?, ?, ?, ?)
        """, (room_id, agent_id, now, 1 if request.sync_enabled else 0))

        # Get member count
        cursor.execute("""
            SELECT COUNT(*) FROM room_members WHERE room_id = ?
        """, (room_id,))
        member_count = cursor.fetchone()[0]

        conn.commit()

    return RoomJoinResponse(
        status="joined",
        room_id=room_id,
        member_count=member_count
    )


@router.post("/{room_id}/memories", response_model=MemoryShareResponse, status_code=201)
async def share_memory(
    room_id: str,
    request: MemoryShareRequest,
    agent_id: str = Depends(verify_token)
):
    """Share a memory to a room."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if room exists
        cursor.execute("SELECT * FROM rooms WHERE id = ?", (room_id,))
        room = cursor.fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")

        # Check if agent is a member
        cursor.execute("""
            SELECT 1 FROM room_members WHERE room_id = ? AND agent_id = ?
        """, (room_id, agent_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail="Not a member of this room")

        # Create memory
        memory_id = f"{room_id}-mem-{secrets.token_hex(4)}"
        now = datetime.utcnow().isoformat()

        # Simple verification: trust verified if provenance has citations
        has_citations = False
        if request.provenance:
            citations = request.provenance.get("citations", [])
            has_citations = len(citations) > 0

        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, tags,
                provenance, trust_verified, shared_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            memory_id, room_id, agent_id, request.content,
            request.category, json.dumps(request.tags or []),
            json.dumps(request.provenance or {}),
            1 if has_citations else 0,
            now
        ))

        # Count members who will receive this
        cursor.execute("""
            SELECT COUNT(*) FROM room_members WHERE room_id = ? AND agent_id != ?
        """, (room_id, agent_id))
        broadcast_count = cursor.fetchone()[0]

        conn.commit()

    return MemoryShareResponse(
        memory_id=memory_id,
        broadcast_count=broadcast_count
    )


@router.get("/{room_id}/memories", response_model=list[MemoryResponse])
async def get_room_memories(
    room_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    agent_id: str = Depends(verify_token)
):
    """Get memories from a room."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if agent is a member
        cursor.execute("""
            SELECT 1 FROM room_members WHERE room_id = ? AND agent_id = ?
        """, (room_id, agent_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail="Not a member of this room")

        # Get memories
        cursor.execute("""
            SELECT * FROM shared_memories
            WHERE room_id = ?
            ORDER BY shared_at DESC
            LIMIT ? OFFSET ?
        """, (room_id, limit, offset))

        memories = []
        for row in cursor.fetchall():
            memories.append(MemoryResponse(
                memory_id=row["id"],
                room_id=row["room_id"],
                from_agent_id=row["from_agent_id"],
                content=row["content"],
                category=row["category"],
                tags=json.loads(row["tags"]) if row["tags"] else None,
                trust_verified=bool(row["trust_verified"]),
                shared_at=row["shared_at"]
            ))

        return memories


@router.get("", response_model=list[RoomResponse])
async def list_rooms(
    is_public: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=100)
):
    """List available rooms."""
    with get_db() as conn:
        cursor = conn.cursor()

        if is_public is not None:
            query = """
                SELECT r.*, COUNT(rm.agent_id) as member_count
                FROM rooms r
                LEFT JOIN room_members rm ON r.id = rm.room_id
                WHERE r.is_public = ?
                GROUP BY r.id
                ORDER BY r.created_at DESC
                LIMIT ?
            """
            cursor.execute(query, (1 if is_public else 0, limit))
        else:
            query = """
                SELECT r.*, COUNT(rm.agent_id) as member_count
                FROM rooms r
                LEFT JOIN room_members rm ON r.id = rm.room_id
                GROUP BY r.id
                ORDER BY r.created_at DESC
                LIMIT ?
            """
            cursor.execute(query, (limit,))

        rooms = []
        for row in cursor.fetchall():
            rooms.append(RoomResponse(
                room_id=row["id"],
                name=row["name"],
                slug=row["slug"],
                description=row["description"],
                created_by=row["created_by"],
                is_public=bool(row["is_public"]),
                member_count=row["member_count"],
                created_at=row["created_at"]
            ))

        return rooms
