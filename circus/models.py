"""Pydantic models for API requests and responses."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# Import ConflictResolution from belief_merge service (domain concept, not transport DTO)
from circus.services.belief_merge import ConflictResolution


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


# Competence models

class DomainCompetence(BaseModel):
    """Domain-specific competence score."""
    domain: str
    score: float = Field(..., ge=0.0, le=1.0)
    observations: int = Field(..., ge=0)


class CompetenceObservationRequest(BaseModel):
    """Record a competence observation."""
    domain: str = Field(..., min_length=1)
    success: bool
    weight: float = Field(1.0, ge=0.1, le=5.0)


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
    public_key: Optional[str] = None
    signed_card: Optional[str] = None
    competence: Optional[list[DomainCompetence]] = None


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
    trace_id: Optional[str] = None


# Task lifecycle models (Phase 3)

class TaskState(str, Enum):
    """Task state machine."""
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class TaskSubmitRequest(BaseModel):
    """Submit task to another agent."""
    to_agent_id: str = Field(..., min_length=1)
    task_type: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(...)
    deadline: Optional[str] = None
    output_schema: Optional[dict[str, Any]] = None


class TaskUpdateRequest(BaseModel):
    """Update task state."""
    state: TaskState
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    notes: Optional[str] = None


class TaskResponse(BaseModel):
    """Task response."""
    task_id: str
    from_agent_id: str
    to_agent_id: str
    task_type: str
    payload: dict[str, Any]
    state: TaskState
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str
    deadline: Optional[str] = None
    output_schema: Optional[dict[str, Any]] = None


class TaskStateTransition(BaseModel):
    """Task state transition record."""
    from_state: Optional[TaskState]
    to_state: TaskState
    notes: Optional[str]
    created_at: str


# Briefing models

class AgentCompetenceSummary(BaseModel):
    """Summary of agent's top competencies."""
    name: str
    agent_id: str
    top_domains: list[DomainCompetence]


class BootBriefingResponse(BaseModel):
    """Theory of mind boot briefing."""
    briefing: str
    agents: list[AgentCompetenceSummary]
    generated_at: str


# Memory Commons models

class GoalCreate(BaseModel):
    """Request to create a goal subscription."""
    goal_description: str = Field(..., min_length=5, max_length=500)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    expires_in_hours: Optional[int] = Field(default=24, ge=1, le=168)


class GoalResponse(BaseModel):
    """Response for goal creation."""
    goal_id: str
    stream_url: str


class GoalInfo(BaseModel):
    """Goal subscription information."""
    id: str
    agent_id: str
    goal_description: str
    min_confidence: float
    created_at: str
    expires_at: Optional[str]
    is_active: bool


class OwnerBinding(BaseModel):
    """Owner signature binding for preference memories (Week 5).

    Cryptographic proof that a publishing agent is authorized to act on
    behalf of the claimed owner. Prevents agent spoofing attacks where
    malicious agents publish preferences claiming owner_id without authority.

    All four fields are included in the signed payload to prevent replay:
    - agent_id: binds signature to specific agent (audit trail)
    - memory_id: binds signature to specific memory (prevents cross-memory replay)
    - timestamp: binds signature to time window (prevents indefinite replay)
    - signature: Ed25519 signature over canonical JSON of above 3 + owner_id

    Verification ensures:
    1. Owner's public key (from owner_keys table) can verify the signature
    2. memory_id matches the actual memory being admitted
    3. timestamp is within ±5min of shared_at (bidirectional window)

    Note: Fields are Optional to allow Pydantic parsing, but publish-side validation
    enforces their presence with precise 400 error messages (W5 5.3 requirement).
    """
    agent_id: Optional[str] = Field(default=None)
    memory_id: Optional[str] = Field(default=None)
    timestamp: Optional[str] = Field(default=None)
    signature: Optional[str] = Field(default=None)


