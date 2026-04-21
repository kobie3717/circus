"""Memory Commons API routes - Week 1: Goal Routing + Write-Through."""

import re
import secrets
from datetime import datetime, timedelta
from typing import AsyncIterator, Optional

# W5.1: Valid memory_id format — client-supplied IDs must match this pattern
# (hex suffix from secrets.token_hex, allowing 16-64 chars to cover both
# server 8-byte and client 16-byte token lengths).
_MEMORY_ID_PATTERN = re.compile(r'^shmem-[0-9a-f]{16,64}$')

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
    PublishResponseWithConflict,
    ConnectedEvent,
    MemoryEvent,
    GoalExpiredEvent,
    HeartbeatEvent,
    AgentInfo,
    ProvenanceEvent,
    DomainClaim,
    DomainClaimResponse,
    DomainSteward,
)
from circus.services.goal_router import goal_router
from circus.services.provenance import decay_confidence
from circus.services.belief_merge import apply_belief_merge_pipeline, ConflictResolution
from circus.services.domain_validation import validate_domain, InvalidDomainError

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
              AND is_active = 1
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
        """, (agent_id, datetime.utcnow().isoformat()))

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


@router.post("/publish", response_model=PublishResponseWithConflict)
async def publish_memory(
    mem_req: MemoryPublish,
    agent_id: str = Depends(verify_token)
):
    """
    Publish a memory to the commons.

    Memory is routed to matching goal subscriptions via SSE.
    Week 2: Applies confidence decay and detects conflicts.
    Week 3: Domain field is required and validated.
    """
    if not settings.memory_commons_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Commons is disabled"
        )

    # Week 3: Validate domain field (required, regex-validated)
    try:
        normalized_domain = validate_domain(mem_req.domain)
    except InvalidDomainError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid domain: {str(e)}"
        )

    # Generate memory ID early (needed for W5 owner_binding validation).
    # W5.1 fix: Accept client-supplied memory_id from owner_binding as
    # authoritative. Real clients cannot know the server's random memory_id
    # before signing, so they generate + sign + publish atomically.
    # Format validated here; uniqueness enforced by DB PRIMARY KEY on insert.
    _client_memory_id = None
    if (mem_req.provenance
            and mem_req.provenance.owner_binding
            and mem_req.provenance.owner_binding.memory_id
            and _MEMORY_ID_PATTERN.match(mem_req.provenance.owner_binding.memory_id)):
        _client_memory_id = mem_req.provenance.owner_binding.memory_id

    memory_id = _client_memory_id or f"shmem-{secrets.token_hex(8)}"

    # Week 4: Validate preference memories (publish-side gate)
    if mem_req.category == "user_preference":
        from circus.services.preference_constants import ALLOWLISTED_PREFERENCE_FIELDS

        # Gate 1: preference object must exist
        if not mem_req.preference:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="category=user_preference requires preference object"
            )

        # Gate 2: field must be in allowlist
        if mem_req.preference.field not in ALLOWLISTED_PREFERENCE_FIELDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"preference.field={mem_req.preference.field} not in allowlist"
            )

        # Gate 3: domain must be "preference.user"
        if normalized_domain != "preference.user":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"category=user_preference requires domain=preference.user (got {normalized_domain})"
            )

        # Gate 4: provenance.owner_id must exist
        if not mem_req.provenance or not mem_req.provenance.owner_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="category=user_preference requires provenance.owner_id"
            )

        # Week 5 (5.3): Shape-validate owner_binding (cryptographic verification is admission's job)
        # R1: Require owner_binding for preference memories
        if not mem_req.provenance.owner_binding:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="missing owner_binding"
            )

        binding = mem_req.provenance.owner_binding

        # R2: Validate owner_binding structure (Pydantic already enforces required fields,
        # but provide precise error messages if fields are missing via explicit checks)
        if not binding.signature:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="owner_binding missing signature"
            )
        if not binding.agent_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="owner_binding missing agent_id"
            )
        if not binding.memory_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="owner_binding missing memory_id"
            )
        if not binding.timestamp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="owner_binding missing timestamp"
            )

        # R3 (W5.1): memory_id format validated above when adopting as
        # authoritative. If client provided an invalid format, it was ignored
        # and server generated its own — which won't match binding.memory_id,
        # so reject here for clarity. This preserves the signed-binding
        # guarantee (sig always binds to the memory_id that gets persisted).
        if binding.memory_id != memory_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="owner_binding memory_id invalid format (expected shmem-[0-9a-f]{16,64})"
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

        # memory_id already generated earlier (before W5 validation)
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
            if mem_req.provenance.owner_id:
                provenance_data["owner_id"] = mem_req.provenance.owner_id
            if mem_req.provenance.owner_binding:
                # Week 5: Store owner_binding for admission-side verification
                provenance_data["owner_binding"] = {
                    "agent_id": mem_req.provenance.owner_binding.agent_id,
                    "memory_id": mem_req.provenance.owner_binding.memory_id,
                    "timestamp": mem_req.provenance.owner_binding.timestamp,
                    "signature": mem_req.provenance.owner_binding.signature,
                }

        # Compute effective_confidence with decay (hop=1, age=0 at publish time)
        effective_conf = decay_confidence(
            base_confidence=mem_req.confidence,
            hop_count=1,
            age_seconds=0.0,
            author_trust_score=trust_score
        )

        # Insert into shared_memories (use memory-commons room)
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, domain, tags, provenance,
                privacy_tier, hop_count, original_author, confidence,
                age_days, effective_confidence, shared_at, trust_verified
            ) VALUES (?, 'room-memory-commons', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, 0)
        """, (
            memory_id,
            agent_id,
            mem_req.content,
            mem_req.category,
            normalized_domain,
            json.dumps(mem_req.tags or []),
            json.dumps(provenance_data),
            mem_req.privacy_tier,
            agent_id,
            mem_req.confidence,
            effective_conf,
            now.isoformat()
        ))
        conn.commit()

        # Week 2: Conflict detection
        conflict_result = await apply_belief_merge_pipeline(
            conn,
            new_memory={
                "id": memory_id,
                "from_agent_id": agent_id,
                "content": mem_req.content,
                "category": mem_req.category,
                "domain": normalized_domain,
                "confidence": mem_req.confidence,
                "shared_at": now.isoformat(),
            },
            agent_id=agent_id,
            now=now,
        )

        # Week 4 (4.2): Preference admission (if user_preference memory)
        # Week 6: Returns decision trace for observability
        preference_activated = None
        decision_trace = None
        if mem_req.category == "user_preference" and mem_req.preference:
            from circus.services.preference_admission import admit_preference

            # W5: Extract owner_binding from provenance for signature verification
            owner_binding_dict = None
            if mem_req.provenance and mem_req.provenance.owner_binding:
                owner_binding_dict = {
                    "agent_id": mem_req.provenance.owner_binding.agent_id,
                    "memory_id": mem_req.provenance.owner_binding.memory_id,
                    "timestamp": mem_req.provenance.owner_binding.timestamp,
                    "signature": mem_req.provenance.owner_binding.signature,
                }

            # Apply passport trust multiplier (non-fatal)
            from circus.services.passport_trust import apply_passport_trust
            effective_conf = apply_passport_trust(conn, agent_id, effective_conf)

            decision = admit_preference(
                conn,
                memory_id=memory_id,
                owner_id=mem_req.provenance.owner_id,  # Already validated to exist (gate 4 above)
                preference_field=mem_req.preference.field,
                preference_value=mem_req.preference.value,
                effective_confidence=effective_conf,
                now=now,
                agent_id=agent_id,
                shared_at=now.isoformat(),
                owner_binding=owner_binding_dict,
            )
            # 4.4 coexistence: commit admission write (get_db() context doesn't auto-commit)
            conn.commit()

            # W6: Build decision trace for response
            preference_activated = decision.admitted
            decision_trace = {
                "gates": decision.gates,
                "outcome": "activated" if decision.admitted else decision.reason,
                "field": decision.field,
                "value": decision.value
            }

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

        response_data = {
            "memory_id": memory_id,
            "routed_to": [m['goal_id'] for m in matches],
            "match_scores": [m['match_score'] for m in matches],
            "conflict_resolution": conflict_result,
            "preference_activated": preference_activated
        }

        # W6: Add decision_trace if available
        if decision_trace:
            response_data["decision_trace"] = decision_trace

        # W10: Enqueue for federation (if CIRCUS_PEERS configured)
        from circus.services.federation_worker import enqueue_for_federation

        # Build federation payload (same shape as publish request)
        federation_payload = {
            "content": mem_req.content,
            "category": mem_req.category,
            "domain": normalized_domain,
            "tags": mem_req.tags or [],
            "confidence": mem_req.confidence,
            "privacy_tier": mem_req.privacy_tier,
        }

        # Include provenance if present
        if mem_req.provenance:
            federation_payload["provenance"] = {
                "derived_from": mem_req.provenance.derived_from,
                "citations": mem_req.provenance.citations,
                "reasoning": mem_req.provenance.reasoning,
                "owner_id": mem_req.provenance.owner_id,
            }
            if mem_req.provenance.owner_binding:
                federation_payload["provenance"]["owner_binding"] = {
                    "agent_id": mem_req.provenance.owner_binding.agent_id,
                    "memory_id": mem_req.provenance.owner_binding.memory_id,
                    "timestamp": mem_req.provenance.owner_binding.timestamp,
                    "signature": mem_req.provenance.owner_binding.signature,
                }

        # Include preference if present
        if mem_req.preference:
            federation_payload["preference"] = {
                "field": mem_req.preference.field,
                "value": mem_req.preference.value,
            }

        enqueue_for_federation(memory_id, federation_payload)

        return PublishResponseWithConflict(**response_data)


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


