"""Memory Commons API routes - Week 1: Goal Routing + Write-Through."""

import secrets
from datetime import datetime, timedelta
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from circus.config import settings
from circus.database import get_db
from circus.routes.agents import verify_token
from circus.models import (
    GoalCreate,
    GoalResponse,
    GoalInfo,
    MemoryPublish,
    PublishResponse,
    ConnectedEvent,
    MemoryEvent,
    GoalExpiredEvent,
    HeartbeatEvent,
    AgentInfo,
    ProvenanceEvent,
)
from circus.services.goal_router import goal_router

import asyncio
import json
import sqlite3

router = APIRouter(prefix="/api/v1/memory-commons", tags=["memory-commons"])


# In-memory SSE connections tracker
# Format: {goal_id: [queue1, queue2, ...]}
_sse_queues: dict[str, list[asyncio.Queue]] = {}


@router.post("/goals", response_model=GoalResponse)
async def create_goal(
    goal_req: GoalCreate,
    agent_id: str = Depends(verify_token)
):
    """
    Create a goal subscription for semantic memory routing.

    Returns SSE stream URL for receiving matched memories.
    """
    if not settings.memory_commons_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Commons is disabled"
        )

    with get_db() as conn:
        cursor = conn.cursor()

        # Check agent's active goal count
        cursor.execute("""
            SELECT COUNT(*) FROM goal_subscriptions
            WHERE agent_id = ? AND is_active = 1
        """, (agent_id,))
        active_count = cursor.fetchone()[0]

        if active_count >= settings.max_goals_per_agent:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Maximum {settings.max_goals_per_agent} active goals per agent"
            )

        # Generate goal ID
        goal_id = f"goal-{secrets.token_hex(8)}"

        # Embed goal description
        goal_embedding = goal_router.embed_text(goal_req.goal_description)

        # Calculate expiry
        now = datetime.utcnow()
        expires_at = None
        if goal_req.expires_in_hours:
            expires_at = (now + timedelta(hours=goal_req.expires_in_hours)).isoformat()

        # Insert goal
        cursor.execute("""
            INSERT INTO goal_subscriptions (
                id, agent_id, goal_description, goal_embedding,
                min_confidence, created_at, expires_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            goal_id,
            agent_id,
            goal_req.goal_description,
            goal_embedding,
            goal_req.min_confidence,
            now.isoformat(),
            expires_at
        ))
        conn.commit()

    stream_url = f"/api/v1/memory-commons/stream?goal_id={goal_id}"
    return GoalResponse(goal_id=goal_id, stream_url=stream_url)


@router.delete("/goals/{goal_id}")
async def delete_goal(
    goal_id: str,
    agent_id: str = Depends(verify_token)
):
    """Delete (unsubscribe from) a goal."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify ownership
        cursor.execute("""
            SELECT agent_id FROM goal_subscriptions WHERE id = ?
        """, (goal_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Goal not found"
            )

        if row[0] != agent_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to delete this goal"
            )

        # Soft delete (mark inactive)
        cursor.execute("""
            UPDATE goal_subscriptions
            SET is_active = 0
            WHERE id = ?
        """, (goal_id,))
        conn.commit()

    # Notify SSE streams
    await _broadcast_to_goal(goal_id, GoalExpiredEvent(
        type="goal_expired",
        goal_id=goal_id,
        reason="manually deleted"
    ))

    # Clean up SSE queues
    if goal_id in _sse_queues:
        del _sse_queues[goal_id]

    return {"status": "unsubscribed", "goal_id": goal_id}


@router.get("/goals", response_model=list[GoalInfo])
async def list_goals(
    agent_id: str = Depends(verify_token)
):
    """List active goals for current agent."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, agent_id, goal_description, min_confidence,
                   created_at, expires_at, is_active
            FROM goal_subscriptions
            WHERE agent_id = ?
            ORDER BY created_at DESC
        """, (agent_id,))

        goals = []
        for row in cursor.fetchall():
            goals.append(GoalInfo(
                id=row[0],
                agent_id=row[1],
                goal_description=row[2],
                min_confidence=row[3],
                created_at=row[4],
                expires_at=row[5],
                is_active=bool(row[6])
            ))

        return goals


