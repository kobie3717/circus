"""Token pool management routes."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from circus.config import settings
from circus.database import get_db

router = APIRouter()


def verify_token(authorization: str = Header(...)) -> str:
    """Verify JWT token and return agent_id."""
    from jose import JWTError, jwt

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization[7:]  # Remove "Bearer " prefix

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        agent_id = payload.get("sub")
        jti = payload.get("jti")
        if agent_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        # Require jti claim for all tokens
        if not jti:
            raise HTTPException(status_code=401, detail="Token missing jti claim")
        # Check revocation list — specific jti OR agent-level wildcard
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM token_revocations WHERE jti = ? OR jti = ?",
                (jti, f"agent:{agent_id}")
            )
            if cursor.fetchone():
                raise HTTPException(status_code=401, detail="Token revoked")
        return agent_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


class TokenCheckRequest(BaseModel):
    """Request to check if bot can use tokens."""
    bot_id: str
    session_id: str
    estimated_tokens: Optional[int] = Field(default=None, description="Estimated tokens for this request")


class TokenCheckResponse(BaseModel):
    """Response from token check."""
    tier: str
    delay_ms: int
    pool_pct: float
    bot_pct: float
    message: Optional[str] = None


class TokenRecordRequest(BaseModel):
    """Request to record token usage."""
    bot_id: str
    session_id: str
    tokens_used: int


class TokenRecordResponse(BaseModel):
    """Response from token recording."""
    tier: str
    daily_used: int
    conversation_used: int
    pool_daily_used: int


class TokenPoolStatus(BaseModel):
    """Overall token pool status."""
    daily_budget: int
    daily_used: int
    pool_pct: float
    current_date: str
    tier: str
    bots: list[dict]


def get_today() -> str:
    """Get today's date in ISO format."""
    return datetime.utcnow().date().isoformat()


def reset_pool_if_needed(cursor) -> None:
    """Reset pool and all bots if date changed."""
    today = get_today()

    # Get current pool date
    cursor.execute("SELECT current_date FROM token_pool WHERE id = 1")
    row = cursor.fetchone()

    if not row or row["current_date"] != today:
        # Reset pool
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            UPDATE token_pool
            SET daily_used = 0, current_date = ?, updated_at = ?
            WHERE id = 1
        """, (today, now))

        # Reset all bots
        cursor.execute("""
            UPDATE token_pool_bots
            SET daily_used = 0, conversation_used = 0, current_session = '',
                tier = 'green', updated_at = ?
        """, (now,))


def calculate_tier(pool_pct: float, conversation_limit_exceeded: bool) -> tuple[str, int, Optional[str]]:
    """
    Calculate tier based on pool percentage.

    Returns: (tier_name, delay_ms, optional_message)
    """
    if conversation_limit_exceeded:
        return ("conv_limit", 0, "Conversation token limit exceeded (100k)")

    if pool_pct >= 100:
        return ("exhausted", 0, "Pool exhausted. Resuming tomorrow.")
    elif pool_pct >= 95:
        return ("red", 5000, "Pool critical")
    elif pool_pct >= 85:
        return ("orange", 2000, None)
    elif pool_pct >= 70:
        return ("yellow", 0, f"Pool at {pool_pct:.1f}%, conserving...")
    else:
        return ("green", 0, None)


@router.get("/api/v1/tokens/status", response_model=TokenPoolStatus)
async def get_token_pool_status(agent_id: str = Depends(verify_token)):
    """Get current token pool status and per-bot breakdown."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Reset pool if needed
        reset_pool_if_needed(cursor)

        # Get pool status
        cursor.execute("""
            SELECT daily_budget, daily_used, current_date, updated_at
            FROM token_pool WHERE id = 1
        """)
        pool_row = cursor.fetchone()

        if not pool_row:
            raise HTTPException(status_code=500, detail="Token pool not initialized")

        daily_budget = pool_row["daily_budget"]
        daily_used = pool_row["daily_used"]
        pool_pct = (daily_used / daily_budget * 100) if daily_budget > 0 else 0

        tier, _, _ = calculate_tier(pool_pct, False)

        # Get all bots
        cursor.execute("""
            SELECT bot_id, daily_used, conversation_used, current_session, tier, updated_at
            FROM token_pool_bots
            ORDER BY daily_used DESC
        """)
        bot_rows = cursor.fetchall()

        bots = []
        for bot in bot_rows:
            bot_pct = (bot["daily_used"] / daily_budget * 100) if daily_budget > 0 else 0
            bots.append({
                "bot_id": bot["bot_id"],
                "daily_used": bot["daily_used"],
                "daily_pct": round(bot_pct, 2),
                "conversation_used": bot["conversation_used"],
                "current_session": bot["current_session"],
                "tier": bot["tier"],
                "updated_at": bot["updated_at"]
            })

        return TokenPoolStatus(
            daily_budget=daily_budget,
            daily_used=daily_used,
            pool_pct=round(pool_pct, 2),
            current_date=pool_row["current_date"],
            tier=tier,
            bots=bots
        )


