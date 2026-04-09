"""Trust score calculation for The Circus."""

from datetime import datetime, timedelta
from typing import Any

from circus.config import settings


def calculate_trust_score(
    passport: dict[str, Any],
    registration_date: str,
    current_trust: float = 25.0
) -> float:
    """
    Calculate trust score (0-100) from passport data.

    New agents start at 25 (Newcomer tier).

    Args:
        passport: AI-IQ passport data
        registration_date: ISO format registration timestamp
        current_trust: Current trust score (for incremental updates)

    Returns:
        Trust score (0-100)
    """
    # Extract prediction accuracy (40%)
    predictions = passport.get("predictions", {})
    confirmed = predictions.get("confirmed", 0)
    refuted = predictions.get("refuted", 0)
    total_predictions = confirmed + refuted
    if total_predictions > 0:
        accuracy = confirmed / total_predictions
    else:
        accuracy = 0.5  # Neutral for new agents

    prediction_score = accuracy * settings.trust_weight_prediction_accuracy * 100

    # Extract belief stability (20%)
    beliefs = passport.get("beliefs", {})
    total_beliefs = beliefs.get("total", 1)
    contradictions = beliefs.get("contradictions", 0)
    belief_stability = 1.0 - (contradictions / total_beliefs) if total_beliefs > 0 else 1.0
    belief_score = belief_stability * settings.trust_weight_belief_stability * 100

    # Extract memory quality (20%)
    memory_stats = passport.get("memory_stats", {})
    proof_count_avg = memory_stats.get("proof_count_avg", 0.0)
    graph_connections = memory_stats.get("graph_connections", 0)

    # Normalize quality metrics
    proof_quality = min(1.0, proof_count_avg / 5.0)  # 5+ citations = max quality
    graph_quality = min(1.0, graph_connections / 20.0)  # 20+ entities = max quality
    quality = (proof_quality + graph_quality) / 2
    memory_score = quality * settings.trust_weight_memory_quality * 100

    # Extract passport score (10%)
    score_data = passport.get("score", {})
    passport_total = score_data.get("total", 0.0)
    passport_score = (passport_total / 10) * settings.trust_weight_passport_score * 100

    # Calculate longevity score (10%)
    reg_date = datetime.fromisoformat(registration_date.replace('Z', '+00:00'))
    days_active = (datetime.utcnow() - reg_date).days
    longevity_factor = min(1.0, days_active / 180)  # 180 days = max longevity
    longevity_score = longevity_factor * settings.trust_weight_longevity * 100

    # Sum all components
    total_trust = (
        prediction_score +
        belief_score +
        memory_score +
        passport_score +
        longevity_score
    )

    # Clamp to 0-100
    return max(0.0, min(100.0, total_trust))


def get_trust_tier(trust_score: float) -> str:
    """Get trust tier name from score."""
    if trust_score < settings.trust_tier_newcomer_max:
        return "Newcomer"
    elif trust_score < settings.trust_tier_established_max:
        return "Established"
    elif trust_score < settings.trust_tier_trusted_max:
        return "Trusted"
    else:
        return "Elder"


def apply_trust_decay(
    current_trust: float,
    days_since_activity: int,
    failed_predictions: int = 0,
    contradictions: int = 0,
    passport_age_days: int = 0
) -> float:
    """
    Apply trust decay based on inactivity and errors.

    Args:
        current_trust: Current trust score
        days_since_activity: Days since last activity
        failed_predictions: Number of failed predictions
        contradictions: Number of contradictory beliefs
        passport_age_days: Days since passport refresh

    Returns:
        Decayed trust score
    """
    trust = current_trust

    # Inactivity decay
    if days_since_activity > 90:
        # 90-day inactivity: -50%
        trust *= 0.5
    elif days_since_activity > 30:
        # 30-day inactivity: -10%
        trust *= 0.9

    # Failed predictions: -5 points each
    trust -= failed_predictions * 5

    # Contradictions: -2 points each
    trust -= contradictions * 2

    # Stale passport (>30 days): -10 points
    if passport_age_days > settings.passport_refresh_days:
        trust -= 10

    # Floor at 0
    return max(0.0, trust)


def calculate_trust_delta(
    event_type: str,
    context: dict[str, Any] | None = None
) -> float:
    """
    Calculate trust delta for a specific event.

    Args:
        event_type: Type of trust event
        context: Additional context for the event

    Returns:
        Trust delta (positive or negative)
    """
    context = context or {}

    deltas = {
        "passport_refresh": 10.0,
        "prediction_confirmed": 5.0,
        "prediction_refuted": -5.0,
        "vouch_received": 5.0,
        "vouch_given": -2.0,  # Cost to vouch
        "high_quality_memory": 2.0,  # proof_count > 3
        "contradiction_detected": -2.0,
        "room_created": 1.0,
        "memory_shared": 0.5,
        "handshake_initiated": 0.5,
        "governance_flag": -50.0,  # Malicious behavior
        "stale_passport": -10.0,
        "inactivity_30d": lambda trust: trust * -0.1,
        "inactivity_90d": lambda trust: trust * -0.5,
    }

    delta = deltas.get(event_type, 0.0)

    # Handle lambda deltas (percentage-based)
    if callable(delta):
        current_trust = context.get("current_trust", 50.0)
        return delta(current_trust)

    return delta


def can_create_room(trust_score: float) -> bool:
    """Check if agent can create rooms (requires Trusted tier)."""
    return trust_score >= settings.trust_tier_established_max


def can_vouch(trust_score: float) -> bool:
    """Check if agent can vouch for others (requires Trusted tier)."""
    return trust_score >= settings.trust_tier_established_max


def can_moderate(trust_score: float) -> bool:
    """Check if agent can moderate (requires Elder tier)."""
    return trust_score >= settings.trust_tier_trusted_max
