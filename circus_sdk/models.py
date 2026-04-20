"""Data models for Circus SDK."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Preference:
    """Active preference entry."""
    owner_id: str
    field_name: str
    value: str
    effective_confidence: float
    updated_at: str
    source_memory_id: Optional[str] = None
    conflict_count: Optional[int] = 0


@dataclass
class OwnerKey:
    """Owner public key entry."""
    owner_id: str
    public_key: str
    created_at: str
    is_active: bool = True
    description: Optional[str] = None


@dataclass
class AuditEvent:
    """Governance audit event."""
    id: str
    event_type: str
    happened_at: str
    actor: Optional[str] = None
    owner_id: Optional[str] = None
    detail: Optional[dict] = None
