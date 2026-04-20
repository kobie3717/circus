"""Federation routes for cross-Circus agent discovery (TRQP) and Memory Commons."""

import json
import logging
import secrets
import time
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response, Request

from circus.database import get_db
from circus.models import AgentResponse
from circus.routes.agents import verify_token
from circus.services.signing import encode_public_key, decode_public_key
from circus.services.trust import can_moderate
from circus.services.federation_auth import verify_peer_challenge, AuthError
from circus.services.federation_pull import pull_bundles, CursorError
from circus.services.federation_admission import admit_bundle
from circus.services.federation_wiring import admit_and_merge

router = APIRouter()
logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when peer exceeds rate limit quota."""
    pass


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
        verify_peer_challenge("pull", peer_id, peer_signature)
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


def _enforce_rate_limit(peer_id: str) -> None:
    """Enforce 100 req/peer/min rate limit.

    Args:
        peer_id: Peer identifier

    Raises:
        RateLimitExceeded: If peer has exceeded quota for current minute bucket
    """
    current_bucket = int(time.time() / 60)

    with get_db() as conn:
        cursor = conn.cursor()

        # Check current count
        cursor.execute("""
            SELECT request_count
            FROM federation_rate_limits
            WHERE peer_id = ? AND window_start = ?
        """, (peer_id, current_bucket))
        row = cursor.fetchone()

        if row and row["request_count"] >= 100:
            raise RateLimitExceeded(f"Exceeded 100 req/min for peer {peer_id}")

        # Increment counter
        cursor.execute("""
            INSERT INTO federation_rate_limits (peer_id, window_start, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT (peer_id, window_start)
            DO UPDATE SET request_count = request_count + 1
        """, (peer_id, current_bucket))

        # Lazy cleanup: delete stale windows (keep last 10 minutes)
        stale_cutoff = current_bucket - 10
        cursor.execute("""
            DELETE FROM federation_rate_limits
            WHERE peer_id = ? AND window_start < ?
        """, (peer_id, stale_cutoff))

        conn.commit()


@router.post("/push")
async def push_federation_bundle(
    request: Request,
    peer_id_header: Optional[str] = Header(None, alias="X-Peer-Id"),
    peer_signature: Optional[str] = Header(None, alias="X-Peer-Signature"),
):
    """Federation PUSH endpoint — receive signed bundles from peers.

    Peers proactively deliver bundles for admission. This endpoint verifies
    authentication, enforces rate limits, and delegates to admit_bundle()
    for verification pipeline (signature, passport, dedup, persistence).

    All business outcomes (admitted/skipped/quarantined/rejected) return 200.
    Only infra_error returns 500. Uniform contract — caller inspects decision.

    Authentication: Challenge-based Ed25519 signature over "push:{peer_id}:{minute_bucket}"
    with ±1 minute clock skew tolerance.

    Rate limit: 100 requests/peer/minute (SQLite-backed, lazy cleanup).

    Args:
        request: FastAPI request object (for body parsing)
        peer_id_header: Pushing peer's identifier (X-Peer-Id header)
        peer_signature: Ed25519 signature over challenge (X-Peer-Signature header)

    Returns:
        JSON with decision, bundle_id, audit_id, counters (if admitted)

    Raises:
        400: Structurally bad request body (invalid JSON, missing peer_id field)
        401: Missing/bad/mismatched auth material (headers, signature, peer_id mismatch)
        403: Known peer but not allowed (not registered / inactive)
        429: Rate limit exceeded
        500: Internal error (infra_error or exception)
    """
    # 1. Auth headers present? (Header(None) + explicit 401, NOT FastAPI's 422)
    if peer_id_header is None:
        raise HTTPException(status_code=401, detail="Missing X-Peer-Id header")
    if peer_signature is None:
        raise HTTPException(status_code=401, detail="Missing X-Peer-Signature header")

    # 2. Parse body
    try:
        bundle = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 3. Extract peer_id from body (structural — 400 if missing)
    bundle_peer_id = bundle.get("peer_id")
    if not bundle_peer_id or not isinstance(bundle_peer_id, str):
        raise HTTPException(status_code=400, detail="Missing peer_id in body")

    # 4. Header/body match (auth-shape failure — 401, not 400)
    if peer_id_header != bundle_peer_id:
        raise HTTPException(
            status_code=401,
            detail="peer_id mismatch between header and body"
        )

    # 5. Auth (signature verification)
    try:
        verify_peer_challenge("push", bundle_peer_id, peer_signature)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    # 6. Rate limit
    try:
        _enforce_rate_limit(bundle_peer_id)
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "60"}
        )

    # 7. Admission
    now = datetime.utcnow()
    try:
        result = admit_bundle(bundle, now=now)
    except Exception as exc:
        logger.error("admit_bundle raised: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")

    # 8. Wiring (if admitted)
    conflicts = []
    if result.decision == "admitted":
        try:
            conflicts = admit_and_merge(bundle, peer_id=bundle_peer_id, now=now)
        except Exception as exc:
            logger.error("admit_and_merge raised: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Internal error")

    # 9. Map result to response
    if result.decision == "infra_error":
        raise HTTPException(
            status_code=500,
            detail=result.detail or "Infra error"
        )

    resp = {"decision": result.decision, "bundle_id": result.bundle_id}
    if result.reason:
        resp["reason"] = result.reason
    if result.quarantine_id:
        resp["quarantine_id"] = result.quarantine_id
    if result.audit_id:
        resp["audit_id"] = result.audit_id
    if result.decision == "admitted":
        resp.update({
            "memories_total": result.memories_total,
            "memories_new": result.memories_new,
            "memories_skipped": result.memories_skipped,
            "conflicts_detected": len(conflicts),
        })
    if result.detail:
        resp["detail"] = result.detail

    return resp


# W10: Federation outbox endpoints

@router.get("/api/v1/federation/peers")
async def get_federation_peers(agent_id: str = Depends(verify_token)):
    """List all federation peers with health status (ring token auth)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT url, name, last_seen_at, last_failed_at,
                   consecutive_failures, is_healthy, registered_at
            FROM federation_peers
            ORDER BY name
        """)

        peers = []
        for row in cursor.fetchall():
            peers.append({
                "url": row["url"],
                "name": row["name"] or row["url"],
                "last_seen_at": row["last_seen_at"],
                "last_failed_at": row["last_failed_at"],
                "consecutive_failures": row["consecutive_failures"],
                "is_healthy": bool(row["is_healthy"]),
                "registered_at": row["registered_at"]
            })

        return {"peers": peers, "count": len(peers)}


@router.get("/api/v1/federation/outbox")
async def get_federation_outbox(
    status: Optional[str] = Query(None, pattern="^(pending|delivered|failed|abandoned)$"),
    agent_id: str = Depends(verify_token)
):
    """List outbox entries (ring token auth)."""
    with get_db() as conn:
        cursor = conn.cursor()

        if status:
            cursor.execute("""
                SELECT id, peer_url, memory_id, status, attempt_count,
                       last_attempted_at, delivered_at, error, created_at, next_retry_at
                FROM federation_outbox
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT 100
            """, (status,))
        else:
            cursor.execute("""
                SELECT id, peer_url, memory_id, status, attempt_count,
                       last_attempted_at, delivered_at, error, created_at, next_retry_at
                FROM federation_outbox
                ORDER BY created_at DESC
                LIMIT 100
            """)

        entries = []
        for row in cursor.fetchall():
            entries.append({
                "id": row["id"],
                "peer_url": row["peer_url"],
                "memory_id": row["memory_id"],
                "status": row["status"],
                "attempt_count": row["attempt_count"],
                "last_attempted_at": row["last_attempted_at"],
                "delivered_at": row["delivered_at"],
                "error": row["error"],
                "created_at": row["created_at"],
                "next_retry_at": row["next_retry_at"]
            })

        return {"entries": entries, "count": len(entries)}


@router.get("/api/v1/federation/metrics")
async def get_federation_metrics():
    """Get federation outbox metrics (public, no auth)."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Count by status
        cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM federation_outbox
            GROUP BY status
        """)

        counts = {row["status"]: row["count"] for row in cursor.fetchall()}

        return {
            "pending": counts.get("pending", 0),
            "delivered": counts.get("delivered", 0),
            "failed": counts.get("failed", 0),
            "abandoned": counts.get("abandoned", 0),
            "total": sum(counts.values())
        }


@router.post("/api/v1/federation/peers")
async def add_federation_peer(
    url: str,
    name: Optional[str] = None,
    agent_id: str = Depends(verify_token)
):
    """Register a federation peer (ring token auth)."""
    now = datetime.utcnow().isoformat()

    with get_db() as conn:
        cursor = conn.cursor()

        # Check if peer already exists
        cursor.execute("SELECT url FROM federation_peers WHERE url = ?", (url,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Peer already registered")

        # Generate peer ID
        peer_id = f"peer-{secrets.token_hex(8)}"

        # Insert peer (public_key is NOT NULL, use dummy for outbox-only peers)
        dummy_key = b'\x00' * 32
        cursor.execute("""
            INSERT INTO federation_peers (
                id, url, name, public_key, created_at, registered_at, is_healthy, consecutive_failures
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 0)
        """, (peer_id, url, name or url, dummy_key, now, now))

        conn.commit()

    return {"status": "registered", "url": url, "name": name or url}


@router.delete("/api/v1/federation/peers/{url:path}")
async def remove_federation_peer(
    url: str,
    agent_id: str = Depends(verify_token)
):
    """Remove a federation peer (ring token auth)."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("DELETE FROM federation_peers WHERE url = ?", (url,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Peer not found")

        conn.commit()

    return {"status": "removed", "url": url}
