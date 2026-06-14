"""FastAPI application setup for The Circus."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from circus.config import settings

logger = logging.getLogger(__name__)
from circus.database import init_database, seed_default_rooms, get_db
from circus.models import HealthResponse
from circus.routes import agents, rooms, handshake, sse, tasks, credentials, federation, memory_commons, key_lifecycle, governance, routing, graphs, tokens, troupes, backing, pools, memory_v2, escrow, task_events, platform, docvault
from circus.trust import apply_trust_decay, get_trust_tier
from circus.middleware.rate_limiter import check_rate_limit
from circus.middleware.telemetry import setup_tracing, get_current_trace_id
from circus.middleware.security import security_middleware


async def trust_decay_task():
    """Background task to apply trust decay to inactive agents."""
    while True:
        try:
            # Run every 24 hours
            await asyncio.sleep(86400)

            with get_db() as conn:
                cursor = conn.cursor()

                # Find agents inactive for 30+ days
                thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).isoformat()
                ninety_days_ago = (datetime.utcnow() - timedelta(days=90)).isoformat()

                cursor.execute("""
                    SELECT id, trust_score, last_seen
                    FROM agents
                    WHERE is_active = 1 AND last_seen < ?
                """, (thirty_days_ago,))

                agents = cursor.fetchall()
                now = datetime.utcnow().isoformat()

                for agent in agents:
                    agent_id = agent["id"]
                    current_trust = agent["trust_score"]
                    last_seen = datetime.fromisoformat(agent["last_seen"])
                    days_inactive = (datetime.utcnow() - last_seen).days

                    # Apply decay
                    new_trust = apply_trust_decay(current_trust, days_inactive)

                    if new_trust != current_trust:
                        delta = new_trust - current_trust
                        new_tier = get_trust_tier(new_trust)

                        # Update agent
                        cursor.execute("""
                            UPDATE agents SET trust_score = ?, trust_tier = ? WHERE id = ?
                        """, (new_trust, new_tier, agent_id))

                        # Log trust event
                        event_type = "inactivity_90d" if days_inactive >= 90 else "inactivity_30d"
                        cursor.execute("""
                            INSERT INTO trust_events (agent_id, event_type, delta, reason, created_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            agent_id,
                            event_type,
                            delta,
                            f"Inactive for {days_inactive} days",
                            now
                        ))

                conn.commit()

        except Exception as e:
            print(f"Error in trust decay task: {e}")


async def liveness_monitor_task():
    """Mark agents inactive when no heartbeat for 15 minutes."""
    while True:
        try:
            await asyncio.sleep(300)  # Check every 5 minutes
            cutoff = (datetime.utcnow() - timedelta(minutes=15)).isoformat()
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE agents SET is_active = 0 WHERE is_active = 1 AND last_seen < ? AND trust_tier != 'Elder'",
                    (cutoff,)
                )
                if cursor.rowcount:
                    print(f"[Liveness] Marked {cursor.rowcount} agent(s) inactive (no heartbeat >15min)")
                conn.commit()
        except Exception as e:
            print(f"[Liveness] Monitor error: {e}")


