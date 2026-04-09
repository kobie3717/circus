"""Server-Sent Events for real-time room activity."""

import asyncio
import json
from datetime import datetime
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from circus.database import get_db
from circus.routes.agents import verify_token

router = APIRouter()


async def room_event_stream(
    room_id: str,
    agent_id: str,
    last_event_id: str | None = None
) -> AsyncGenerator[dict, None]:
    """
    Stream room events (new memories, member joins, etc.).

    In production, this would use Redis pub/sub or similar.
    For now, we poll the database every 5 seconds.
    """
    # Verify agent is member of room
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 1 FROM room_members WHERE room_id = ? AND agent_id = ?
        """, (room_id, agent_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail="Not a member of this room")

    # Get last seen timestamp
    if last_event_id:
        try:
            last_seen = datetime.fromisoformat(last_event_id)
        except ValueError:
            last_seen = datetime.utcnow()
    else:
        last_seen = datetime.utcnow()

    # Send initial connection event
    yield {
        "event": "connected",
        "id": datetime.utcnow().isoformat(),
        "data": json.dumps({
            "type": "connected",
            "room_id": room_id,
            "agent_id": agent_id,
            "timestamp": datetime.utcnow().isoformat()
        })
    }

    while True:
        with get_db() as conn:
            cursor = conn.cursor()

            # Get new memories since last_seen
            cursor.execute("""
                SELECT id, from_agent_id, content, category, shared_at
                FROM shared_memories
                WHERE room_id = ? AND shared_at > ?
                ORDER BY shared_at ASC
                LIMIT 50
            """, (room_id, last_seen.isoformat()))

            memories = cursor.fetchall()

            for memory in memories:
                event_data = {
                    "type": "memory_shared",
                    "memory_id": memory["id"],
                    "from_agent_id": memory["from_agent_id"],
                    "content": memory["content"],
                    "category": memory["category"],
                    "shared_at": memory["shared_at"]
                }

                yield {
                    "event": "memory",
                    "id": memory["shared_at"],
                    "data": json.dumps(event_data)
                }

                last_seen = datetime.fromisoformat(memory["shared_at"])

            # Check for new room members (agent_joined event)
            cursor.execute("""
                SELECT agent_id, joined_at
                FROM room_members
                WHERE room_id = ? AND joined_at > ?
                ORDER BY joined_at ASC
                LIMIT 20
            """, (room_id, last_seen.isoformat()))

            new_members = cursor.fetchall()

            for member in new_members:
                # Don't send join event for the current agent's own join
                if member["agent_id"] != agent_id:
                    event_data = {
                        "type": "agent_joined",
                        "agent_id": member["agent_id"],
                        "joined_at": member["joined_at"]
                    }

                    yield {
                        "event": "agent_joined",
                        "id": member["joined_at"],
                        "data": json.dumps(event_data)
                    }

                joined_time = datetime.fromisoformat(member["joined_at"])
                if joined_time > last_seen:
                    last_seen = joined_time

        # Send heartbeat every poll cycle
        yield {
            "event": "heartbeat",
            "id": datetime.utcnow().isoformat(),
            "data": json.dumps({
                "type": "heartbeat",
                "timestamp": datetime.utcnow().isoformat()
            })
        }

        # Wait 5 seconds before next poll
        await asyncio.sleep(5)


@router.get("/rooms/{room_id}/stream")
async def stream_room_events(
    room_id: str,
    agent_id: str = Depends(verify_token),
    last_event_id: str = Query(None)
):
    """
    SSE endpoint for room events.

    Usage:
        const eventSource = new EventSource('/api/v1/rooms/{room_id}/stream');
        eventSource.addEventListener('memory', (e) => {
            const data = JSON.parse(e.data);
            console.log('New memory:', data);
        });
        eventSource.addEventListener('agent_joined', (e) => {
            const data = JSON.parse(e.data);
            console.log('Agent joined:', data);
        });
    """
    return EventSourceResponse(
        room_event_stream(room_id, agent_id, last_event_id)
    )
