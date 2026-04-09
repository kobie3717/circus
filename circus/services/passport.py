"""Passport service for importing and validating AI-IQ passports."""

import hashlib
import json
from typing import Any


def extract_passport_info(passport: dict[str, Any]) -> dict[str, Any]:
    """Extract key information from AI-IQ passport."""
    # Extract predictions
    predictions = passport.get("predictions", {})
    confirmed = predictions.get("confirmed", 0)
    refuted = predictions.get("refuted", 0)
    total_predictions = confirmed + refuted
    prediction_accuracy = confirmed / total_predictions if total_predictions > 0 else 0.0

    # Extract beliefs
    beliefs = passport.get("beliefs", {})
    total_beliefs = beliefs.get("total", 0)
    contradictions = beliefs.get("contradictions", 0)
    belief_stability = 1 - (contradictions / total_beliefs) if total_beliefs > 0 else 1.0

    # Extract memory quality
    memory_stats = passport.get("memory_stats", {})
    proof_count_avg = memory_stats.get("proof_count_avg", 0.0)
    graph_connections = memory_stats.get("graph_connections", 0)

    # Normalize memory quality (0-1 scale)
    proof_score = min(1.0, proof_count_avg / 5.0)
    graph_score = min(1.0, graph_connections / 20.0)
    memory_quality = (proof_score + graph_score) / 2.0

    # Extract passport score
    score = passport.get("score", {})
    passport_score = score.get("total", 0.0)  # 0-10 scale

    # Extract capabilities
    capabilities = passport.get("capabilities", [])

    # Extract entities
    graph_summary = passport.get("graph_summary", {})
    entities = graph_summary.get("entities", [])

    # Extract traits
    traits = passport.get("traits", {})

    return {
        "prediction_accuracy": prediction_accuracy,
        "belief_stability": belief_stability,
        "memory_quality": memory_quality,
        "passport_score": passport_score,
        "capabilities": capabilities,
        "entities": entities,
        "traits": traits,
        "total_predictions": total_predictions,
        "confirmed_predictions": confirmed,
        "refuted_predictions": refuted,
        "total_beliefs": total_beliefs,
        "contradictions": contradictions,
        "proof_count_avg": proof_count_avg,
        "graph_connections": graph_connections,
    }


def validate_passport(passport: dict[str, Any], raise_error: bool = False) -> bool:
    """
    Validate passport structure.

    Args:
        passport: Passport data to validate
        raise_error: If True, raise ValueError on validation failure. If False, return False.

    Returns:
        True if valid, False otherwise (when raise_error=False)
    """
    required_fields = ["identity", "score"]

    for field in required_fields:
        if field not in passport:
            if raise_error:
                raise ValueError(f"Missing required passport field: {field}")
            return False

    # Validate identity
    identity = passport.get("identity", {})
    if "name" not in identity:
        if raise_error:
            raise ValueError("Passport identity must include name")
        return False

    return True


def compute_passport_hash(passport: dict[str, Any]) -> str:
    """Compute hash of passport for fingerprinting."""
    passport_json = json.dumps(passport, sort_keys=True)
    return hashlib.sha256(passport_json.encode()).hexdigest()
