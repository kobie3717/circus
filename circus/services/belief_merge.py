"""Belief conflict detection and auto-resolution for Memory Commons.

Implements domain authority-based conflict resolution as specified in
Circus Memory Commons spec §6.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from circus.services.embeddings import embed_text

logger = logging.getLogger(__name__)


# Conflict detection threshold
SIMILARITY_THRESHOLD = 0.8  # 80% semantic similarity = potential conflict

# Auto-resolution threshold
AUTO_RESOLVE_RATIO = 1.5  # Winner score must be 1.5x loser to auto-resolve

# Negation patterns (English, Afrikaans, Portuguese)
# Context-aware patterns to avoid false positives like "no idea", "not sure"
NEGATION_PATTERNS = [
    r'\bnot\s+(?!sure\b|certain\b|clear\b|yet\b)',  # "not" but exclude "not sure", "not certain", etc.
    r'\bnever\b',  # Unambiguous negation
    r"\bisn't\b",  # Contraction negations
    r"\baren't\b",
    r"\bwasn't\b",
    r"\bweren't\b",
    r"\bno\s+(longer|more|way)\b",  # Context-aware "no" patterns
    r'\bnie\b',  # Afrikaans negation (unambiguous)
    r'\bnooit\b',  # Afrikaans "never"
    r'\bnão\b',  # Portuguese negation (unambiguous)
    r'\bnunca\b',  # Portuguese "never"
]


class ConflictInfo:
    """Information about a detected conflict."""

    def __init__(
        self,
        memory_a_id: str,
        memory_b_id: str,
        conflict_type: str,
        similarity: float,
        domain: Optional[str] = None,
    ):
        self.memory_a_id = memory_a_id
        self.memory_b_id = memory_b_id
        self.conflict_type = conflict_type
        self.similarity = similarity
        self.domain = domain


class ResolutionResult:
    """Result of conflict resolution."""

    def __init__(
        self,
        winner_id: str,
        loser_id: str,
        strategy: str,
        auto_resolved: bool,
        reason: str,
        authority_score_a: float,
        authority_score_b: float,
    ):
        self.winner_id = winner_id
        self.loser_id = loser_id
        self.strategy = strategy
        self.auto_resolved = auto_resolved
        self.reason = reason
        self.authority_score_a = authority_score_a
        self.authority_score_b = authority_score_b


class ConflictResolution(BaseModel):
    """Conflict resolution result."""
    memory_id_a: str
    memory_id_b: str
    conflict_type: str
    winner_id: str
    strategy: str
    auto_resolved: bool
    reason: str
    authority_score_a: float
    authority_score_b: float


async def detect_conflict(
    new_memory: dict,
    existing_memories: list[dict],
) -> Optional[ConflictInfo]:
    """
    Detect conflicts between new memory and existing memories.

    Uses semantic similarity + negation pattern detection.

    Args:
        new_memory: Dict with keys: id, content, category, from_agent_id
        existing_memories: List of existing memory dicts

    Returns:
        ConflictInfo if conflict detected, None otherwise
    """
    if not existing_memories:
        return None

    # Embed new memory content
    new_embedding = await embed_text(new_memory["content"])
    new_content_lower = new_memory["content"].lower()
    new_has_negation = _has_negation(new_content_lower)

    for existing in existing_memories:
        # Check semantic similarity first
        existing_embedding = await embed_text(existing["content"])
        similarity = _cosine_similarity(new_embedding, existing_embedding)

        if similarity < SIMILARITY_THRESHOLD:
            continue  # Not similar enough

        # Check for negation in both memories
        existing_content_lower = existing["content"].lower()
        existing_has_negation = _has_negation(existing_content_lower)

        # Same author + negation difference = self-contradiction (critical)
        if existing["from_agent_id"] == new_memory["from_agent_id"]:
            if new_has_negation != existing_has_negation:
                # Self-contradiction: same author contradicting themselves
                return ConflictInfo(
                    memory_a_id=existing["id"],
                    memory_b_id=new_memory["id"],
                    conflict_type="self-contradiction",
                    similarity=similarity,
                    domain=new_memory["domain"],
                )
            else:
                # Same author, high similarity, no negation = update
                return ConflictInfo(
                    memory_a_id=existing["id"],
                    memory_b_id=new_memory["id"],
                    conflict_type="update",
                    similarity=similarity,
                    domain=new_memory["domain"],
                )

        # Different authors with negation difference = contradiction
        if new_has_negation != existing_has_negation:
            return ConflictInfo(
                memory_a_id=existing["id"],
                memory_b_id=new_memory["id"],
                conflict_type="contradiction",
                similarity=similarity,
                domain=new_memory["domain"],
            )

        # High similarity, no negation difference = refinement
        if new_memory["from_agent_id"] != existing["from_agent_id"]:
            return ConflictInfo(
                memory_a_id=existing["id"],
                memory_b_id=new_memory["id"],
                conflict_type="refinement",
                similarity=similarity,
                domain=new_memory["domain"],
            )

    return None


async def apply_belief_merge_pipeline(
    conn: sqlite3.Connection,
    new_memory: dict,  # Keys: id, from_agent_id, content, category, domain, confidence, shared_at
    agent_id: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[ConflictResolution]:
    """Run conflict detection + resolution + merge for newly-inserted memory.

    PRE: new_memory MUST exist in shared_memories (caller INSERTs before calling).
    POST: If conflict detected, belief_conflicts row written + merge applied if auto_resolved.
    COMMIT: Function commits internally (matches current line 376 behavior).

    Returns ConflictResolution if conflict found, else None.
    Returns None if conflict_detection_enabled=False.
    """
    from circus.config import settings

    if now is None:
        now = datetime.utcnow()

    conflict_result = None
    if settings.conflict_detection_enabled:
        cursor = conn.cursor()

        # Fetch recent memories in same category for conflict detection
        cursor.execute("""
            SELECT id, from_agent_id, content, category, domain, confidence, shared_at
            FROM shared_memories
            WHERE category = ?
              AND id != ?
              AND privacy_tier IN ('public', 'team')
            ORDER BY shared_at DESC
            LIMIT 50
        """, (new_memory["category"], new_memory["id"]))

        existing_memories = [
            {
                "id": row[0],
                "from_agent_id": row[1],
                "content": row[2],
                "category": row[3],
                "domain": row[4],
                "confidence": row[5],
                "shared_at": row[6],
            }
            for row in cursor.fetchall()
        ]

        conflict_info = await detect_conflict(new_memory, existing_memories)

        if conflict_info:
            # Fetch full memory data for resolution
            cursor.execute("""
                SELECT id, from_agent_id, content, category, domain, confidence, shared_at
                FROM shared_memories
                WHERE id = ?
            """, (conflict_info.memory_a_id,))
            row = cursor.fetchone()
            memory_a = {
                "id": row[0],
                "from_agent_id": row[1],
                "content": row[2],
                "category": row[3],
                "domain": row[4],
                "confidence": row[5],
                "shared_at": row[6],
            }

            # Resolve conflict (domain is now required)
            resolution = resolve_conflict(
                conn,
                memory_a,
                new_memory,
                conflict_info.domain
            )

            # Record conflict in belief_conflicts table
            cursor.execute("""
                INSERT INTO belief_conflicts (
                    memory_id_a, memory_id_b, conflict_type, detected_at,
                    resolution, resolved_at, resolved_by_agent_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                conflict_info.memory_a_id,
                conflict_info.memory_b_id,
                conflict_info.conflict_type,
                now.isoformat(),
                resolution.strategy if resolution.auto_resolved else None,
                now.isoformat() if resolution.auto_resolved else None,
                agent_id if resolution.auto_resolved else None,
            ))
            conn.commit()

            # Apply merge if auto-resolved
            if resolution.auto_resolved:
                apply_merge(
                    conn,
                    resolution.winner_id,
                    resolution.loser_id,
                    resolution.strategy
                )

            # Build conflict result for response
            conflict_result = ConflictResolution(
                memory_id_a=conflict_info.memory_a_id,
                memory_id_b=conflict_info.memory_b_id,
                conflict_type=conflict_info.conflict_type,
                winner_id=resolution.winner_id,
                strategy=resolution.strategy,
                auto_resolved=resolution.auto_resolved,
                reason=resolution.reason,
                authority_score_a=resolution.authority_score_a,
                authority_score_b=resolution.authority_score_b,
            )

    return conflict_result


