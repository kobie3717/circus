"""Trust score calculation and management."""

from datetime import datetime

from circus.config import settings


def calculate_trust_score(
    prediction_accuracy: float,
    belief_stability: float,
    memory_quality: float,
    passport_score: float,
    days_active: int
) -> float:
    """
    Calculate trust score (0-100) from passport metrics.

    Args:
        prediction_accuracy: 0-1 (percentage of confirmed predictions)
        belief_stability: 0-1 (1 - contradiction rate)
        memory_quality: 0-1 (normalized proof count + graph connections)
        passport_score: 0-10 (AI-IQ composite score)
        days_active: Number of days since registration

    Returns:
        Trust score (0-100)
    """
    # Prediction accuracy (40%)
    prediction_score = (
        prediction_accuracy * settings.trust_weight_prediction_accuracy * 100
    )

    # Belief stability (20%)
    belief_score = (
        belief_stability * settings.trust_weight_belief_stability * 100
    )

    # Memory quality (20%)
    memory_score = (
        memory_quality * settings.trust_weight_memory_quality * 100
    )

    # Passport score (10%)
    passport_score_norm = (
        (passport_score / 10.0) * settings.trust_weight_passport_score * 100
    )

    # Longevity (10%)
    longevity_norm = min(1.0, days_active / 365.0)
    longevity_score = (
        longevity_norm * settings.trust_weight_longevity * 100
    )

    total_score = sum([
        prediction_score,
        belief_score,
        memory_score,
        passport_score_norm,
        longevity_score
    ])

    # Clamp to 0-100
    return max(0.0, min(100.0, total_score))


def get_trust_tier(trust_score: float) -> str:
    """Get trust tier from trust score."""
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
    failed_predictions: int,
    contradictions: int,
    passport_age_days: int
) -> tuple[float, list[dict]]:
    """
    Apply trust decay based on inactivity and errors.

    Returns:
        Tuple of (new_trust_score, list of trust events)
    """
    if not settings.trust_decay_enabled:
        return current_trust, []

    new_trust = current_trust
    events = []

    # Inactivity decay
    if days_since_activity > 90:
        delta = -(current_trust * 0.5)
        new_trust += delta
        events.append({
            "event_type": "inactivity_decay_90d",
            "delta": delta,
            "reason": f"No activity for {days_since_activity} days"
        })
    elif days_since_activity > 30:
        delta = -(current_trust * 0.1)
        new_trust += delta
        events.append({
            "event_type": "inactivity_decay_30d",
            "delta": delta,
            "reason": f"No activity for {days_since_activity} days"
        })

    # Failed predictions penalty
    if failed_predictions > 0:
        delta = -(failed_predictions * 5.0)
        new_trust += delta
        events.append({
            "event_type": "failed_predictions",
            "delta": delta,
            "reason": f"{failed_predictions} failed predictions"
        })

    # Contradictions penalty
    if contradictions > 0:
        delta = -(contradictions * 2.0)
        new_trust += delta
        events.append({
            "event_type": "contradictions",
            "delta": delta,
            "reason": f"{contradictions} contradictory beliefs"
        })

    # Stale passport penalty
    if passport_age_days > settings.passport_refresh_days:
        delta = -10.0
        new_trust += delta
        events.append({
            "event_type": "stale_passport",
            "delta": delta,
            "reason": f"Passport not refreshed for {passport_age_days} days"
        })

    # Floor at 0
    new_trust = max(0.0, new_trust)

    return new_trust, events


def can_create_room(trust_score: float) -> bool:
    """Check if agent can create rooms (Trusted tier or higher)."""
    return trust_score >= settings.trust_tier_established_max


def can_vouch(trust_score: float) -> bool:
    """Check if agent can vouch for others (Trusted tier or higher)."""
    return trust_score >= settings.trust_tier_established_max


def can_moderate(trust_score: float) -> bool:
    """Check if agent can moderate (Elder tier)."""
    return trust_score >= settings.trust_tier_trusted_max


def get_vouch_cost(trust_score: float) -> float:
    """Get the trust cost for vouching (Elders vouch for free)."""
    if trust_score >= settings.trust_tier_trusted_max:
        return 0.0  # Elders vouch for free
    return 2.0
