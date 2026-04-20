"""Governance API routes (W11) — quarantine + audit."""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from circus.database import get_db
from circus.routes.agents import verify_token
from circus.services import quarantine as quar_service
from circus.services.preference_admission import admit_preference

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/governance", tags=["governance"])


class ReleaseRequest(BaseModel):
    """Request body for quarantine release."""
    admit: bool = Field(default=False, description="Whether to force-activate the preference")
    reason: str = Field(..., description="Human-readable release reason")


@router.get("/quarantine")
async def list_quarantine(
    owner_id: Optional[str] = Query(None, description="Filter by owner"),
    include_released: bool = Query(False, description="Include released entries"),
    agent_id: str = Depends(verify_token),
):
    """List quarantined memories (ring auth required)."""
    with get_db() as conn:
        entries = quar_service.list_quarantined(conn, owner_id, include_released)

        # Enrich with memory details
        cursor = conn.cursor()
        result = []
        for entry in entries:
            cursor.execute(
                """
                SELECT content, category, from_agent_id, shared_at
                FROM shared_memories
                WHERE id = ?
                """,
                (entry.memory_id,)
            )
            mem_row = cursor.fetchone()

            result.append({
                "id": entry.id,
                "memory_id": entry.memory_id,
                "owner_id": entry.owner_id,
                "reason": entry.reason,
                "quarantined_at": entry.quarantined_at,
                "released_at": entry.released_at,
                "released_by": entry.released_by,
                "release_reason": entry.release_reason,
                "auto_release_at": entry.auto_release_at,
                "memory": {
                    "content": mem_row[0] if mem_row else None,
                    "category": mem_row[1] if mem_row else None,
                    "from_agent_id": mem_row[2] if mem_row else None,
                    "shared_at": mem_row[3] if mem_row else None,
                } if mem_row else None,
            })

        return {"quarantined": result, "count": len(result)}


@router.post("/quarantine/{quarantine_id}/release")
async def release_quarantine(
    quarantine_id: str,
    request: ReleaseRequest,
    agent_id: str = Depends(verify_token),
):
    """Release a quarantined memory, optionally force-admitting it.

    If admit=true, the preference is force-activated bypassing confidence gate.
    """

    with get_db() as conn:
        cursor = conn.cursor()

        # Fetch quarantine entry
        cursor.execute(
            """
            SELECT memory_id, owner_id, reason, released_at
            FROM quarantine
            WHERE id = ?
            """,
            (quarantine_id,)
        )
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Quarantine entry not found")

        memory_id, owner_id, quar_reason, released_at = row

        if released_at:
            raise HTTPException(status_code=400, detail="Already released")

        # Release from quarantine
        success = quar_service.release_from_quarantine(
            conn,
            quarantine_id,
            agent_id,
            request.reason,
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to release")

        # If admit=true, force-activate the preference
        if request.admit:
            # Fetch memory details
            cursor.execute(
                """
                SELECT content, from_agent_id, shared_at
                FROM shared_memories
                WHERE id = ?
                """,
                (memory_id,)
            )
            mem_row = cursor.fetchone()

            if not mem_row:
                raise HTTPException(status_code=404, detail="Memory not found")

            content_str, from_agent_id, shared_at = mem_row

            try:
                content = json.loads(content_str)
                provenance = content.get("provenance", {})
                preference_field = provenance.get("preference_field")
                preference_value = provenance.get("preference_value")
                owner_binding = provenance.get("owner_binding")

                if not preference_field or not preference_value:
                    raise HTTPException(status_code=400, detail="Memory is not a preference")

                # Force-activate with confidence=1.0 (operator override)
                from datetime import datetime
                decision = admit_preference(
                    conn,
                    memory_id=memory_id,
                    owner_id=owner_id,
                    preference_field=preference_field,
                    preference_value=preference_value,
                    effective_confidence=1.0,  # Operator override
                    now=datetime.utcnow(),
                    agent_id=from_agent_id,
                    shared_at=shared_at,
                    owner_binding=owner_binding,
                )

                conn.commit()

                return {
                    "released": True,
                    "admitted": decision.admitted,
                    "decision": {
                        "admitted": decision.admitted,
                        "reason": decision.reason,
                        "gates": decision.gates,
                    }
                }

            except (json.JSONDecodeError, KeyError) as e:
                logger.error("Failed to parse memory for admission: %s", e)
                raise HTTPException(status_code=400, detail="Invalid memory format")

        conn.commit()

        return {"released": True, "admitted": False}


@router.post("/quarantine/{quarantine_id}/discard")
async def discard_quarantine(
    quarantine_id: str,
    agent_id: str = Depends(verify_token),
):
    """Discard a quarantined memory (no admission)."""

    with get_db() as conn:
        success = quar_service.discard_from_quarantine(conn, quarantine_id, agent_id)

        if not success:
            raise HTTPException(status_code=404, detail="Quarantine entry not found or already released")

        conn.commit()

        return {"discarded": True}


@router.get("/audit")
async def get_audit_log(
    owner_id: Optional[str] = Query(None, description="Filter by owner"),
    limit: int = Query(100, ge=1, le=500, description="Max entries to return"),
    agent_id: str = Depends(verify_token),
):
    """Get governance audit log (ring auth required).

    Returns unified log of:
    - Preference activations
    - Preference clears
    - Key rotations/revocations
    - Quarantine actions
    """
    with get_db() as conn:
        events = quar_service.get_audit_log(conn, owner_id, limit)

        # Merge with key_events for unified view
        cursor = conn.cursor()

        key_query = """
            SELECT id, event_type, actor, owner_id, reason, happened_at
            FROM key_events
        """
        key_params = []

        if owner_id:
            key_query += " WHERE owner_id = ?"
            key_params.append(owner_id)

        key_query += " ORDER BY happened_at DESC LIMIT ?"
        key_params.append(limit)

        cursor.execute(key_query, key_params)
        key_rows = cursor.fetchall()

        key_events = [
            {
                "id": row[0],
                "event_type": f"key_{row[1]}",  # Prefix to distinguish
                "actor": row[2],
                "owner_id": row[3],
                "detail": {"reason": row[4]} if row[4] else None,
                "happened_at": row[5],
                "source": "key_events",
            }
            for row in key_rows
        ]

        # Mark governance_audit events
        for event in events:
            event["source"] = "governance_audit"

        # Merge and sort by timestamp
        all_events = events + key_events
        all_events.sort(key=lambda x: x["happened_at"], reverse=True)

        return {
            "events": all_events[:limit],
            "count": len(all_events[:limit]),
        }
