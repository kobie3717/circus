"""Agent registration and discovery routes."""

import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from jose import JWTError, jwt
from passlib.hash import bcrypt

from circus.config import settings
from circus.database import get_db
from circus.models import (
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentResponse,
    BootBriefingResponse,
    CompetenceObservationRequest,
    DiscoverResponse,
    DomainCompetence,
    PassportRefreshRequest,
    PassportRefreshResponse,
    VouchRequest,
    VouchResponse,
)
from circus.passport import calculate_passport_hash
from circus.trust import calculate_trust_score, calculate_trust_delta, can_vouch, get_trust_tier
from circus.services.signing import (
    generate_keypair,
    sign_agent_card,
    verify_signature,
    encode_public_key,
    decode_public_key,
)

router = APIRouter()


def get_agent_competence_list(agent_id: str) -> list[DomainCompetence] | None:
    """Helper to get competence list for an agent."""
    try:
        from circus.services.briefing import get_agent_competence
        competencies = get_agent_competence(agent_id)
        if competencies:
            return [
                DomainCompetence(
                    domain=c["domain"],
                    score=c["score"],
                    observations=c["observations"]
                )
                for c in competencies[:5]  # Top 5 domains
            ]
    except Exception:
        pass
    return None


def create_access_token(agent_id: str, expires_delta: timedelta) -> str:
    """Create JWT access token."""
    expire = datetime.utcnow() + expires_delta
    to_encode = {
        "sub": agent_id,
        "exp": expire,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def verify_token(authorization: str = Header(...)) -> str:
    """Verify JWT token and return agent_id."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization[7:]  # Remove "Bearer " prefix

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        agent_id = payload.get("sub")
        if agent_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return agent_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


@router.post("/register", response_model=AgentRegisterResponse, status_code=201)
async def register_agent(request: AgentRegisterRequest):
    """Register a new agent with AI-IQ passport."""
    # Validate passport structure
    required_fields = ["identity", "score"]
    for field in required_fields:
        if field not in request.passport:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid passport: missing field '{field}'"
            )

    # Validate identity structure
    identity = request.passport.get("identity", {})
    if "name" not in identity:
        raise HTTPException(
            status_code=400,
            detail="Invalid passport: identity.name is required"
        )

    # Generate agent ID
    agent_id = f"{request.name.lower().replace(' ', '-')}-{secrets.token_hex(3)}"

    # Compute passport hash
    passport_hash = calculate_passport_hash(request.passport)

    # Generate ring token
    ring_token_value = secrets.token_urlsafe(32)
    token_hash = bcrypt.hash(ring_token_value)

    # Calculate initial trust score
    now = datetime.utcnow().isoformat()
    trust_score = calculate_trust_score(request.passport, now)
    trust_tier = get_trust_tier(trust_score)

    # Generate Ed25519 keypair for signing
    private_key_bytes, public_key_bytes = generate_keypair()

    # Sign agent card
    card_data = {
        "agent_id": agent_id,
        "name": request.name,
        "role": request.role,
        "capabilities": request.capabilities,
        "registered_at": now
    }
    signed_card = sign_agent_card(card_data, private_key_bytes)

    # Store in database
    expires_at = datetime.utcnow() + timedelta(days=settings.access_token_expire_days)

    with get_db() as conn:
        cursor = conn.cursor()

        # Check if agent with same name exists
        cursor.execute("SELECT id FROM agents WHERE name = ?", (request.name,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Agent name already registered")

        # Insert agent
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance, contact,
                passport_hash, token_hash, trust_score, trust_tier,
                public_key, signed_card,
                registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, request.name, request.role,
            json.dumps(request.capabilities), request.home,
            request.contact, passport_hash, token_hash,
            trust_score, trust_tier,
            public_key_bytes, signed_card,
            now, now
        ))

        # Insert passport
        # Calculate prediction accuracy from confirmed/refuted
        predictions = request.passport.get("predictions", {})
        confirmed = predictions.get("confirmed", 0)
        refuted = predictions.get("refuted", 0)
        total_predictions = confirmed + refuted
        prediction_accuracy = confirmed / total_predictions if total_predictions > 0 else 0.5

        # Belief stability (lower contradictions = higher stability)
        beliefs = request.passport.get("beliefs", {})
        total_beliefs = beliefs.get("total", 1)
        contradictions = beliefs.get("contradictions", 0)
        belief_stability = 1.0 - (contradictions / total_beliefs) if total_beliefs > 0 else 1.0

        # Memory quality from memory_stats
        memory_stats = request.passport.get("memory_stats", {})
        memory_quality = memory_stats.get("proof_count_avg", 0.0)

        # Passport score - handle both dict and float formats
        score_data = request.passport.get("score", {})
        if isinstance(score_data, dict):
            passport_score = score_data.get("total", 0.0)
        else:
            passport_score = score_data if isinstance(score_data, (int, float)) else 0.0

        cursor.execute("""
            INSERT INTO passports (
                agent_id, passport_data, trust_score,
                prediction_accuracy, belief_stability,
                memory_quality, passport_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, json.dumps(request.passport),
            trust_score, prediction_accuracy,
            belief_stability, memory_quality,
            passport_score, now
        ))

        # Generate and store embedding (optional - only if embeddings available)
        try:
            from circus.services.embeddings import embed_agent_profile
            embedding = embed_agent_profile(
                request.name,
                request.role,
                request.capabilities
            )
            # Store both blob (for sqlite-vec) and JSON (for fallback)
            import numpy as np
            embedding_array = np.array(embedding, dtype=np.float32)
            cursor.execute("""
                INSERT INTO agent_embeddings (agent_id, embedding, embedding_json, created_at)
                VALUES (?, ?, ?, ?)
            """, (agent_id, embedding_array.tobytes(), json.dumps(embedding), now))
        except (ImportError, RuntimeError):
            # sentence-transformers not installed, skip embeddings
            pass

        conn.commit()

    # Create JWT token
    jwt_token = create_access_token(
        agent_id,
        timedelta(days=settings.access_token_expire_days)
    )

    return AgentRegisterResponse(
        agent_id=agent_id,
        ring_token=jwt_token,
        trust_score=trust_score,
        trust_tier=trust_tier,
        expires_at=expires_at.isoformat()
    )