@router.post("/api/v1/tokens/check", response_model=TokenCheckResponse)
async def check_token_availability(request: TokenCheckRequest, agent_id: str = Depends(verify_token)):
    """Check if bot can use tokens and get tier/delay."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Reset pool if needed
        reset_pool_if_needed(cursor)

        # Get pool status
        cursor.execute("SELECT daily_budget, daily_used FROM token_pool WHERE id = 1")
        pool_row = cursor.fetchone()

        if not pool_row:
            raise HTTPException(status_code=500, detail="Token pool not initialized")

        daily_budget = pool_row["daily_budget"]
        daily_used = pool_row["daily_used"]
        pool_pct = (daily_used / daily_budget * 100) if daily_budget > 0 else 0

        # Get or create bot record
        cursor.execute("""
            SELECT daily_used, conversation_used, current_session
            FROM token_pool_bots WHERE bot_id = ?
        """, (request.bot_id,))
        bot_row = cursor.fetchone()

        if not bot_row:
            # Create new bot record
            now = datetime.utcnow().isoformat()
            cursor.execute("""
                INSERT INTO token_pool_bots (bot_id, daily_used, conversation_used,
                                            current_session, tier, updated_at)
                VALUES (?, 0, 0, ?, 'green', ?)
            """, (request.bot_id, request.session_id, now))
            conn.commit()

            bot_daily_used = 0
            bot_conversation_used = 0
            bot_current_session = request.session_id
        else:
            bot_daily_used = bot_row["daily_used"]
            bot_conversation_used = bot_row["conversation_used"]
            bot_current_session = bot_row["current_session"]

        # Check conversation limit
        conversation_limit_exceeded = False
        if bot_current_session == request.session_id and bot_conversation_used > 100000:
            conversation_limit_exceeded = True

        # Calculate tier
        tier, delay_ms, message = calculate_tier(pool_pct, conversation_limit_exceeded)

        # Calculate bot percentage
        bot_pct = (bot_daily_used / daily_budget * 100) if daily_budget > 0 else 0

        return TokenCheckResponse(
            tier=tier,
            delay_ms=delay_ms,
            pool_pct=round(pool_pct, 2),
            bot_pct=round(bot_pct, 2),
            message=message
        )


@router.post("/api/v1/tokens/record", response_model=TokenRecordResponse)
async def record_token_usage(request: TokenRecordRequest, agent_id: str = Depends(verify_token)):
    """Record token usage for a bot."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Reset pool if needed
        reset_pool_if_needed(cursor)

        now = datetime.utcnow().isoformat()

        # Get pool status (daily_budget only, no read-before-write for daily_used)
        cursor.execute("SELECT daily_budget FROM token_pool WHERE id = 1")
        pool_row = cursor.fetchone()

        if not pool_row:
            raise HTTPException(status_code=500, detail="Token pool not initialized")

        daily_budget = pool_row["daily_budget"]

        # H2: Atomic update — increment daily_used in single SQL statement
        cursor.execute("""
            UPDATE token_pool
            SET daily_used = daily_used + ?, updated_at = ?
            WHERE id = 1
        """, (request.tokens_used, now))

        # Read back the new value for response
        cursor.execute("SELECT daily_used FROM token_pool WHERE id = 1")
        new_pool_daily_used = cursor.fetchone()["daily_used"]

        # Get or create bot record
        cursor.execute("""
            SELECT daily_used, conversation_used, current_session
            FROM token_pool_bots WHERE bot_id = ?
        """, (request.bot_id,))
        bot_row = cursor.fetchone()

        if not bot_row:
            # Create new bot record
            cursor.execute("""
                INSERT INTO token_pool_bots (bot_id, daily_used, conversation_used,
                                            current_session, tier, updated_at)
                VALUES (?, ?, ?, ?, 'green', ?)
            """, (request.bot_id, request.tokens_used, request.tokens_used, request.session_id, now))
            new_bot_daily_used = request.tokens_used
            new_conversation_used = request.tokens_used
        else:
            # Check if session changed
            if bot_row["current_session"] != request.session_id:
                # New session - reset conversation counter
                new_conversation_used = request.tokens_used
            else:
                # Same session - increment conversation counter
                new_conversation_used = bot_row["conversation_used"] + request.tokens_used

            new_bot_daily_used = bot_row["daily_used"] + request.tokens_used

            # Calculate tier
            pool_pct = (new_pool_daily_used / daily_budget * 100) if daily_budget > 0 else 0
            conversation_limit_exceeded = new_conversation_used > 100000
            tier, _, _ = calculate_tier(pool_pct, conversation_limit_exceeded)

            # Update bot record
            cursor.execute("""
                UPDATE token_pool_bots
                SET daily_used = ?, conversation_used = ?, current_session = ?,
                    tier = ?, updated_at = ?
                WHERE bot_id = ?
            """, (new_bot_daily_used, new_conversation_used, request.session_id, tier, now, request.bot_id))

        conn.commit()

        # Calculate final tier
        pool_pct = (new_pool_daily_used / daily_budget * 100) if daily_budget > 0 else 0
        conversation_limit_exceeded = new_conversation_used > 100000
        tier, _, _ = calculate_tier(pool_pct, conversation_limit_exceeded)

        return TokenRecordResponse(
            tier=tier,
            daily_used=new_bot_daily_used,
            conversation_used=new_conversation_used,
            pool_daily_used=new_pool_daily_used
        )


