"""Federation routes for cross-Circus agent discovery (TRQP) and Memory Commons."""

import json
import secrets
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response

from circus.database import get_db
from circus.models import AgentResponse
from circus.routes.agents import verify_token
from circus.services.signing import encode_public_key, decode_public_key
from circus.services.trust import can_moderate
from circus.services.federation_auth import verify_pull_challenge, AuthError
from circus.services.federation_pull import pull_bundles, CursorError

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


@router.get("/pull")
async def pull_federation_bundles(
    response: Response,
    since: Optional[str] = Query(None, description="Opaque cursor for pagination"),
    limit: int = Query(50, ge=1, le=100, description="Page size (max 100)"),
    domain: Optional[str] = Query(None, description="Filter by memory domain"),
    peer_id: str = Header(..., alias="X-Peer-Id"),
    peer_signature: str = Header(..., alias="X-Peer-Signature"),
):
    """Federation PULL endpoint — emit signed bundles to peers.

    Clients MUST verify each returned bundle via admit_bundle() before
    trusting its contents. This endpoint is transport-only, NOT a trust
    boundary.

    Authentication: Challenge-based Ed25519 signature over "pull:{peer_id}:{minute_bucket}"
    with ±1 minute clock skew tolerance.

    Response header X-Admission-Required: true indicates receiver must run
    full verification pipeline (signature + passport + peer trust) via
    admit_bundle() on each bundle.

    Args:
        since: Opaque cursor from previous response (exclusive pagination)
        limit: Max bundles to return (clamped to 100)
        domain: Optional domain filter (narrows to matching memories only)
        peer_id: Pulling peer's identifier (X-Peer-Id header)
        peer_signature: Ed25519 signature over challenge (X-Peer-Signature header)

    Returns:
        JSON with bundles[], next_cursor, has_more, server_time

    Raises:
        401: Invalid/missing signature or expired timestamp
        403: Peer not registered or inactive
        400: Malformed cursor
        500: Internal error (DB/signing failure)
    """
    # Add response header
    response.headers["X-Admission-Required"] = "true"

    # 1. Validate authentication
    try:
        verify_pull_challenge(peer_id, peer_signature)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    # 2. Query bundles
    try:
        with get_db() as conn:
            bundles, next_cursor, has_more = pull_bundles(
                conn,
                puller_peer_id=peer_id,
                since_cursor=since,
                limit=limit,
                domain=domain
            )
            conn.commit()  # Passport cache writes need commit

    except CursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        # Log internal error
        import logging
        logging.getLogger(__name__).error(
            "PULL endpoint internal error: %s",
            exc,
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

    # 3. Build response
    return {
        "bundles": bundles,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "server_time": datetime.utcnow().isoformat(),
    }
