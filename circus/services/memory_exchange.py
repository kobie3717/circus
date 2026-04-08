"""Memory exchange and provenance verification."""

import hashlib
import hmac
import json
from typing import Any, Optional

from circus.config import settings


def sign_memory(memory: dict[str, Any], agent_secret: str) -> str:
    """Generate HMAC signature for memory."""
    memory_json = json.dumps(memory, sort_keys=True)
    signature = hmac.new(
        agent_secret.encode(),
        memory_json.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"sha256:{signature}"


def verify_memory_signature(
    memory: dict[str, Any],
    signature: str,
    agent_secret: str
) -> bool:
    """Verify HMAC signature of memory."""
    if not signature.startswith("sha256:"):
        return False

    expected_sig = sign_memory(memory, agent_secret)
    return hmac.compare_digest(signature, expected_sig)


def verify_memory_provenance(
    memory: dict[str, Any],
    sender_trust_score: float,
    sender_passport: Optional[dict[str, Any]] = None
) -> tuple[str, list[str]]:
    """
    Verify memory provenance.

    Returns:
        Tuple of (verification_level, issues_found)
        verification_level: 'green', 'yellow', or 'red'
        issues_found: List of verification issues
    """
    issues = []
    provenance = memory.get("provenance", {})

    # Check citations
    citations = provenance.get("citations", [])
    if not citations:
        issues.append("No citations provided")

    # Check derived-from chain
    derived_from = provenance.get("derived_from", [])
    if derived_from and sender_passport:
        # In full implementation, would check if derived_from IDs exist in sender's passport
        pass

    # Check graph entities
    entities = memory.get("graph_entities", [])
    if entities and sender_passport:
        # In full implementation, would verify entities match sender's known entities
        pass

    # Determine verification level
    if sender_trust_score >= 80 and len(issues) == 0:
        return "green", issues
    elif sender_trust_score >= 50 and len(issues) <= 1:
        return "yellow", issues
    else:
        return "red", issues


def format_memory_for_export(
    memory_id: str,
    agent_id: str,
    content: str,
    category: str,
    tags: Optional[list[str]] = None,
    provenance: Optional[dict[str, Any]] = None,
    project: Optional[str] = None
) -> dict[str, Any]:
    """Format memory for exchange/export."""
    from datetime import datetime

    return {
        "id": memory_id,
        "agent": agent_id,
        "category": category,
        "content": content,
        "project": project,
        "tags": tags or [],
        "provenance": provenance or {},
        "created_at": datetime.utcnow().isoformat(),
    }