# Domain stewardship endpoints (Week 2)


@router.post("/domains/claim", response_model=DomainClaimResponse)
async def claim_domain(
    claim_req: DomainClaim,
    agent_id: str = Depends(verify_token)
):
    """
    Claim stewardship of a domain.

    Requires Established+ tier (trust_score >= 30).
    Max 5 domains per agent.
    """
    if not settings.memory_commons_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Commons is disabled"
        )

    with get_db() as conn:
        cursor = conn.cursor()

        # Check trust tier
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found"
            )

        if row[0] < settings.trust_tier_newcomer_max:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Established tier or higher required to claim domains"
            )

        # Check domain count limit
        cursor.execute("""
            SELECT COUNT(*) FROM agent_domains WHERE agent_id = ?
        """, (agent_id,))
        domain_count = cursor.fetchone()[0]

        if domain_count >= settings.max_domains_per_agent:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Maximum {settings.max_domains_per_agent} domains per agent"
            )

        # Insert or update claim
        now = datetime.utcnow().isoformat()
        try:
            cursor.execute("""
                INSERT INTO agent_domains (
                    agent_id, domain, stewardship_level, claim_reason,
                    claimed_at, last_updated
                ) VALUES (?, ?, 0.5, ?, ?, ?)
            """, (
                agent_id,
                claim_req.domain,
                claim_req.reason,
                now,
                now
            ))
            conn.commit()
            status_msg = "claimed"
        except sqlite3.IntegrityError:
            # Already claimed by this agent, update reason
            cursor.execute("""
                UPDATE agent_domains
                SET claim_reason = ?, last_updated = ?
                WHERE agent_id = ? AND domain = ?
            """, (claim_req.reason, now, agent_id, claim_req.domain))
            conn.commit()
            status_msg = "updated"

        # Fetch current stewardship level
        cursor.execute("""
            SELECT stewardship_level FROM agent_domains
            WHERE agent_id = ? AND domain = ?
        """, (agent_id, claim_req.domain))
        stewardship_level = cursor.fetchone()[0]

        return DomainClaimResponse(
            domain=claim_req.domain,
            stewardship_level=stewardship_level,
            status=status_msg
        )