async def _warmup_embeddings():
    """Warm up sentence-transformers model to avoid 14-16s cold start on first /register."""
    try:
        from circus.services.embeddings import embed_agent_profile
        # Dummy warmup call with minimal data
        await embed_agent_profile("warmup-agent", "system", [])
        logger.info("[Startup] Sentence-transformers model warmed up")
    except Exception as e:
        logger.warning(f"[Startup] Embedding warmup failed (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup: validate required configuration
    if not settings.secret_key or settings.secret_key == "":
        raise RuntimeError(
            "CIRCUS_SECRET_KEY environment variable must be set. "
            "Generate with: openssl rand -hex 32"
        )

    init_database()
    seed_default_rooms()

    # H4: DB table existence check on startup
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agents'")
        if not cursor.fetchone():
            raise RuntimeError("Critical: agents table missing after init")

    # Warm up embedding model to prevent 14-16s cold start on first /register
    asyncio.create_task(_warmup_embeddings())

    # Start background tasks
    trust_task = asyncio.create_task(trust_decay_task())
    liveness_task = asyncio.create_task(liveness_monitor_task())

    # Start federation worker (W10)
    from circus.services.federation_worker import run_federation_worker
    federation_task = asyncio.create_task(run_federation_worker())

    # Start federation outbox worker (W10 - outbound delivery)
    from circus.federation_outbox_worker import start_federation_outbox_worker
    federation_outbox_task = asyncio.create_task(start_federation_outbox_worker())

    # Start webhook delivery worker (Phase 6)
    from circus.webhook_worker import start_webhook_worker
    webhook_task = asyncio.create_task(start_webhook_worker())

    # Start scheduler (memory consolidation + escrow auto-release)
    from circus.scheduler import start_scheduler
    scheduler_tasks = await start_scheduler(str(settings.database_path))

    yield

    # Shutdown
    trust_task.cancel()
    liveness_task.cancel()
    federation_task.cancel()
    federation_outbox_task.cancel()
    webhook_task.cancel()
    for t in scheduler_tasks:
        t.cancel()
    for task in [trust_task, liveness_task, federation_task, federation_outbox_task, webhook_task] + scheduler_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Circus — ai-mesh Coordination Layer",
    version="0.6.0",
    description="The standard multi-agent coordination protocol. Trust tiers, capability attestation, adaptive memory, attack-resistant escrow, doom-loop detection.",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# Setup OpenTelemetry tracing
setup_tracing(app)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Trace ID middleware
@app.middleware("http")
async def add_trace_id_header(request: Request, call_next):
    """Add X-Trace-ID header to all responses."""
    response = await call_next(request)
    trace_id = get_current_trace_id()
    if trace_id:
        response.headers["X-Trace-ID"] = trace_id
    return response


# Security middleware (OWASP)
@app.middleware("http")
async def security_middleware_handler(request: Request, call_next):
    """Security middleware wrapper."""
    return await security_middleware(request, call_next)


# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Rate limiting middleware."""
    await check_rate_limit(request)
    response = await call_next(request)
    return response


# Exception handlers
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Handle ValueError exceptions."""
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc), "error_code": "VALIDATION_ERROR"}
    )


@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError):
    """Handle PermissionError exceptions."""
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc), "error_code": "PERMISSION_DENIED"}
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle all unhandled exceptions (M2)."""
    logger.error(f"Unhandled exception on {request.url}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error_code": "INTERNAL_ERROR"}
    )


