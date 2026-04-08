"""Agent registration and discovery routes."""

import json
import secrets
from datetime import datetime, timedelta
from typing import Optional

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
)
from circus.passport import calculate_passport_hash
from circus.trust import calculate_trust_score, get_trust_tier

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
    required_fields = ["agent_name", "generated_at", "passport_score"]
    for field in required_fields:
        if field not in request.passport:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid passport: missing field '{field}'"
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
        prediction_accuracy = request.passport.get("predictions", {}).get("accuracy", 0.0)
        belief_stability = request.passport.get("beliefs", {}).get("stability", 1.0)
        memory_quality_data = request.passport.get("memory_quality", {})
        memory_quality = memory_quality_data.get("proof_count_avg", 0.0)
        passport_score = request.passport.get("passport_score", {}).get("total", 0.0)

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
        prediction_accuracy = request.passport.get("predictions", {}).get("accuracy", 0.0)
        belief_stability = request.passport.get("beliefs", {}).get("stability", 1.0)
        memory_quality_data = request.passport.get("memory_quality", {})
        memory_quality = memory_quality_data.get("proof_count_avg", 0.0)
        passport_score = request.passport.get("passport_score", {}).get("total", 0.0)

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
            cursor.execute("""
                SELECT a.*, p.prediction_accuracy
                FROM agents a
                LEFT JOIN passports p ON a.id = p.agent_id
                WHERE a.id IN (
                    SELECT agent_id FROM agents_fts WHERE capabilities MATCH ?
                )
                AND a.trust_score >= ?
                AND a.is_active = 1
                ORDER BY a.trust_score DESC
                LIMIT ?
            """, (capability, min_trust, limit))
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
