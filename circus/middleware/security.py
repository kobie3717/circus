"""OWASP security middleware for The Circus."""

import re
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Request
from jose import jwt, JWTError

from circus.config import settings
from circus.database import get_db


# Capability gating by trust tier
CAPABILITY_GATES = {
    "create_room": 60,      # Trusted tier
    "vouch": 60,            # Trusted tier
    "moderate": 85,         # Elder tier
    "create_task": 30,      # Established tier
    "federation_sync": 85,  # Elder tier
}


# SQL injection patterns (basic detection)
SQL_INJECTION_PATTERNS = [
    r"(\bunion\b.*\bselect\b)",
    r"(\bor\b\s+\d+\s*=\s*\d+)",
    r"(\bdrop\b\s+\btable\b)",
    r"(\binsert\b\s+\binto\b)",
    r"(\bdelete\b\s+\bfrom\b)",
    r"(--;)",
    r"(/\*.*\*/)",
]


def detect_injection_attempt(text: str) -> bool:
    """Detect SQL injection patterns in input."""
    text_lower = text.lower()
    for pattern in SQL_INJECTION_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


def get_agent_context(request: Request) -> tuple[Optional[str], str]:
    """Extract agent_id and trust_tier from request."""
    authorization = request.headers.get("authorization")

    if not authorization or not authorization.startswith("Bearer "):
        return None, "Newcomer"

    token = authorization[7:]

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        agent_id = payload.get("sub")

        # Look up trust tier
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trust_tier FROM agents WHERE id = ?", (agent_id,))
            row = cursor.fetchone()
            trust_tier = row["trust_tier"] if row else "Newcomer"

        return agent_id, trust_tier
    except JWTError:
        return None, "Newcomer"


def check_capability_gate(
    capability: str,
    trust_tier: str
) -> bool:
    """Check if trust tier has permission for capability."""
    required_score = CAPABILITY_GATES.get(capability)
    if required_score is None:
        return True  # No gate for this capability

    tier_scores = {
        "Newcomer": 15,
        "Established": 45,
        "Trusted": 72,
        "Elder": 92
    }

    current_score = tier_scores.get(trust_tier, 0)
    return current_score >= required_score


def log_audit_event(
    agent_id: Optional[str],
    action: str,
    resource_type: Optional[str],
    resource_id: Optional[str],
    trust_tier: str,
    allowed: bool,
    reason: Optional[str],
    ip_address: str
):
    """Log security audit event."""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO audit_log (
                agent_id, action, resource_type, resource_id,
                trust_tier, allowed, reason, ip_address, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id, action, resource_type, resource_id,
            trust_tier, 1 if allowed else 0, reason, ip_address, now
        ))

        conn.commit()


async def security_middleware(request: Request, call_next):
    """OWASP security middleware."""
    from fastapi.responses import JSONResponse

    # Skip for health/docs
    if request.url.path in ["/health", "/docs", "/openapi.json", "/.well-known/agent.json", "/"]:
        return await call_next(request)

    agent_id, trust_tier = get_agent_context(request)
    ip_address = request.client.host if request.client else "unknown"

    # Injection detection on query parameters
    for param_name, param_value in request.query_params.items():
        if isinstance(param_value, str) and detect_injection_attempt(param_value):
            log_audit_event(
                agent_id, "injection_attempt", "query_param",
                param_name, trust_tier, False,
                f"SQL injection pattern in {param_name}",
                ip_address
            )
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid input detected"}
            )

    # Capability gating for specific endpoints
    # Only gate POST requests that create new resources
    if request.method == "POST":
        # Check for room creation (requires Trusted tier)
        if request.url.path == "/api/v1/rooms":
            if not check_capability_gate("create_room", trust_tier):
                log_audit_event(
                    agent_id, "create_room", "endpoint",
                    request.url.path, trust_tier, False,
                    f"Insufficient trust tier: {trust_tier}",
                    ip_address
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Insufficient trust tier for create_room"}
                )
            log_audit_event(
                agent_id, "create_room", "endpoint",
                request.url.path, trust_tier, True, None, ip_address
            )

        # Check for vouch (requires Trusted tier)
        elif "/vouch" in request.url.path and "/api/v1/agents/" in request.url.path:
            if not check_capability_gate("vouch", trust_tier):
                log_audit_event(
                    agent_id, "vouch", "endpoint",
                    request.url.path, trust_tier, False,
                    f"Insufficient trust tier: {trust_tier}",
                    ip_address
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Insufficient trust tier for vouch"}
                )
            log_audit_event(
                agent_id, "vouch", "endpoint",
                request.url.path, trust_tier, True, None, ip_address
            )

        # Check for task creation (requires Established tier)
        elif request.url.path == "/api/v1/tasks":
            if not check_capability_gate("create_task", trust_tier):
                log_audit_event(
                    agent_id, "create_task", "endpoint",
                    request.url.path, trust_tier, False,
                    f"Insufficient trust tier: {trust_tier}",
                    ip_address
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Insufficient trust tier for create_task"}
                )
            log_audit_event(
                agent_id, "create_task", "endpoint",
                request.url.path, trust_tier, True, None, ip_address
            )

    response = await call_next(request)
    return response