def resolve_conflict(
    conn: sqlite3.Connection,
    memory_a: dict,
    memory_b: dict,
    domain: str,
) -> ResolutionResult:
    """
    Resolve conflict based on domain authority.

    Implements authority scoring from spec §6.3:
    Multiplicative 3-factor formula: stewardship × effective_confidence × recency_weight
    Trust is already baked into effective_confidence via provenance decay.

    If no registered stewards exist for the domain, falls back to neutral resolution
    using only: effective_confidence × recency_weight

    Auto-resolves if winner_score / loser_score >= 1.5

    Args:
        conn: Database connection
        memory_a: First memory dict
        memory_b: Second memory dict
        domain: Domain for stewardship lookup

    Returns:
        ResolutionResult
    """
    cursor = conn.cursor()

    # Get authors
    author_a = memory_a["from_agent_id"]
    author_b = memory_b["from_agent_id"]

    logger.debug("Resolving conflict on domain '%s'", domain)

    # Get stewardship levels for registered stewards in this domain
    cursor.execute(
        """
        SELECT agent_id, stewardship_level
        FROM agent_domains
        WHERE domain = ?
        ORDER BY stewardship_level DESC
    """,
        (domain,),
    )
    stewards = {row[0]: row[1] for row in cursor.fetchall()}

    # Fix A: Check if domain has registered stewards
    has_stewards = len(stewards) > 0

    steward_a = stewards.get(author_a, 0.0)
    steward_b = stewards.get(author_b, 0.0)

    # Get trust scores (used for effective_confidence calculation in provenance decay)
    cursor.execute("SELECT id, trust_score FROM agents WHERE id IN (?, ?)", (author_a, author_b))
    trust_scores = {row[0]: row[1] for row in cursor.fetchall()}

    trust_a = trust_scores.get(author_a, 50.0)
    trust_b = trust_scores.get(author_b, 50.0)

    # Calculate recency scores (0-1, newer = higher)
    recency_a = _recency_score(memory_a["shared_at"])
    recency_b = _recency_score(memory_b["shared_at"])

    # Get effective_confidence (post-decay) from provenance
    # This already includes trust via provenance.decay_confidence()
    # Fall back to base confidence if effective not available
    effective_confidence_a = memory_a.get("effective_confidence", memory_a.get("confidence", 1.0))
    effective_confidence_b = memory_b.get("effective_confidence", memory_b.get("confidence", 1.0))

    # Fix D: Multiplicative 3-factor formula (spec §6.1)
    # If no registered stewards, use only effective_confidence × recency
    if has_stewards:
        # Use full formula: stewardship × effective_confidence × recency
        score_a = steward_a * effective_confidence_a * recency_a
        score_b = steward_b * effective_confidence_b * recency_b
    else:
        # Neutral resolution: no stewardship factor
        score_a = effective_confidence_a * recency_a
        score_b = effective_confidence_b * recency_b
        logger.info(
            "No registered stewards for domain '%s', using neutral resolution (confidence × recency)",
            domain
        )

    # Determine winner
    if score_a > score_b:
        winner_id = memory_a["id"]
        loser_id = memory_b["id"]
        winner_score = score_a
        loser_score = score_b
    else:
        winner_id = memory_b["id"]
        loser_id = memory_a["id"]
        winner_score = score_b
        loser_score = score_a

    # Check if auto-resolvable
    auto_resolved = False
    if loser_score == 0 and winner_score > 0:
        # One side has no authority, clear win
        auto_resolved = True
    elif loser_score > 0 and (winner_score / loser_score) >= AUTO_RESOLVE_RATIO:
        # Winner score is 1.5x+ loser score
        auto_resolved = True

    # Determine strategy
    strategy = "supersede"  # Default strategy
    if author_a == author_b:
        strategy = "merge"  # Same author updates

    reason = (
        f"Authority: {score_a:.3f} vs {score_b:.3f} | "
        f"Stewardship: {steward_a:.2f} vs {steward_b:.2f} | "
        f"Effective confidence: {effective_confidence_a:.2f} vs {effective_confidence_b:.2f} | "
        f"Recency: {recency_a:.2f} vs {recency_b:.2f}"
    )

    return ResolutionResult(
        winner_id=winner_id,
        loser_id=loser_id,
        strategy=strategy,
        auto_resolved=auto_resolved,
        reason=reason,
        authority_score_a=score_a,
        authority_score_b=score_b,
    )


