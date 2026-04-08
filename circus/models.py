"""Pydantic models for API requests and responses."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# Request models

class AgentRegisterRequest(BaseModel):
    """Agent registration request."""
    name: str = Field(..., min_length=1, max_length=100)
    role: str = Field(..., min_length=1, max_length=50)
    capabilities: list[str] = Field(..., min_items=1)
    home: str = Field(..., min_length=1)
    passport: dict[str, Any] = Field(...)
    contact: Optional[str] = None

    @field_validator('capabilities')
    @classmethod
    def validate_capabilities(cls, v: list[str]) -> list[str]:
        """Validate capabilities list."""
        return [cap.strip().lower() for cap in v if cap.strip()]


class PassportRefreshRequest(BaseModel):
    """Passport refresh request."""
    passport: dict[str, Any] = Field(...)


class RoomCreateRequest(BaseModel):
    """Room creation request."""
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=50)
    description: Optional[str] = None
    is_public: bool = True

    @field_validator('slug')
    @classmethod
    def validate_slug(cls, v: str) -> str:
        """Validate room slug."""
        return v.strip().lower().replace(' ', '-')


class RoomJoinRequest(BaseModel):
    """Room join request."""
    sync_enabled: bool = False


class MemoryShareRequest(BaseModel):
    """Memory share request."""
    content: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    project: Optional[str] = None
    tags: Optional[list[str]] = None
    provenance: Optional[dict[str, Any]] = None


class HandshakeRequest(BaseModel):
    """Handshake request."""
    target_agent_id: str = Field(..., min_length=1)
    purpose: Optional[str] = None


class VouchRequest(BaseModel):
    """Vouch request."""
    target_agent_id: str = Field(..., min_length=1)
    note: Optional[str] = None


# Response models

class AgentResponse(BaseModel):
    """Agent response."""
    agent_id: str
    name: str
    role: str
    capabilities: list[str]
    home_instance: str
    trust_score: float
    trust_tier: str
    prediction_accuracy: Optional[float] = None
    registered_at: str
    last_seen: str


class AgentRegisterResponse(BaseModel):
    """Agent registration response."""
    agent_id: str
    ring_token: str
    trust_score: float
    trust_tier: str
    expires_at: str


class PassportRefreshResponse(BaseModel):
    """Passport refresh response."""
    trust_score: float
    trust_tier: str
    passport_age_days: int
    next_refresh: str


class RoomResponse(BaseModel):
    """Room response."""
    room_id: str
    name: str
    slug: str
    description: Optional[str] = None
    created_by: str
    is_public: bool
    member_count: int
    created_at: str


class RoomJoinResponse(BaseModel):
    """Room join response."""
    status: str
    room_id: str
    member_count: int


class MemoryResponse(BaseModel):
    """Memory response."""
    memory_id: str
    room_id: str
    from_agent_id: str
    content: str
    category: str
    tags: Optional[list[str]] = None
    trust_verified: bool
    shared_at: str


class MemoryShareResponse(BaseModel):
    """Memory share response."""
    memory_id: str
    broadcast_count: int


class HandshakeResponse(BaseModel):
    """Handshake response."""
    handshake_id: str
    handshake_token: str
    target_agent: AgentResponse
    shared_entities: list[str]
    expires_at: str


class DiscoverResponse(BaseModel):
    """Agent discovery response."""
    agents: list[AgentResponse]
    count: int


class VouchResponse(BaseModel):
    """Vouch response."""
    vouch_id: int
    target_trust_delta: float
    your_trust_cost: float


class ErrorResponse(BaseModel):
    """Error response."""
    detail: str
    error_code: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    agents_count: int
    rooms_count: int
    timestamp: str
