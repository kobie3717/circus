"""AI-IQ Passport generation from memory database."""

import hashlib
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def generate_passport(
    memory_db_path: str | Path,
    agent_name: str,
    agent_role: str
) -> dict[str, Any]:
    """
    Generate AI-IQ passport from a memory database.

    Args:
        memory_db_path: Path to AI-IQ memories.db
        agent_name: Agent's name
        agent_role: Agent's role (e.g., "engineering-bot")

    Returns:
        Passport dictionary with all identity components
    """
    db_path = Path(memory_db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Memory database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Extract basic memory stats
    cursor.execute("SELECT COUNT(*) as count FROM memories WHERE status = 'active'")
    memory_count = cursor.fetchone()['count']

    # Extract entities (if graph tables exist)
    entity_count = 0
    relationship_count = 0
    top_entities = []

    try:
        cursor.execute("SELECT COUNT(*) FROM entities")
        entity_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM relationships")
        relationship_count = cursor.fetchone()[0]

        # Get top 10 entities by relationship count
        cursor.execute("""
            SELECT e.name, e.type, COUNT(r.id) as rel_count
            FROM entities e
            LEFT JOIN relationships r ON e.name = r.from_entity OR e.name = r.to_entity
            GROUP BY e.name
            ORDER BY rel_count DESC
            LIMIT 10
        """)
        top_entities = [
            {"name": row[0], "type": row[1], "connections": row[2]}
            for row in cursor.fetchall()
        ]
    except sqlite3.OperationalError:
        # Graph tables don't exist
        pass

    # Extract beliefs
    belief_count = 0
    top_beliefs = []
    contradictions = 0
    avg_confidence = 0.5

    try:
        cursor.execute("""
            SELECT COUNT(*) FROM beliefs WHERE status = 'active'
        """)
        belief_count = cursor.fetchone()[0]

        cursor.execute("""
            SELECT statement, confidence
            FROM beliefs
            WHERE status = 'active'
            ORDER BY confidence DESC
            LIMIT 5
        """)
        top_beliefs = [
            {"statement": row[0], "confidence": row[1]}
            for row in cursor.fetchall()
        ]

        # Count contradictions
        cursor.execute("""
            SELECT COUNT(*) FROM beliefs
            WHERE status = 'active'
            AND id IN (
                SELECT belief_id FROM belief_contradictions
            )
        """)
        contradictions = cursor.fetchone()[0]

        # Average confidence
        cursor.execute("""
            SELECT AVG(confidence) FROM beliefs WHERE status = 'active'
        """)
        avg_confidence = cursor.fetchone()[0] or 0.5

    except sqlite3.OperationalError:
        pass

    # Extract predictions
    prediction_count = 0
    confirmed_count = 0
    refuted_count = 0
    prediction_accuracy = 0.0

    try:
        cursor.execute("""
            SELECT COUNT(*) FROM predictions
        """)
        prediction_count = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*) FROM predictions WHERE resolution = 'confirmed'
        """)
        confirmed_count = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*) FROM predictions WHERE resolution = 'refuted'
        """)
        refuted_count = cursor.fetchone()[0]

        total_resolved = confirmed_count + refuted_count
        if total_resolved > 0:
            prediction_accuracy = confirmed_count / total_resolved

    except sqlite3.OperationalError:
        pass

    # Extract memory quality metrics
    cursor.execute("""
        SELECT
            AVG(priority) as avg_priority,
            AVG(access_count) as avg_access,
            COUNT(*) as total
        FROM memories
        WHERE status = 'active'
    """)
    row = cursor.fetchone()
    avg_priority = row['avg_priority'] or 5.0
    avg_access = row['avg_access'] or 0.0

    # Calculate proof count (citations) - check if column exists first
    avg_citations = 0.0
    try:
        cursor.execute("""
            SELECT AVG(json_array_length(citations)) as avg_citations
            FROM memories
            WHERE status = 'active' AND citations IS NOT NULL
        """)
        row = cursor.fetchone()
        avg_citations = row['avg_citations'] or 0.0
    except sqlite3.OperationalError:
        # citations column doesn't exist, check for citationsJson
        try:
            cursor.execute("""
                SELECT COUNT(*) as count
                FROM memories
                WHERE status = 'active' AND citationsJson IS NOT NULL AND citationsJson != '[]'
            """)
            row = cursor.fetchone()
            # Estimate average based on presence
            if memory_count > 0:
                avg_citations = row['count'] / memory_count * 2.0
        except sqlite3.OperationalError:
            pass  # No citation tracking

    # Extract behavioral traits (if identity table exists)
    behavioral_traits = []
    try:
        cursor.execute("""
            SELECT trait, confidence, evidence_count
            FROM identity_traits
            WHERE confidence >= 0.5
            ORDER BY confidence DESC
            LIMIT 10
        """)
        behavioral_traits = [
            {
                "trait": row[0],
                "confidence": row[1],
                "evidence_count": row[2]
            }
            for row in cursor.fetchall()
        ]
    except sqlite3.OperationalError:
        pass

    conn.close()

    # Calculate passport score (0-10)
    # Based on: priority (30%), access (20%), citations (20%), graph (15%), recency (15%)

    priority_score = (avg_priority / 10) * 0.3 * 10  # Normalize to 0-10

    # Log scale for access count
    access_score = 0.0
    if avg_access > 0:
        access_score = min(1.0, math.log10(avg_access + 1) / 2) * 0.2 * 10

    # Log scale for citations
    citation_score = 0.0
    if avg_citations > 0:
        citation_score = min(1.0, math.log10(avg_citations + 1) / 1.5) * 0.2 * 10

    # Log scale for graph connections
    graph_score = 0.0
    if entity_count > 0:
        graph_score = min(1.0, math.log10(entity_count + 1) / 2) * 0.15 * 10

    # Recency score (assume fresh for new passport)
    recency_score = 0.15 * 10

    passport_score = (
        priority_score +
        access_score +
        citation_score +
        graph_score +
        recency_score
    )

    # Calculate belief stability
    belief_stability = 1.0
    if belief_count > 0:
        belief_stability = max(0.0, 1.0 - (contradictions / belief_count))

    # Build passport
    # NOTE: This passport must satisfy two consumers:
    #   1. POST /api/v1/agents/register — requires `identity.name` and `score`
    #   2. calculate_trust_score() — reads `predictions`, `beliefs`, `memory_stats`, `score`
    # We emit BOTH the legacy rich fields (agent_name, memory_stats, passport_score, ...)
    # AND the canonical register-API shape (identity, score) so either consumer works.
    passport = {
        # Canonical register-API shape (required by POST /agents/register)
        "identity": {
            "name": agent_name,
            "role": agent_role,
        },
        # Legacy/rich fields (required by calculate_trust_score and clients that
        # want the full memory dossier)
        "agent_name": agent_name,
        "agent_role": agent_role,
        "generated_at": datetime.utcnow().isoformat(),
        "memory_stats": {
            "memory_count": memory_count,
            "entity_count": entity_count,
            "relationship_count": relationship_count,
            "belief_count": belief_count,
            "prediction_count": prediction_count,
            # Trust score reads these from memory_stats directly (see trust.py:47-49)
            "proof_count_avg": avg_citations,
            "graph_connections": entity_count,
        },
        "graph": {
            "top_entities": top_entities,
            "entity_count": entity_count,
            "relationship_count": relationship_count
        },
        "beliefs": {
            "total": belief_count,
            "top_beliefs": top_beliefs,
            "contradictions": contradictions,
            "average_confidence": avg_confidence,
            "stability": belief_stability
        },
        "predictions": {
            "total": prediction_count,
            "confirmed": confirmed_count,
            "refuted": refuted_count,
            "accuracy": prediction_accuracy
        },
        "memory_quality": {
            "average_priority": avg_priority,
            "average_access_count": avg_access,
            "average_citations": avg_citations,
            "proof_count_avg": avg_citations
        },
        "behavioral_traits": behavioral_traits,
        # Canonical score (required by POST /agents/register, read by trust.py)
        # Kept as a dict with {"total": N} so both the API (which requires the key
        # to exist) and trust.py (which handles both dict and float) are happy.
        "score": {
            "total": round(passport_score, 2),
        },
        # Full breakdown preserved under passport_score for backwards compatibility
        # with any clients that already read this field.
        "passport_score": {
            "total": round(passport_score, 2),
            "breakdown": {
                "priority": round(priority_score, 2),
                "access": round(access_score, 2),
                "citations": round(citation_score, 2),
                "graph_connections": round(graph_score, 2),
                "recency": round(recency_score, 2)
            }
        }
    }

    # Generate fingerprint (hash of memory database)
    with open(db_path, 'rb') as f:
        db_hash = hashlib.sha256(f.read()).hexdigest()
    passport["fingerprint"] = db_hash[:16]  # First 16 chars

    # Add domain competence section (if we have an agent_id context)
    # This would be added when generating passport for a specific agent
    passport["domain_competence"] = {
        "note": "Domain competence scores tracked separately in The Circus registry"
    }

    # Add theory of mind section placeholder
    passport["theory_of_mind"] = {
        "note": "Boot briefings available via GET /api/v1/agents/briefing/boot"
    }

    return passport


def calculate_passport_hash(passport: dict[str, Any]) -> str:
    """Calculate hash of passport data for storage."""
    passport_str = json.dumps(passport, sort_keys=True)
    return hashlib.sha256(passport_str.encode()).hexdigest()
