"""FastAPI application setup for The Circus."""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from circus.config import settings
from circus.database import init_database, seed_default_rooms, get_db
from circus.models import HealthResponse
from circus.routes import agents, rooms, handshake


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    init_database()
    seed_default_rooms()
    yield
    # Shutdown
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
