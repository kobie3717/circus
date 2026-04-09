"""FastAPI application setup for The Circus."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from circus.config import settings
from circus.database import init_database, seed_default_rooms, get_db
from circus.models import HealthResponse
from circus.routes import agents, rooms, handshake
from circus.trust import apply_trust_decay, get_trust_tier


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    init_database()
    seed_default_rooms()

    # Start background task
    task = asyncio.create_task(trust_decay_task())

    yield

    # Shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Agent commons and registry with AI-IQ passport-based identity",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# Health check endpoint
@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Health check endpoint."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM agents WHERE is_active = 1")
        agents_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM rooms")
        rooms_count = cursor.fetchone()[0]

    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        agents_count=agents_count,
        rooms_count=rooms_count,
        timestamp=datetime.utcnow().isoformat()
    )


# Include routers
app.include_router(agents.router, prefix="/api/v1/agents", tags=["Agents"])
app.include_router(rooms.router, prefix="/api/v1/rooms", tags=["Rooms"])
app.include_router(handshake.router, prefix="/api/v1", tags=["Handshake"])


@app.get("/", tags=["System"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "description": "Agent commons and registry with AI-IQ passport-based identity",
        "docs": "/docs",
        "health": "/health",
        "api_endpoints": {
            "agents": "/api/v1/agents",
            "rooms": "/api/v1/rooms",
            "handshake": "/api/v1/handshake"
        }
    }