@router.get("/domains/{domain}/stewards", response_model=list[DomainSteward])
async def get_domain_stewards(
    domain: str,
    agent_id: str = Depends(verify_token)
):
    """
    Get stewards for a domain.

    Returns agents ordered by stewardship level (highest first).
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT ad.agent_id, a.name, ad.stewardship_level, ad.claimed_at
            FROM agent_domains ad
            JOIN agents a ON ad.agent_id = a.id
            WHERE ad.domain = ?
            ORDER BY ad.stewardship_level DESC
        """, (domain,))

        stewards = []
        for row in cursor.fetchall():
            stewards.append(DomainSteward(
                agent_id=row[0],
                agent_name=row[1],
                stewardship_level=row[2],
                claimed_at=row[3]
            ))

        return stewards


@router.delete("/domains/{claim_id}")
async def release_domain_claim(
    claim_id: int,
    agent_id: str = Depends(verify_token)
):
    """
    Release a domain claim.

    Agent can only release their own claims.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify ownership
        cursor.execute("""
            SELECT agent_id FROM agent_domains WHERE id = ?
        """, (claim_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Domain claim not found"
            )

        if row[0] != agent_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to release this claim"
            )

        # Delete claim
        cursor.execute("DELETE FROM agent_domains WHERE id = ?", (claim_id,))
        conn.commit()

        return {"status": "released", "claim_id": claim_id}


# Preference Inspector API endpoints (Week 6)


@router.get("/preferences/{owner_id}")
async def list_preferences(
    owner_id: str,
    agent_id: str = Depends(verify_token)
):
    """
    List active preferences for an owner.

    Requires ring token auth. Returns all active preferences.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT field_name, value, effective_confidence, updated_at
            FROM active_preferences
            WHERE owner_id = ?
            ORDER BY updated_at DESC
        """, (owner_id,))

        preferences = []
        for row in cursor.fetchall():
            preferences.append({
                "field": row[0],
                "value": row[1],
                "confidence": row[2],
                "updated_at": row[3]
            })

        return {
            "owner_id": owner_id,
            "preferences": preferences,
            "count": len(preferences)
        }


@router.delete("/preferences/{owner_id}/{field_name}")
async def clear_preference(
    owner_id: str,
    field_name: str,
    agent_id: str = Depends(verify_token)
):
    """
    Clear (delete) a specific preference for an owner.

    Operator override — requires ring token auth.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if preference exists
        cursor.execute("""
            SELECT 1 FROM active_preferences
            WHERE owner_id = ? AND field_name = ?
        """, (owner_id, field_name))

        if not cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Preference not found"
            )

        # Delete preference
        cursor.execute("""
            DELETE FROM active_preferences
            WHERE owner_id = ? AND field_name = ?
        """, (owner_id, field_name))
        conn.commit()

        # Non-fatal AI-IQ clear
        try:
            from circus.services.aiiq_bridge import clear_preference_in_aiiq
            clear_preference_in_aiiq(owner_id, field_name)
        except Exception:
            pass  # Never block preference deletion

        return {
            "status": "cleared",
            "owner_id": owner_id,
            "field_name": field_name
        }


