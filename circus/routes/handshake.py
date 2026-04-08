"""P2P handshake routes."""

import json
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from jose import jwt

from circus.config import settings
from circus.database import get_db
from circus.models import AgentResponse, HandshakeRequest, HandshakeResponse
from circus.routes.agents import verify_token

router = APIRouter()


@router.post("/handshake", response_model=HandshakeResponse)
async def initiate_handshake(
    request: HandshakeRequest,
    agent_id: str = Depends(verify_token)
):
    """Initiate P2P handshake with another agent."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get target agent
        cursor.execute("""
            SELECT a.*, p.prediction_accuracy
            FROM agents a
            LEFT JOIN passports p ON a.id = p.agent_id
            WHERE a.id = ?
        """, (request.target_agent_id,))
        target_row = cursor.fetchone()

        if not target_row:
            raise HTTPException(status_code=404, detail="Target agent not found")

        # Check trust score threshold
        if target_row["trust_score"] < 30:
            raise HTTPException(
                status_code=403,
                detail="Target agent trust score too low (need 30+)"
            )

        # Simple shared entities check (would be more sophisticated in production)
        # For now, just check if both agents are members of any shared rooms
        cursor.execute("""
            SELECT DISTINCT r.name
            FROM room_members rm1
            JOIN room_members rm2 ON rm1.room_id = rm2.room_id
            JOIN rooms r ON rm1.room_id = r.id
            WHERE rm1.agent_id = ? AND rm2.agent_id = ?
        """, (agent_id, request.target_agent_id))

        shared_rooms = [row[0] for row in cursor.fetchall()]
        shared_entities = shared_rooms  # In full implementation, would extract from passports

        if not shared_entities:
            # Allow handshake anyway but note lack of common ground
            shared_entities = ["none"]

        # Create handshake token
        handshake_id = f"hs-{secrets.token_hex(6)}"
        expires_at = datetime.utcnow() + timedelta(hours=24)

        handshake_token = jwt.encode(
            {
                "sub": handshake_id,
                "agent_a": agent_id,
                "agent_b": request.target_agent_id,
                "purpose": request.purpose,
                "exp": expires_at,
                "iat": datetime.utcnow(),
            },
            settings.secret_key,
            algorithm=settings.algorithm
        )

        # Store handshake
        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO handshakes (
                id, agent_a_id, agent_b_id, token_hash, purpose,
                shared_entities, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            handshake_id, agent_id, request.target_agent_id,
            handshake_token[:32],  # Store partial hash for lookup
            request.purpose,
            json.dumps(shared_entities),
            now,
            expires_at.isoformat()
        ))

        conn.commit()

        # Build target agent response
        target_agent_response = AgentResponse(
            agent_id=target_row["id"],
            name=target_row["name"],
            role=target_row["role"],
            capabilities=json.loads(target_row["capabilities"]),
            home_instance=target_row["home_instance"],
            trust_score=target_row["trust_score"],
            trust_tier=target_row["trust_tier"],
            prediction_accuracy=target_row["prediction_accuracy"],
            registered_at=target_row["registered_at"],
            last_seen=target_row["last_seen"]
        )

        return HandshakeResponse(
            handshake_id=handshake_id,
            handshake_token=handshake_token,
            target_agent=target_agent_response,
            shared_entities=shared_entities,
            expires_at=expires_at.isoformat()
        )
