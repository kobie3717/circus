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
    DiscoverResponse,
    PassportRefreshRequest,
    PassportRefreshResponse,
    VouchRequest,
    VouchResponse,
)
from circus.passport import calculate_passport_hash
from circus.trust import calculate_trust_score, calculate_trust_delta, can_vouch, get_trust_tier

router = APIRouter()


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
                registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, request.name, request.role,
            json.dumps(request.capabilities), request.home,
            request.contact, passport_hash, token_hash,
            trust_score, trust_tier, now, now
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

        # Passport score
        passport_score = request.passport.get("score", {}).get("total", 0.0)

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

        # Passport score
        passport_score = request.passport.get("score", {}).get("total", 0.0)

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
            last_seen=row["last_seen"]
        ))

    return DiscoverResponse(
        agents=agent_responses,
        count=len(agent_responses)
    )


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
        last_seen=row["last_seen"]
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