@router.post("/publish", response_model=PublishResponse)
async def publish_memory(
    mem_req: MemoryPublish,
    agent_id: str = Depends(verify_token)
):
    """
    Publish a memory to the commons.

    Memory is routed to matching goal subscriptions via SSE.
    """
    if not settings.memory_commons_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Commons is disabled"
        )

    with get_db() as conn:
        cursor = conn.cursor()

        # Get agent trust info
        cursor.execute("""
            SELECT name, trust_score, trust_tier
            FROM agents WHERE id = ?
        """, (agent_id,))
        agent_row = cursor.fetchone()
        if not agent_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found"
            )

        agent_name, trust_score, trust_tier = agent_row

        # Trust gate: public memories require Established+ tier (trust_score >= 30)
        if mem_req.privacy_tier == "public" and trust_score < settings.trust_tier_newcomer_max:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Established tier or higher required to publish public memories"
            )

        # Generate memory ID
        memory_id = f"shmem-{secrets.token_hex(8)}"
        now = datetime.utcnow()

        # Build provenance JSON
        provenance_data = {
            "hop_count": 1,
            "original_author": agent_id,
            "original_timestamp": now.isoformat(),
            "confidence": mem_req.confidence,
        }
        if mem_req.provenance:
            if mem_req.provenance.derived_from:
                provenance_data["derived_from"] = mem_req.provenance.derived_from
            if mem_req.provenance.citations:
                provenance_data["citations"] = mem_req.provenance.citations
            if mem_req.provenance.reasoning:
                provenance_data["reasoning"] = mem_req.provenance.reasoning

        # Insert into shared_memories (use memory-commons room)
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, tags, provenance,
                privacy_tier, hop_count, original_author, confidence,
                age_days, effective_confidence, shared_at, trust_verified
            ) VALUES (?, 'room-memory-commons', ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, 0)
        """, (
            memory_id,
            agent_id,
            mem_req.content,
            mem_req.category,
            json.dumps(mem_req.tags or []),
            json.dumps(provenance_data),
            mem_req.privacy_tier,
            agent_id,
            mem_req.confidence,
            mem_req.confidence,  # effective_confidence = confidence at hop=1
            now.isoformat()
        ))
        conn.commit()

        # Semantic routing: find matching goals
        matches = goal_router.find_matching_goals(
            conn,
            mem_req.content,
            mem_req.confidence
        )

        # Broadcast to matching goals via SSE
        for match in matches:
            await _broadcast_memory_to_goal(
                match['goal_id'],
                memory_id=memory_id,
                content=mem_req.content,
                category=mem_req.category,
                tags=mem_req.tags,
                from_agent=AgentInfo(
                    id=agent_id,
                    name=agent_name,
                    trust_score=trust_score
                ),
                provenance=ProvenanceEvent(
                    hop_count=1,
                    original_author=agent_id,
                    confidence=mem_req.confidence,
                    age_days=0,
                    effective_confidence=mem_req.confidence
                ),
                match_score=match['match_score']
            )

        return PublishResponse(
            memory_id=memory_id,
            routed_to=[m['goal_id'] for m in matches],
            match_scores=[m['match_score'] for m in matches]
        )


@router.get("/stream")
async def stream_memories(
    goal_id: str,  # Required to prevent orphaned queue leak
    agent_id: str = Depends(verify_token)
):
    """
    SSE stream for receiving memories matched to a goal.

    goal_id is required to prevent memory leaks from orphaned queues.
    """
    if not settings.memory_commons_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Commons is disabled"
        )

    # Verify goal ownership
    if goal_id:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_id FROM goal_subscriptions WHERE id = ?
            """, (goal_id,))
            row = cursor.fetchone()

            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Goal not found"
                )

            if row[0] != agent_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not authorized to access this goal stream"
                )

    async def event_generator() -> AsyncIterator[str]:
        """Generate SSE events."""
        # Create queue for this connection
        queue: asyncio.Queue = asyncio.Queue()

        # Register queue (goal_id is now required)
        if goal_id not in _sse_queues:
            _sse_queues[goal_id] = []
        _sse_queues[goal_id].append(queue)

        try:
            # Send connected event
            connected = ConnectedEvent(
                type="connected",
                timestamp=datetime.utcnow().isoformat(),
                goal_id=goal_id
            )
            yield f"event: connected\ndata: {connected.model_dump_json()}\n\n"

            # Event loop
            while True:
                try:
                    # Wait for events with timeout for heartbeat
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
                except asyncio.TimeoutError:
                    # Send heartbeat
                    heartbeat = HeartbeatEvent(
                        type="heartbeat",
                        timestamp=datetime.utcnow().isoformat()
                    )
                    yield f"event: heartbeat\ndata: {heartbeat.model_dump_json()}\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            # Cleanup queue on disconnect (defensive check)
            if goal_id in _sse_queues:
                if queue in _sse_queues[goal_id]:
                    _sse_queues[goal_id].remove(queue)
                if not _sse_queues[goal_id]:
                    del _sse_queues[goal_id]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# Helper functions for SSE broadcasting
async def _broadcast_memory_to_goal(
    goal_id: str,
    memory_id: str,
    content: str,
    category: str,
    tags: Optional[list[str]],
    from_agent: AgentInfo,
    provenance: ProvenanceEvent,
    match_score: float
):
    """Broadcast memory event to all SSE clients for a goal."""
    if goal_id not in _sse_queues:
        return

    event_data = {
        "type": "memory",
        "memory_id": memory_id,
        "content": content,
        "category": category,
        "tags": tags,
        "from_agent": from_agent.model_dump(),
        "provenance": provenance.model_dump(),
        "match_score": match_score,
        "goal_id": goal_id,
        "timestamp": datetime.utcnow().isoformat()
    }

    event = {
        "type": "memory",
        "data": event_data
    }

    # Send to all queues for this goal
    for queue in _sse_queues[goal_id]:
        try:
            await queue.put(event)
        except Exception:
            pass  # Ignore queue errors


async def _broadcast_to_goal(goal_id: str, event: BaseModel):
    """Broadcast a generic event to a goal's SSE streams."""
    if goal_id not in _sse_queues:
        return

    event_dict = {
        "type": event.type,  # type: ignore
        "data": event.model_dump()
    }

    for queue in _sse_queues[goal_id]:
        try:
            await queue.put(event_dict)
        except Exception:
            pass
