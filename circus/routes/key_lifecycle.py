"""Owner key lifecycle routes (W9): discovery, rotation, revocation."""

import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from circus.database import get_db
from circus.routes.agents import verify_token


router = APIRouter(prefix="/api/v1/owners", tags=["key-lifecycle"])


# Request/Response models

class KeyRotateRequest(BaseModel):
    """Request to rotate an owner's key."""
    new_public_key: str
    reason: Optional[str] = "routine-rotation"


class KeyRotateResponse(BaseModel):
    """Response from key rotation."""
    status: str
    previous_key: str
    new_key: str
    rotated_at: str


class KeyRevokeRequest(BaseModel):
    """Request to revoke an owner's key."""
    reason: str = "compromised"


class KeyRevokeResponse(BaseModel):
    """Response from key revocation."""
    status: str
    owner_id: str
    revoked_at: str
    reason: str


class KeyDiscoveryResponse(BaseModel):
    """Response from key discovery endpoint."""
    owner_id: str
    public_key: str
    registered_at: str
    key_event_count: int


# Endpoints

@router.get("/{owner_id}/pubkey", response_model=KeyDiscoveryResponse)
async def discover_owner_key(owner_id: str):
    """
    Discover the current active public key for an owner.

    This is a public endpoint (no auth required) — used by federated nodes
    to verify signatures from remote owners.

    Returns:
        - 200: Active public key found
        - 404: Owner not found
        - 410 Gone: Owner exists but has no active key (all revoked)
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Fetch active key
        cursor.execute("""
            SELECT public_key, created_at
            FROM owner_keys
            WHERE owner_id = ? AND is_active = 1
        """, (owner_id,))
        row = cursor.fetchone()

        if not row:
            # Check if owner exists but all keys revoked (410 Gone)
            cursor.execute("""
                SELECT COUNT(*) FROM owner_keys WHERE owner_id = ?
            """, (owner_id,))
            exists = cursor.fetchone()[0] > 0

            if exists:
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail="Owner has no active key (all keys revoked)"
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Owner not found"
                )

        public_key_b64, created_at = row

        # Count key events for this owner
        cursor.execute("""
            SELECT COUNT(*) FROM key_events WHERE owner_id = ?
        """, (owner_id,))
        event_count = cursor.fetchone()[0]

        return KeyDiscoveryResponse(
            owner_id=owner_id,
            public_key=public_key_b64,
            registered_at=created_at,
            key_event_count=event_count
        )


@router.post("/{owner_id}/rotate-key", response_model=KeyRotateResponse)
async def rotate_owner_key(
    owner_id: str,
    req: KeyRotateRequest,
    agent_id: str = Depends(verify_token)
):
    """
    Rotate an owner's key (operator only, requires ring token auth).

    Marks old key as inactive, inserts new key as active, logs key_events entry.

    Args:
        owner_id: Owner whose key to rotate
        req: New public key and rotation reason
        agent_id: Authenticated agent (ring token required)

    Returns:
        - 200: Key rotated successfully
        - 404: Owner not found
        - 400: No active key to rotate
    """
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Fetch current active key
        cursor.execute("""
            SELECT public_key FROM owner_keys
            WHERE owner_id = ? AND is_active = 1
        """, (owner_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Owner not found or no active key"
            )

        old_public_key = row[0]

        # Mark old key as rotated
        cursor.execute("""
            UPDATE owner_keys
            SET is_active = 0,
                rotated_at = ?,
                superseded_by = ?
            WHERE owner_id = ? AND public_key = ? AND is_active = 1
        """, (now, req.new_public_key, owner_id, old_public_key))

        # Insert new key as active (composite PK allows multiple keys per owner)
        cursor.execute("""
            INSERT INTO owner_keys (owner_id, public_key, created_at, is_active)
            VALUES (?, ?, ?, 1)
        """, (owner_id, req.new_public_key, now))

        # Log key_events entry
        event_id = f"kevent-{secrets.token_hex(8)}"
        cursor.execute("""
            INSERT INTO key_events (
                id, owner_id, event_type, public_key_b64,
                previous_key_b64, reason, happened_at, actor
            ) VALUES (?, ?, 'rotated', ?, ?, ?, ?, ?)
        """, (event_id, owner_id, req.new_public_key, old_public_key, req.reason, now, agent_id))

        conn.commit()

        return KeyRotateResponse(
            status="rotated",
            previous_key=old_public_key,
            new_key=req.new_public_key,
            rotated_at=now
        )


@router.post("/{owner_id}/revoke-key", response_model=KeyRevokeResponse)
async def revoke_owner_key(
    owner_id: str,
    req: KeyRevokeRequest,
    agent_id: str = Depends(verify_token)
):
    """
    Revoke an owner's key (operator only, requires ring token auth).

    Marks current active key as revoked. All future preference publishes
    from this owner will fail at gate 3 (owner_key_unknown).

    Args:
        owner_id: Owner whose key to revoke
        req: Revocation reason
        agent_id: Authenticated agent (ring token required)

    Returns:
        - 200: Key revoked successfully
        - 404: Owner not found or no active key
    """
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Fetch current active key
        cursor.execute("""
            SELECT public_key FROM owner_keys
            WHERE owner_id = ? AND is_active = 1
        """, (owner_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Owner not found or no active key"
            )

        public_key_b64 = row[0]

        # Mark key as revoked
        cursor.execute("""
            UPDATE owner_keys
            SET is_active = 0,
                revoked_at = ?,
                revoked_reason = ?
            WHERE owner_id = ? AND public_key = ? AND is_active = 1
        """, (now, req.reason, owner_id, public_key_b64))

        # Log key_events entry
        event_id = f"kevent-{secrets.token_hex(8)}"
        cursor.execute("""
            INSERT INTO key_events (
                id, owner_id, event_type, public_key_b64,
                reason, happened_at, actor
            ) VALUES (?, ?, 'revoked', ?, ?, ?, ?)
        """, (event_id, owner_id, public_key_b64, req.reason, now, agent_id))

        conn.commit()

        return KeyRevokeResponse(
            status="revoked",
            owner_id=owner_id,
            revoked_at=now,
            reason=req.reason
        )