@router.post("/api/v1/tokens/reset")
async def reset_token_pool(authorization: str = Header(...)):
    """Reset token pool (owner-only, for testing/emergency).

    C3: Uses CIRCUS_OWNER_KEY env var for admin auth (separate from JWT secret).
    Falls back to CIRCUS_SECRET_KEY for backward compatibility.
    """
    import os

    # Verify secret key
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization[7:]  # Remove "Bearer " prefix

    # C3: Use separate admin key (CIRCUS_OWNER_KEY) for owner operations
    # CIRCUS_SECRET_KEY is used for JWT signing; CIRCUS_OWNER_KEY is for admin ops
    admin_key = os.environ.get('CIRCUS_OWNER_KEY', settings.secret_key)

    if token != admin_key:
        raise HTTPException(status_code=403, detail="Invalid secret key")

    with get_db() as conn:
        cursor = conn.cursor()

        today = get_today()
        now = datetime.utcnow().isoformat()

        # Reset pool
        cursor.execute("""
            UPDATE token_pool
            SET daily_used = 0, current_date = ?, updated_at = ?
            WHERE id = 1
        """, (today, now))

        # Reset all bots
        cursor.execute("""
            UPDATE token_pool_bots
            SET daily_used = 0, conversation_used = 0, current_session = '',
                tier = 'green', updated_at = ?
        """, (now,))

        conn.commit()

        return {
            "status": "reset",
            "current_date": today,
            "updated_at": now
        }