@router.get("/preferences/{owner_id}/conflicts")
async def get_conflicts(
    owner_id: str,
    agent_id: str = Depends(verify_token)
):
    """
    Get contested preferences for an owner (W7).

    Returns preferences that have been contested (conflict_count > 0),
    showing which preferences have had competing values published.

    Requires ring token auth.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT field_name, value, effective_confidence, conflict_count, updated_at
            FROM active_preferences
            WHERE owner_id = ? AND conflict_count > 0
            ORDER BY conflict_count DESC, updated_at DESC
        """, (owner_id,))

        conflicts = []
        for row in cursor.fetchall():
            conflicts.append({
                "field": row[0],
                "value": row[1],
                "confidence": row[2],
                "conflict_count": row[3],
                "updated_at": row[4]
            })

        return {
            "owner_id": owner_id,
            "conflicts": conflicts,
            "count": len(conflicts)
        }


# Cross-Agent Shared Learning API (W11)


@router.get("/search")
async def search_shared_knowledge(
    q: str,
    limit: int = 3,
    owner_id: Optional[str] = None
):
    """
    Search shared memories (no auth required - read-only, public knowledge).

    Query shared_memories table using LIKE on content (FTS if available).
    Score: confidence × (1 - hop_count × 0.1) — capped at 0.0
    Returns top `limit` results (default 3, max 10).

    Optional owner_id filter for owner-specific knowledge.
    """
    if not settings.memory_commons_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Commons is disabled"
        )

    # Clamp limit
    limit = max(1, min(limit, 10))

    with get_db() as conn:
        cursor = conn.cursor()

        # Check if FTS table exists (fts_shared_memories)
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='fts_shared_memories'
        """)
        has_fts = cursor.fetchone() is not None

        # Build query
        if has_fts:
            # FTS search
            if owner_id:
                cursor.execute("""
                    SELECT sm.id, sm.content, sm.category, sm.domain, sm.confidence,
                           sm.hop_count, sm.from_agent_id, sm.shared_at
                    FROM fts_shared_memories fts
                    JOIN shared_memories sm ON fts.rowid = sm.rowid
                    WHERE fts.content MATCH ? AND sm.provenance LIKE ?
                    ORDER BY rank
                    LIMIT ?
                """, (q, f'%"owner_id": "{owner_id}"%', limit))
            else:
                cursor.execute("""
                    SELECT sm.id, sm.content, sm.category, sm.domain, sm.confidence,
                           sm.hop_count, sm.from_agent_id, sm.shared_at
                    FROM fts_shared_memories fts
                    JOIN shared_memories sm ON fts.rowid = sm.rowid
                    WHERE fts.content MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (q, limit))
        else:
            # Fallback: LIKE search
            if owner_id:
                cursor.execute("""
                    SELECT id, content, category, domain, confidence,
                           hop_count, from_agent_id, shared_at
                    FROM shared_memories
                    WHERE content LIKE ? AND provenance LIKE ?
                    ORDER BY shared_at DESC
                    LIMIT ?
                """, (f'%{q}%', f'%"owner_id": "{owner_id}"%', limit))
            else:
                cursor.execute("""
                    SELECT id, content, category, domain, confidence,
                           hop_count, from_agent_id, shared_at
                    FROM shared_memories
                    WHERE content LIKE ?
                    ORDER BY shared_at DESC
                    LIMIT ?
                """, (f'%{q}%', limit))

        results = []
        for row in cursor.fetchall():
            memory_id, content, category, domain, confidence, hop_count, from_agent_id, shared_at = row

            # Calculate score: confidence × (1 - hop_count × 0.1) — capped at 0.0
            score = max(0.0, confidence * (1.0 - hop_count * 0.1))

            # Extract clean source_agent name (strip timestamp suffix if present)
            source_agent = from_agent_id.split('-')[0] if from_agent_id else 'unknown'

            results.append({
                "memory_id": memory_id,
                "content": content,
                "category": category,
                "domain": domain,
                "confidence": confidence,
                "score": round(score, 2),
                "source_agent": source_agent,
                "shared_at": shared_at
            })

        return {
            "results": results,
            "query": q,
            "count": len(results)
        }