def apply_merge(
    conn: sqlite3.Connection,
    winner_id: str,
    loser_id: str,
    strategy: str,
) -> None:
    """
    Apply merge strategy to resolve conflict.

    Strategies:
    - supersede: Mark loser as superseded, keep winner
    - merge: Update winner's provenance to include loser
    - keep-both: No action (for manual resolution)

    Args:
        conn: Database connection
        winner_id: Winning memory ID
        loser_id: Losing memory ID
        strategy: Merge strategy
    """
    cursor = conn.cursor()

    if strategy == "supersede":
        # Mark loser's provenance to indicate supersession
        cursor.execute(
            """
            UPDATE shared_memories
            SET provenance = json_set(provenance, '$.superseded_by', ?)
            WHERE id = ?
        """,
            (winner_id, loser_id),
        )

    elif strategy == "merge":
        # Update winner's provenance to reference loser
        cursor.execute(
            """
            SELECT provenance FROM shared_memories WHERE id = ?
        """,
            (winner_id,),
        )
        row = cursor.fetchone()
        if row:
            import json

            provenance = json.loads(row[0]) if row[0] else {}
            derived_from = provenance.get("derived_from", [])
            if loser_id not in derived_from:
                derived_from.append(loser_id)
            provenance["derived_from"] = derived_from

            cursor.execute(
                """
                UPDATE shared_memories
                SET provenance = ?
                WHERE id = ?
            """,
                (json.dumps(provenance), winner_id),
            )

    # Note: "keep-both" does nothing, for manual review
    conn.commit()


# Helper functions


def _has_negation(text: str) -> bool:
    """Check if text contains negation patterns."""
    for pattern in NEGATION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _cosine_similarity(embedding_a: list[float], embedding_b: list[float]) -> float:
    """Calculate cosine similarity between two embeddings."""
    import math

    if len(embedding_a) != len(embedding_b):
        return 0.0

    dot_product = sum(a * b for a, b in zip(embedding_a, embedding_b))
    magnitude_a = math.sqrt(sum(a * a for a in embedding_a))
    magnitude_b = math.sqrt(sum(b * b for b in embedding_b))

    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0

    return dot_product / (magnitude_a * magnitude_b)


def _recency_score(timestamp_str: str) -> float:
    """
    Calculate recency score (0-1).

    Recent memories get higher scores. Decay over 180 days.
    """
    try:
        timestamp = datetime.fromisoformat(timestamp_str)
        age_seconds = (datetime.utcnow() - timestamp).total_seconds()
        age_days = age_seconds / 86400.0

        # Exponential decay with 180-day half-life
        import math

        score = math.exp(-age_days / 180.0 * math.log(2))
        return max(0.0, min(1.0, score))
    except (ValueError, TypeError):
        return 0.5  # Default to neutral if timestamp invalid
