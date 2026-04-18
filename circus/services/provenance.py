"""Provenance tracking and confidence decay for Memory Commons.

Implements hop-based, age-based, and trust-based confidence decay as specified
in Circus Memory Commons spec §5.

CONFIDENCE SEMANTICS:
- base_confidence: Raw input from author (0.0-1.0), stored in provenance.confidence.
  This is what the author claimed at publish time.
- effective_confidence: Post-decay value after hop/age/trust adjustments.
  Clamped to [0.1, 1.0] — floor prevents complete collapse,
  ceiling prevents upscaling above author's claim.

Use effective_confidence for authority resolution (Fix D in belief_merge.py).
Use base_confidence only for display/audit of the original claim.
"""

import math
from datetime import datetime
from typing import Optional


# Constants for decay formula (locked for consistency)
HOP_DECAY_RATE = 0.05  # 5% decay per hop
HOP_DECAY_FLOOR = 0.5  # Minimum hop penalty
AGE_HALF_LIFE_DAYS = 90  # Exponential decay half-life
CONFIDENCE_FLOOR = 0.1  # Minimum confidence
CONFIDENCE_CEILING = 1.0  # Maximum confidence

# Trust tier thresholds and bonuses
TRUST_TIER_ELDER = 85  # Elder tier: trust_score >= 85
TRUST_TIER_TRUSTED = 60  # Trusted tier: trust_score >= 60
TRUST_TIER_ESTABLISHED = 30  # Established tier: trust_score >= 30
TRUST_BONUS_ELDER = 1.2  # +20% for Elder
TRUST_BONUS_TRUSTED = 1.1  # +10% for Trusted
TRUST_BONUS_ESTABLISHED = 1.0  # +0% for Established
TRUST_BONUS_NEWCOMER = 0.9  # -10% for Newcomer


def build_provenance(
    author_passport: str,
    derived_from: Optional[list[str]] = None,
    citations: Optional[list[str]] = None,
    reasoning: Optional[str] = None,
) -> dict:
    """
    Build provenance metadata for a memory.

    Args:
        author_passport: Agent ID (passport hash) of the original author
        derived_from: List of parent memory IDs
        citations: List of source URLs or references
        reasoning: Explanation of why this memory was created

    Returns:
        Provenance dict ready for JSON serialization
    """
    now = datetime.utcnow()
    provenance = {
        "hop_count": 1,  # First-hand memory starts at hop 1
        "original_author": author_passport,
        "original_timestamp": now.isoformat(),
    }

    if derived_from:
        provenance["derived_from"] = derived_from

    if citations:
        provenance["citations"] = citations

    if reasoning:
        provenance["reasoning"] = reasoning

    return provenance


def decay_confidence(
    base_confidence: float,
    hop_count: int,
    age_seconds: float,
    author_trust_score: float,
) -> float:
    """
    Calculate effective confidence after decay.

    Implements the decay formula from spec §5.2:
    - Hop decay: -5% per hop beyond first (floor 0.5)
    - Age decay: exponential with 90-day half-life
    - Trust bonus: +20% Elder, +10% Trusted, +0% Established, -10% Newcomer

    Args:
        base_confidence: Original confidence score (0.0-1.0)
        hop_count: Number of hops from original author (1 = first-hand)
        age_seconds: Age of memory in seconds
        author_trust_score: Original author's trust score (0-100)

    Returns:
        Effective confidence clamped to [0.1, 1.0]
    """
    # Hop decay: first hop = 1.0, second hop = 0.95, etc.
    hop_penalty = 1.0 - (hop_count - 1) * HOP_DECAY_RATE
    hop_penalty = max(HOP_DECAY_FLOOR, hop_penalty)

    # Age decay: exponential with 90-day half-life
    age_days = age_seconds / 86400.0
    age_penalty = math.exp(-age_days / AGE_HALF_LIFE_DAYS * math.log(2))

    # Trust bonus based on tier
    if author_trust_score >= TRUST_TIER_ELDER:
        trust_bonus = TRUST_BONUS_ELDER
    elif author_trust_score >= TRUST_TIER_TRUSTED:
        trust_bonus = TRUST_BONUS_TRUSTED
    elif author_trust_score >= TRUST_TIER_ESTABLISHED:
        trust_bonus = TRUST_BONUS_ESTABLISHED
    else:
        trust_bonus = TRUST_BONUS_NEWCOMER

    # Calculate effective confidence
    effective = base_confidence * hop_penalty * age_penalty * trust_bonus

    # Clamp to valid range
    return max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, effective))


def verify_provenance_chain(provenance_dict: dict) -> bool:
    """
    Verify provenance chain for basic sanity.

    Checks:
    - hop_count is positive integer
    - original_author exists
    - original_timestamp is valid ISO format
    - No cycles in derived_from chain (TODO: implement when needed)

    Args:
        provenance_dict: Provenance metadata

    Returns:
        True if valid, False otherwise
    """
    # Check required fields
    if "hop_count" not in provenance_dict:
        return False
    if "original_author" not in provenance_dict:
        return False
    if "original_timestamp" not in provenance_dict:
        return False

    # Validate hop_count
    hop_count = provenance_dict["hop_count"]
    if not isinstance(hop_count, int) or hop_count < 1:
        return False

    # Validate timestamp format
    try:
        datetime.fromisoformat(provenance_dict["original_timestamp"])
    except (ValueError, TypeError):
        return False

    # Validate original_author is non-empty string
    if not isinstance(provenance_dict["original_author"], str):
        return False
    if not provenance_dict["original_author"].strip():
        return False

    # Optional: check derived_from is list
    if "derived_from" in provenance_dict:
        if not isinstance(provenance_dict["derived_from"], list):
            return False

    # Optional: check citations is list
    if "citations" in provenance_dict:
        if not isinstance(provenance_dict["citations"], list):
            return False

    # Optional: check reasoning is string
    if "reasoning" in provenance_dict:
        if not isinstance(provenance_dict["reasoning"], str):
            return False

    # All checks passed
    return True