# Health check endpoint
@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Health check endpoint."""
    trace_id = get_current_trace_id()
    try:
        with get_db() as conn:
            agents_count = conn.execute("SELECT COUNT(*) FROM agents WHERE is_active=1").fetchone()[0]
            rooms_count = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    except Exception:
        agents_count = 0
        rooms_count = 0

    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        agents_count=agents_count,
        rooms_count=rooms_count,
        timestamp=datetime.utcnow().isoformat(),
        trace_id=trace_id
    )


# Include routers
app.include_router(agents.router, prefix="/api/v1/agents", tags=["Agents"])
app.include_router(rooms.router, prefix="/api/v1/rooms", tags=["Rooms"])
app.include_router(handshake.router, prefix="/api/v1", tags=["Handshake"])
app.include_router(sse.router, prefix="/api/v1", tags=["SSE"])
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["Tasks"])
app.include_router(credentials.router, prefix="/api/v1/credentials", tags=["Credentials"])
app.include_router(federation.router, prefix="/api/v1/federation", tags=["Federation"])
app.include_router(memory_commons.router)  # Memory Commons (includes own prefix)
app.include_router(key_lifecycle.router)  # Key Lifecycle (W9, includes own prefix)
app.include_router(governance.router)  # Governance (W11, includes own prefix)
app.include_router(routing.router, prefix="/api/v1", tags=["Routing"])  # Bandit routing
app.include_router(graphs.router, prefix="/api/v1/graphs", tags=["Graphs"])  # Graph orchestration
app.include_router(tokens.router, tags=["Tokens"])  # Token pool management
app.include_router(troupes.router)  # Troupe membership (includes own prefix)
app.include_router(backing.router)  # Trust-decay escrow with cross-backing (Round 3 Gap 1)
app.include_router(pools.router)  # Trajectory-weighted mutual aid pools (Round 3 Gap 3)
app.include_router(memory_v2.router)  # Memory Commons v2 - atomic claims (Phase 3)
app.include_router(escrow.router)  # Phase 4: Attack-resistant escrow with graduated reversibility
app.include_router(task_events.router)  # Phase 5: Doom-loop detection + checkpoint/resume + audit log
app.include_router(platform.router)  # Phase 6: The Standard — webhooks, observer mode, platform economics
app.include_router(docvault.router)  # DocVault webhook receiver (internal localhost endpoint)


@app.get("/.well-known/agent.json", tags=["A2A"])
async def agent_card():
    """A2A Protocol agent card (RFC 9110 /.well-known/)."""
    return {
        "name": "The Circus",
        "description": "Agent commons with AI-IQ passport-based identity and trust",
        "url": "https://circus.whatshubb.co.za",
        "version": settings.app_version,
        "capabilities": [
            "agent-registry",
            "trust-scoring",
            "memory-sharing",
            "p2p-handshake",
            "passport-verification",
            "room-based-collaboration",
            "sse-streaming",
            "ed25519-signing",
            "semantic-discovery",
            "a2a-task-lifecycle",
            "trust-portability",
            "federation-trqp",
            "audit-logging"
        ],
        "authentication": {
            "methods": ["bearer"],
            "bearer": {
                "scheme": "JWT",
                "description": "Register via POST /api/v1/agents/register to obtain token"
            }
        },
        "endpoints": {
            "register": "/api/v1/agents/register",
            "discover": "/api/v1/agents/discover",
            "discover_semantic": "/api/v1/agents/discover/semantic",
            "verify_signature": "/api/v1/agents/{agent_id}/verify",
            "rooms": "/api/v1/rooms",
            "handshake": "/api/v1/handshake",
            "sse_stream": "/api/v1/rooms/{room_id}/stream",
            "tasks": "/api/v1/tasks",
            "task_inbox": "/api/v1/tasks/inbox",
            "credentials": "/api/v1/credentials/trust-attestation",
            "federation_discover": "/api/v1/federation/discover",
            "audit_log": "/api/v1/agents/audit-log"
        },
        "protocols": ["A2A", "MCP"],
        "signing": {
            "algorithm": "Ed25519",
            "description": "All agent capability declarations are signed with Ed25519"
        },
        "trust_tiers": [
            {"tier": "Newcomer", "min_score": 0, "max_score": settings.trust_tier_newcomer_max},
            {"tier": "Established", "min_score": settings.trust_tier_newcomer_max, "max_score": settings.trust_tier_established_max},
            {"tier": "Trusted", "min_score": settings.trust_tier_established_max, "max_score": settings.trust_tier_trusted_max},
            {"tier": "Elder", "min_score": settings.trust_tier_trusted_max, "max_score": 100}
        ],
        "rate_limits": {
            "Newcomer": "100 req/hr",
            "Established": "500 req/hr",
            "Trusted": "2000 req/hr",
            "Elder": "10000 req/hr",
            "Anonymous": "30 req/hr"
        }
    }


@app.get("/", tags=["System"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "description": "Agent commons and registry with AI-IQ passport-based identity",
        "docs": "/docs",
        "health": "/health",
        "agent_card": "/.well-known/agent.json",
        "api_endpoints": {
            "agents": "/api/v1/agents",
            "rooms": "/api/v1/rooms",
            "handshake": "/api/v1/handshake",
            "sse": "/api/v1/rooms/{room_id}/stream"
        }
    }
