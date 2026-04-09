"""Federation routes for cross-Circus agent discovery (TRQP)."""

import json
import secrets
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from circus.database import get_db
from circus.models import AgentResponse
from circus.routes.agents import verify_token
from circus.services.signing import encode_public_key, decode_public_key
from circus.services.trust import can_moderate

router = APIRouter()


@router.post("/peers")
async def register_peer(
    name: str,
    url: str,
    public_key_b64: str,
    agent_id: str = Depends(verify_token)
):
    """Register a federation peer (Elders only)."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if agent is Elder
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()

        if not row or not can_moderate(row["trust_score"]):
            raise HTTPException(status_code=403, detail="Requires Elder tier")

        # Decode public key
        public_key_bytes = decode_public_key(public_key_b64)

        # Create peer
        peer_id = f"peer-{secrets.token_hex(4)}"
        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO federation_peers (
                id, name, url, public_key, created_at
            ) VALUES (?, ?, ?, ?, ?)
        """, (peer_id, name, url, public_key_bytes, now))

        conn.commit()

    return {
        "peer_id": peer_id,
        "name": name,
        "url": url,
        "status": "registered"
    }


@router.get("/peers")
async def list_peers():
    """List all federation peers."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, url, trust_score, last_sync, is_active
            FROM federation_peers
            WHERE is_active = 1
        """)

        peers = []
        for row in cursor.fetchall():
            peers.append({
                "peer_id": row["id"],
                "name": row["name"],
                "url": row["url"],
                "trust_score": row["trust_score"],
                "last_sync": row["last_sync"],
                "is_active": bool(row["is_active"])
            })

        return peers


@router.get("/discover")
async def federated_discovery(
    capability: Optional[str] = Query(None),
    min_trust: float = Query(30.0, ge=0, le=100),
    limit: int = Query(20, ge=1, le=100),
    include_local: bool = Query(True),
    agent_id: str = Depends(verify_token)
):
    """
    Query agents across all federation peers (TRQP).

    Returns aggregated results from local + remote Circus instances.
    """
    all_agents = []

    # Get local agents first
    if include_local:
        with get_db() as conn:
            cursor = conn.cursor()

            if capability:
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
                cursor.execute("""
                    SELECT a.*, p.prediction_accuracy
                    FROM agents a
                    LEFT JOIN passports p ON a.id = p.agent_id
                    WHERE a.trust_score >= ?
                    AND a.is_active = 1
                    ORDER BY a.trust_score DESC
                    LIMIT ?
                """, (min_trust, limit))

            for row in cursor.fetchall():
                all_agents.append({
                    "agent_id": row["id"],
                    "name": row["name"],
                    "role": row["role"],
                    "capabilities": json.loads(row["capabilities"]),
                    "home_instance": row["home_instance"],
                    "trust_score": row["trust_score"],
                    "trust_tier": row["trust_tier"],
                    "prediction_accuracy": row["prediction_accuracy"],
                    "registered_at": row["registered_at"],
                    "last_seen": row["last_seen"],
                    "source": "local"
                })

    # Query federation peers
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, url FROM federation_peers WHERE is_active = 1
        """)
        peers = cursor.fetchall()

    async with httpx.AsyncClient(timeout=5.0) as client:
        for peer in peers:
            try:
                # Query remote Circus instance
                params = {
                    "min_trust": min_trust,
                    "limit": limit
                }
                if capability:
                    params["capability"] = capability

                response = await client.get(
                    f"{peer['url']}/api/v1/agents/discover",
                    params=params
                )

                if response.status_code == 200:
                    remote_data = response.json()
                    for agent in remote_data.get("agents", []):
                        agent["source"] = peer["url"]
                        all_agents.append(agent)

                        if len(all_agents) >= limit * 3:  # Cap at 3x limit
                            break

            except Exception as e:
                # Log federation query failure
                now = datetime.utcnow().isoformat()
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO federation_sync_log (
                            peer_id, direction, status, error, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                    """, (peer["id"], "pull", "failed", str(e), now))
                    conn.commit()

    # Sort by trust score and limit
    all_agents.sort(key=lambda x: x["trust_score"], reverse=True)
    all_agents = all_agents[:limit]

    return {
        "agents": all_agents,
        "count": len(all_agents),
        "sources": {
            "local": sum(1 for a in all_agents if a["source"] == "local"),
            "remote": sum(1 for a in all_agents if a["source"] != "local")
        }
    }
