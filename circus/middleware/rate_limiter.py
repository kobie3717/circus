"""Per-token rate limiting middleware."""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict

from fastapi import HTTPException, Request
from jose import jwt, JWTError

from circus.config import settings


# In-memory rate limit store (consider Redis for production)
# Structure: {agent_id: [(timestamp, count), ...]}
rate_limits: Dict[str, list] = defaultdict(list)

# Rate limit configuration by trust tier
TIER_LIMITS = {
    "Newcomer": {"requests": 100, "window_minutes": 60},
    "Contributor": {"requests": 500, "window_minutes": 60},
    "Steward": {"requests": 2000, "window_minutes": 60},
    "Elder": {"requests": 10000, "window_minutes": 60},
    "Established": {"requests": 500, "window_minutes": 60},
    "Trusted": {"requests": 2000, "window_minutes": 60},
}

# Anonymous rate limit (no token)
ANONYMOUS_LIMIT = {"requests": 30, "window_minutes": 60}


def get_agent_from_token(authorization: str) -> tuple[str | None, str]:
    """Extract agent_id and trust_tier from JWT token."""
    if not authorization or not authorization.startswith("Bearer "):
        return None, "Newcomer"

    token = authorization[7:]

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        agent_id = payload.get("sub")
        # We need to look up trust tier from DB
        from circus.database import get_db
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trust_tier FROM agents WHERE id = ?", (agent_id,))
            row = cursor.fetchone()
            trust_tier = row["trust_tier"] if row else "Newcomer"

        return agent_id, trust_tier
    except JWTError:
        return None, "Newcomer"


async def check_rate_limit(request: Request):
    """Check rate limit for current request."""
    # Skip rate limiting for health/docs endpoints
    if request.url.path in ["/health", "/docs", "/openapi.json", "/redoc", "/.well-known/agent.json"]:
        return

    # Skip rate limiting for federation PUSH (has its own per-peer rate limiting).
    # PULL stays under the generic middleware until per-peer pull limits land.
    if request.url.path == "/api/v1/federation/push":
        return

    authorization = request.headers.get("authorization")
    agent_id, trust_tier = get_agent_from_token(authorization)

    # Use IP as fallback for unauthenticated requests
    identifier = agent_id or request.client.host

    # Get rate limit config for tier
    if agent_id:
        config = TIER_LIMITS.get(trust_tier, TIER_LIMITS["Newcomer"])
    else:
        config = ANONYMOUS_LIMIT

    max_requests = config["requests"]
    window_minutes = config["window_minutes"]

    # Clean old entries
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=window_minutes)
    rate_limits[identifier] = [
        (ts, count) for ts, count in rate_limits[identifier] if ts > cutoff
    ]

    # Count requests in window
    total_requests = sum(count for _, count in rate_limits[identifier])

    if total_requests >= max_requests:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Tier: {trust_tier}, Limit: {max_requests}/{window_minutes}m"
        )

    # Record this request
    rate_limits[identifier].append((now, 1))