@router.put("/{agent_id}/passport", response_model=PassportRefreshResponse)
async def refresh_passport(
    agent_id: str,
    request: PassportRefreshRequest,
    current_agent_id: str = Depends(verify_token)
):
    """Refresh agent's passport."""
    if agent_id != current_agent_id:
        raise HTTPException(status_code=403, detail="Can only refresh your own passport")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT registered_at FROM agents WHERE id = ?
        """, (agent_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")

        registered_at = row["registered_at"]

        # Recalculate trust score
        trust_score = calculate_trust_score(request.passport, registered_at)
        trust_tier = get_trust_tier(trust_score)

        # Update agent
        now = datetime.utcnow().isoformat()
        passport_hash = calculate_passport_hash(request.passport)

        cursor.execute("""
            UPDATE agents
            SET passport_hash = ?, trust_score = ?, trust_tier = ?, last_seen = ?
            WHERE id = ?
        """, (passport_hash, trust_score, trust_tier, now, agent_id))

        # Insert new passport
        # Calculate prediction accuracy from confirmed/refuted
        predictions = request.passport.get("predictions", {})
        confirmed = predictions.get("confirmed", 0)
        refuted = predictions.get("refuted", 0)
        total_predictions = confirmed + refuted
        prediction_accuracy = confirmed / total_predictions if total_predictions > 0 else 0.5

        # Belief stability (lower contradictions = higher stability)
        beliefs = request.passport.get("beliefs", {})
        total_beliefs = beliefs.get("total", 1)
        contradictions = beliefs.get("contradictions", 0)
        belief_stability = 1.0 - (contradictions / total_beliefs) if total_beliefs > 0 else 1.0

        # Memory quality from memory_stats
        memory_stats = request.passport.get("memory_stats", {})
        memory_quality = memory_stats.get("proof_count_avg", 0.0)

        # Passport score - handle both dict and float formats
        score_data = request.passport.get("score", {})
        if isinstance(score_data, dict):
            passport_score = score_data.get("total", 0.0)
        else:
            passport_score = score_data if isinstance(score_data, (int, float)) else 0.0

        cursor.execute("""
            INSERT INTO passports (
                agent_id, passport_data, trust_score,
                prediction_accuracy, belief_stability,
                memory_quality, passport_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, json.dumps(request.passport),
            trust_score, prediction_accuracy,
            belief_stability, memory_quality,
            passport_score, now
        ))

        # Log trust event
        cursor.execute("""
            INSERT INTO trust_events (agent_id, event_type, delta, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (agent_id, "passport_refresh", 10.0, "Passport refreshed", now))

        conn.commit()

    next_refresh = datetime.utcnow() + timedelta(days=settings.passport_refresh_days)

    return PassportRefreshResponse(
        trust_score=trust_score,
        trust_tier=trust_tier,
        passport_age_days=0,
        next_refresh=next_refresh.isoformat()
    )


@router.get("/discover", response_model=DiscoverResponse)
async def discover(
    capability: Optional[str] = Query(None),
    min_trust: float = Query(30.0, ge=0, le=100),
    limit: int = Query(50, ge=1, le=100)
):
    """Discover agents by capability or trust score."""
    with get_db() as conn:
        cursor = conn.cursor()

        if capability:
            # Search by capability using FTS
            # For FTS5, we need to search in the capabilities column specifically
            # Escape and quote the search term for FTS5
            fts_query = f'capabilities: "{capability}"'
            cursor.execute("""
                SELECT a.*, p.prediction_accuracy
                FROM agents a
                LEFT JOIN passports p ON a.id = p.agent_id
                WHERE a.id IN (
                    SELECT agent_id FROM agents_fts WHERE agents_fts MATCH ?
                )
                AND a.trust_score >= ?
                AND a.is_active = 1
                ORDER BY a.trust_score DESC
                LIMIT ?
            """, (fts_query, min_trust, limit))
        else:
            # List all agents above trust threshold
            cursor.execute("""
                SELECT a.*, p.prediction_accuracy
                FROM agents a
                LEFT JOIN passports p ON a.id = p.agent_id
                WHERE a.trust_score >= ?
                AND a.is_active = 1
                ORDER BY a.trust_score DESC
                LIMIT ?
            """, (min_trust, limit))

        rows = cursor.fetchall()

    agent_responses = []
    for row in rows:
        # Encode public key if available
        public_key_str = None
        try:
            if row["public_key"]:
                public_key_str = encode_public_key(row["public_key"])
        except (KeyError, IndexError):
            pass

        signed_card_val = None
        try:
            signed_card_val = row["signed_card"]
        except (KeyError, IndexError):
            pass

        agent_responses.append(AgentResponse(
            agent_id=row["id"],
            name=row["name"],
            role=row["role"],
            capabilities=json.loads(row["capabilities"]),
            home_instance=row["home_instance"],
            trust_score=row["trust_score"],
            trust_tier=row["trust_tier"],
            prediction_accuracy=row["prediction_accuracy"],
            registered_at=row["registered_at"],
            last_seen=row["last_seen"],
            public_key=public_key_str,
            signed_card=signed_card_val,
            competence=get_agent_competence_list(row["id"])
        ))

    return DiscoverResponse(
        agents=agent_responses,
        count=len(agent_responses)
    )


@router.get("/audit-log")
async def get_audit_log(
    agent_id: str = Depends(verify_token),
    limit: int = Query(100, ge=1, le=500)
):
    """Get audit log (Elders only)."""
    from circus.services.trust import can_moderate

    with get_db() as conn:
        cursor = conn.cursor()

        # Check if agent is Elder
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()

        if not row or not can_moderate(row["trust_score"]):
            raise HTTPException(status_code=403, detail="Requires Elder tier")

        # Get audit log
        cursor.execute("""
            SELECT * FROM audit_log
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))

        logs = []
        for row in cursor.fetchall():
            logs.append({
                "id": row["id"],
                "agent_id": row["agent_id"],
                "action": row["action"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "trust_tier": row["trust_tier"],
                "allowed": bool(row["allowed"]),
                "reason": row["reason"],
                "ip_address": row["ip_address"],
                "created_at": row["created_at"]
            })

        return logs


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str):
    """Get agent details by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT a.*, p.prediction_accuracy
            FROM agents a
            LEFT JOIN passports p ON a.id = p.agent_id
            WHERE a.id = ?
        """, (agent_id,))
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Encode public key if available
    public_key_str = None
    try:
        if row["public_key"]:
            public_key_str = encode_public_key(row["public_key"])
    except (KeyError, IndexError):
        pass

    signed_card_val = None
    try:
        signed_card_val = row["signed_card"]
    except (KeyError, IndexError):
        pass

    return AgentResponse(
        agent_id=row["id"],
        name=row["name"],
        role=row["role"],
        capabilities=json.loads(row["capabilities"]),
        home_instance=row["home_instance"],
        trust_score=row["trust_score"],
        trust_tier=row["trust_tier"],
        prediction_accuracy=row["prediction_accuracy"],
        registered_at=row["registered_at"],
        last_seen=row["last_seen"],
        public_key=public_key_str,
        signed_card=signed_card_val,
        competence=get_agent_competence_list(row["id"])
    )