class ProvenanceInfo(BaseModel):
    """Provenance metadata for memory."""
    derived_from: Optional[list[str]] = Field(default=None)
    citations: Optional[list[str]] = Field(default=None)
    reasoning: Optional[str] = Field(default=None)
    owner_id: Optional[str] = Field(default=None)  # Week 4: owner identifier for preference memories
    owner_binding: Optional[OwnerBinding] = Field(default=None)  # Week 5: cryptographic owner proof
    supersedes_memory_id: Optional[str] = Field(default=None)  # For corrections: ID of stale memory being replaced


class PreferenceField(BaseModel):
    """Preference field within a behavior-delta memory (Week 4).

    Represents a mutable user preference that affects bot behavior at runtime.
    Field names must start with "user." and be in the allowlist.
    """
    field: str = Field(..., pattern=r"^user\.")
    value: str = Field(..., min_length=1, max_length=100)


class MemoryPublish(BaseModel):
    """Request to publish a memory."""
    content: str = Field(..., min_length=10, max_length=5000)
    category: str = Field(..., min_length=2, max_length=50)
    domain: str = Field(..., min_length=1, max_length=50)
    tags: Optional[list[str]] = Field(default=None)
    privacy_tier: str = Field(default="team", pattern="^(private|team|public)$")
    provenance: Optional[ProvenanceInfo] = Field(default=None)
    confidence: float = Field(default=0.9, ge=0.1, le=1.0)
    preference: Optional[PreferenceField] = Field(default=None)  # Week 4: behavior-delta preference


class PublishResponse(BaseModel):
    """Response for memory publish."""
    memory_id: str
    routed_to: list[str]
    match_scores: list[float]


class AgentInfo(BaseModel):
    """Minimal agent info for events."""
    id: str
    name: str
    trust_score: float


class ProvenanceEvent(BaseModel):
    """Provenance data in SSE events."""
    hop_count: int
    original_author: str
    confidence: float
    age_days: int
    effective_confidence: float


class MemoryEvent(BaseModel):
    """Memory SSE event."""
    type: str = "memory"
    memory_id: str
    content: str
    category: str
    tags: Optional[list[str]]
    from_agent: AgentInfo
    provenance: ProvenanceEvent
    match_score: float
    goal_id: str
    timestamp: str


class ConnectedEvent(BaseModel):
    """Connected SSE event."""
    type: str = "connected"
    timestamp: str
    goal_id: Optional[str] = None


class GoalExpiredEvent(BaseModel):
    """Goal expired SSE event."""
    type: str = "goal_expired"
    goal_id: str
    reason: str


class HeartbeatEvent(BaseModel):
    """Heartbeat SSE event."""
    type: str = "heartbeat"
    timestamp: str


class SharedMemoryResponse(BaseModel):
    """Shared memory with provenance."""
    id: str
    content: str
    category: str
    tags: Optional[list[str]]
    from_agent: AgentInfo
    privacy_tier: str
    hop_count: int
    original_author: Optional[str]
    confidence: float
    effective_confidence: float
    shared_at: str


# Domain stewardship models (Week 2)


class DomainClaim(BaseModel):
    """Request to claim domain stewardship."""
    domain: str = Field(..., min_length=2, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=500)


class DomainClaimResponse(BaseModel):
    """Response for domain claim."""
    domain: str
    stewardship_level: float
    status: str


class DomainSteward(BaseModel):
    """Domain steward info."""
    agent_id: str
    agent_name: str
    stewardship_level: float
    claimed_at: str


class PublishResponseWithConflict(BaseModel):
    """Response for memory publish with conflict resolution."""
    memory_id: str
    routed_to: list[str]
    match_scores: list[float]
    conflict_resolution: Optional[ConflictResolution] = None
    preference_activated: Optional[bool] = None  # Week 4: True if preference was admitted to active_preferences
    decision_trace: Optional[dict[str, Any]] = None  # Week 6: Gate-by-gate preference admission trace
