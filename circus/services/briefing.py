"""Theory of mind boot briefing service."""

from datetime import datetime
from typing import Optional

from circus.database import get_db


def generate_boot_briefing(room_id: Optional[str] = None) -> dict:
    """
    Generate a theory-of-mind briefing for an agent boot.

    Returns structured summary of who's good at what, so the booting
    agent knows who to delegate to.

    Args:
        room_id: Optional room ID to filter agents by room membership

    Returns:
        Dict with briefing text, agent summaries, and metadata
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Get agents (filtered by room if specified)
        if room_id:
            cursor.execute("""
                SELECT DISTINCT a.id, a.name
                FROM agents a
                JOIN room_members rm ON a.id = rm.agent_id
                WHERE rm.room_id = ? AND a.is_active = 1
                ORDER BY a.name
            """, (room_id,))
        else:
            cursor.execute("""
                SELECT id, name
                FROM agents
                WHERE is_active = 1
                ORDER BY name
            """)

        agents = cursor.fetchall()

        agent_summaries = []
        briefing_parts = []

        for agent_row in agents:
            agent_id = agent_row["id"]
            agent_name = agent_row["name"]

            # Get top 3 domains by competence score
            cursor.execute("""
                SELECT domain, score, observations
                FROM agent_competence
                WHERE agent_id = ? AND observations > 0
                ORDER BY score DESC, observations DESC
                LIMIT 3
            """, (agent_id,))

            competencies = cursor.fetchall()

            if competencies:
                top_domains = [
                    {
                        "domain": row["domain"],
                        "score": row["score"],
                        "observations": row["observations"]
                    }
                    for row in competencies
                ]

                agent_summaries.append({
                    "name": agent_name,
                    "agent_id": agent_id,
                    "top_domains": top_domains
                })

                # Format for briefing text
                domain_strs = [
                    f"{d['domain']} ({d['score']:.2f})"
                    for d in top_domains
                ]
                briefing_parts.append(
                    f"{agent_name} excels at {' and '.join(domain_strs)}"
                )

        # Generate briefing text
        if briefing_parts:
            briefing_text = f"Agent overview: {'. '.join(briefing_parts)}."
        else:
            briefing_text = "No agents with established competencies found."

        return {
            "briefing": briefing_text,
            "agents": agent_summaries,
            "generated_at": datetime.utcnow().isoformat()
        }


def get_agent_competence(agent_id: str) -> list[dict]:
    """
    Get all domain competence scores for an agent.

    Args:
        agent_id: Agent ID

    Returns:
        List of competence records
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT domain, score, observations, last_updated
            FROM agent_competence
            WHERE agent_id = ?
            ORDER BY score DESC
        """, (agent_id,))

        return [
            {
                "domain": row["domain"],
                "score": row["score"],
                "observations": row["observations"],
                "last_updated": row["last_updated"]
            }
            for row in cursor.fetchall()
        ]


def record_competence_observation(
    agent_id: str,
    domain: str,
    success: bool,
    weight: float = 1.0
) -> dict:
    """
    Record a competence observation and update score using weighted moving average.

    Args:
        agent_id: Agent ID
        domain: Domain name (e.g., "coding", "research")
        success: Whether the observation was successful
        weight: Weight of this observation (default 1.0)

    Returns:
        Updated competence record
    """
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Get current score and observations
        cursor.execute("""
            SELECT score, observations
            FROM agent_competence
            WHERE agent_id = ? AND domain = ?
        """, (agent_id, domain))

        row = cursor.fetchone()

        if row:
            # Update existing record
            current_score = row["score"]
            current_obs = row["observations"]

            # Weighted moving average
            observation_value = 1.0 if success else 0.0
            new_score = (current_score * current_obs + observation_value * weight) / (current_obs + weight)
            new_obs = current_obs + int(weight)

            cursor.execute("""
                UPDATE agent_competence
                SET score = ?, observations = ?, last_updated = ?
                WHERE agent_id = ? AND domain = ?
            """, (new_score, new_obs, now, agent_id, domain))
        else:
            # Create new record
            new_score = 1.0 if success else 0.0
            new_obs = int(weight)

            cursor.execute("""
                INSERT INTO agent_competence (agent_id, domain, score, observations, last_updated)
                VALUES (?, ?, ?, ?, ?)
            """, (agent_id, domain, new_score, new_obs, now))

        conn.commit()

        return {
            "domain": domain,
            "score": new_score,
            "observations": new_obs,
            "last_updated": now
        }


def calculate_average_competence(agent_id: str) -> float:
    """
    Calculate average competence score across all domains for an agent.

    Used for trust score bonus calculation.

    Args:
        agent_id: Agent ID

    Returns:
        Average competence score (0.0-1.0)
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT AVG(score) as avg_score, COUNT(*) as domain_count
            FROM agent_competence
            WHERE agent_id = ? AND observations > 0
        """, (agent_id,))

        row = cursor.fetchone()

        if row and row["domain_count"] > 0:
            return row["avg_score"]

        return 0.5  # Neutral score for agents with no observations