@router.post("/{agent_id}/vouch", response_model=VouchResponse)
async def vouch_for_agent(
    agent_id: str,
    request: VouchRequest,
    current_agent_id: str = Depends(verify_token)
):
    """Vouch for another agent (costs trust to the voucher, benefits the vouchee)."""
    target_agent_id = request.target_agent_id

    # Can't vouch for yourself
    if current_agent_id == target_agent_id:
        raise HTTPException(status_code=400, detail="Cannot vouch for yourself")

    with get_db() as conn:
        cursor = conn.cursor()

        # Get voucher's trust score
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (current_agent_id,))
        voucher_row = cursor.fetchone()
        if not voucher_row:
            raise HTTPException(status_code=404, detail="Voucher not found")

        voucher_trust = voucher_row["trust_score"]

        # Check if voucher has permission
        if not can_vouch(voucher_trust):
            raise HTTPException(
                status_code=403,
                detail="Insufficient trust to vouch (requires Trusted tier or higher)"
            )

        # Get target agent
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (target_agent_id,))
        target_row = cursor.fetchone()
        if not target_row:
            raise HTTPException(status_code=404, detail="Target agent not found")

        # Check if vouch already exists
        cursor.execute("""
            SELECT id FROM vouches
            WHERE from_agent_id = ? AND to_agent_id = ?
        """, (current_agent_id, target_agent_id))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Already vouched for this agent")

        # Calculate trust deltas
        target_delta = calculate_trust_delta("vouch_received")
        voucher_cost = calculate_trust_delta("vouch_given")  # This is negative

        now = datetime.utcnow().isoformat()

        # Insert vouch record
        cursor.execute("""
            INSERT INTO vouches (from_agent_id, to_agent_id, weight, note, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (current_agent_id, target_agent_id, target_delta, request.note, now))

        vouch_id = cursor.lastrowid

        # Update target agent's trust score
        new_target_trust = target_row["trust_score"] + target_delta
        cursor.execute("""
            UPDATE agents SET trust_score = ?, trust_tier = ? WHERE id = ?
        """, (new_target_trust, get_trust_tier(new_target_trust), target_agent_id))

        # Update voucher's trust score
        new_voucher_trust = voucher_trust + voucher_cost
        cursor.execute("""
            UPDATE agents SET trust_score = ?, trust_tier = ? WHERE id = ?
        """, (new_voucher_trust, get_trust_tier(new_voucher_trust), current_agent_id))

        # Log trust events
        cursor.execute("""
            INSERT INTO trust_events (agent_id, event_type, delta, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (target_agent_id, "vouch_received", target_delta, f"Vouched by {current_agent_id}", now))

        cursor.execute("""
            INSERT INTO trust_events (agent_id, event_type, delta, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (current_agent_id, "vouch_given", voucher_cost, f"Vouched for {target_agent_id}", now))

        conn.commit()

    return VouchResponse(
        vouch_id=vouch_id,
        target_trust_delta=target_delta,
        your_trust_cost=voucher_cost
    )


@router.post("/{agent_id}/trust-event")
async def record_trust_event(
    agent_id: str,
    event_type: str,
    context: dict[str, Any] | None = None,
    current_agent_id: str = Depends(verify_token)
):
    """Record a trust event and update agent's trust score."""
    # For now, allow agents to record their own trust events
    # In production, this should be admin-only or restricted
    if agent_id != current_agent_id:
        raise HTTPException(status_code=403, detail="Can only record trust events for yourself")

    with get_db() as conn:
        cursor = conn.cursor()

        # Get current trust score
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")

        current_trust = row["trust_score"]

        # Calculate trust delta
        context = context or {}
        context["current_trust"] = current_trust
        delta = calculate_trust_delta(event_type, context)

        # Update trust score
        new_trust = max(0.0, min(100.0, current_trust + delta))
        new_tier = get_trust_tier(new_trust)

        cursor.execute("""
            UPDATE agents SET trust_score = ?, trust_tier = ? WHERE id = ?
        """, (new_trust, new_tier, agent_id))

        # Log trust event
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO trust_events (agent_id, event_type, delta, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (agent_id, event_type, delta, json.dumps(context), now))

        conn.commit()

    return {
        "agent_id": agent_id,
        "event_type": event_type,
        "delta": delta,
        "new_trust_score": new_trust,
        "new_trust_tier": new_tier
    }


@router.get("/{agent_id}/verify")
async def verify_agent_card(agent_id: str):
    """Verify agent's signed capability card."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, role, capabilities, public_key, signed_card, registered_at
            FROM agents
            WHERE id = ?
        """, (agent_id,))
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not row["public_key"] or not row["signed_card"]:
        raise HTTPException(status_code=400, detail="Agent card not signed")

    # Reconstruct card data
    card_data = {
        "agent_id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "capabilities": json.loads(row["capabilities"]),
        "registered_at": row["registered_at"]
    }

    # Verify signature
    is_valid = verify_signature(
        card_data,
        row["signed_card"],
        row["public_key"]
    )

    return {
        "agent_id": agent_id,
        "signature_valid": is_valid,
        "public_key": encode_public_key(row["public_key"]),
        "signed_card": row["signed_card"]
    }


@router.get("/discover/semantic", response_model=DiscoverResponse)
async def discover_semantic(
    q: str = Query(..., min_length=1, description="Natural language search query"),
    min_similarity: float = Query(0.5, ge=0, le=1, description="Minimum similarity score"),
    min_trust: float = Query(30.0, ge=0, le=100, description="Minimum trust score"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results")
):
    """
    Semantic agent discovery using vector similarity.

    Example: "Find agents who help with WhatsApp automation and testing"

    Requires sentence-transformers to be installed. Falls back to keyword search if not available.
    """
    try:
        from circus.services.embeddings import search_similar_agents_vector

        # Get similar agents by embedding
        similar_agents = search_similar_agents_vector(
            q,
            settings.database_path,
            limit=limit * 2,  # Get more to filter by trust
            min_score=min_similarity
        )

        # Fetch full agent data and filter by trust
        with get_db() as conn:
            cursor = conn.cursor()

            agent_responses = []
            for agent_id, similarity in similar_agents:
                cursor.execute("""
                    SELECT a.*, p.prediction_accuracy
                    FROM agents a
                    LEFT JOIN passports p ON a.id = p.agent_id
                    WHERE a.id = ? AND a.trust_score >= ? AND a.is_active = 1
                """, (agent_id, min_trust))

                row = cursor.fetchone()
                if row and len(agent_responses) < limit:
                    public_key_str = None
                    try:
                        if row["public_key"]:
                            public_key_str = encode_public_key(row["public_key"])
                    except (KeyError, IndexError):
                        pass

                    signed_card_val = None
                    try:
                        signed_card_val = row["signed_card"]
                    except (KeyError, IndexError):
                        pass

                    agent_responses.append(AgentResponse(
                        agent_id=row["id"],
                        name=row["name"],
                        role=row["role"],
                        capabilities=json.loads(row["capabilities"]),
                        home_instance=row["home_instance"],
                        trust_score=row["trust_score"],
                        trust_tier=row["trust_tier"],
                        prediction_accuracy=row["prediction_accuracy"],
                        registered_at=row["registered_at"],
                        last_seen=row["last_seen"],
                        public_key=public_key_str,
                        signed_card=signed_card_val,
                        competence=get_agent_competence_list(row["id"])
                    ))

        return DiscoverResponse(
            agents=agent_responses,
            count=len(agent_responses)
        )

    except (ImportError, RuntimeError) as e:
        # Fallback to keyword search if embeddings not available
        raise HTTPException(
            status_code=501,
            detail=f"Semantic search not available: {str(e)}. Install with: pip install sentence-transformers"
        )


@router.post("/{agent_id}/competence")
async def record_competence(
    agent_id: str,
    request: CompetenceObservationRequest,
    current_agent_id: str = Depends(verify_token)
):
    """
    Record a competence observation for an agent.

    Updates the agent's domain-specific competence score using weighted moving average.
    Agents can only record observations for themselves, or Elders can record for any agent.
    """
    from circus.services.briefing import record_competence_observation

    # Check permissions
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (current_agent_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Current agent not found")

        # Allow self-recording or Elder tier
        from circus.services.trust import can_moderate
        if agent_id != current_agent_id and not can_moderate(row["trust_score"]):
            raise HTTPException(
                status_code=403,
                detail="Can only record competence for yourself (or Elder tier required)"
            )

    # Validate domain
    valid_domains = [
        "coding", "research", "monitoring", "testing",
        "planning", "creative", "devops", "communication"
    ]
    if request.domain not in valid_domains:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid domain. Must be one of: {', '.join(valid_domains)}"
        )

    # Record observation
    result = record_competence_observation(
        agent_id,
        request.domain,
        request.success,
        request.weight
    )

    return {
        "agent_id": agent_id,
        "domain": result["domain"],
        "new_score": result["score"],
        "observations": result["observations"],
        "updated_at": result["last_updated"]
    }


@router.get("/{agent_id}/competence")
async def get_agent_competence_scores(agent_id: str):
    """Get all domain competence scores for an agent."""
    from circus.services.briefing import get_agent_competence

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM agents WHERE id = ?", (agent_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Agent not found")

    competencies = get_agent_competence(agent_id)

    return {
        "agent_id": agent_id,
        "competencies": competencies,
        "count": len(competencies)
    }


@router.get("/briefing/boot", response_model=BootBriefingResponse)
async def get_boot_briefing():
    """
    Generate a theory-of-mind boot briefing.

    Returns a structured summary of who's good at what across all agents,
    so a booting agent knows who to delegate to.
    """
    from circus.services.briefing import generate_boot_briefing
    from circus.models import AgentCompetenceSummary

    briefing_data = generate_boot_briefing()

    # Convert to Pydantic models
    agents = [
        AgentCompetenceSummary(
            name=agent["name"],
            agent_id=agent["agent_id"],
            top_domains=[
                DomainCompetence(
                    domain=d["domain"],
                    score=d["score"],
                    observations=d["observations"]
                )
                for d in agent["top_domains"]
            ]
        )
        for agent in briefing_data["agents"]
    ]

    return BootBriefingResponse(
        briefing=briefing_data["briefing"],
        agents=agents,
        generated_at=briefing_data["generated_at"]
    )
