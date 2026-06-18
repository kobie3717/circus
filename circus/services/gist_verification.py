"""
Gist verification service — DeLM Phase 2.

Verifies that a compressed gist is grounded in its raw evidence.
Uses lightweight rule-based checks (no LLM calls for Phase 2).
"""
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class GistVerificationResult:
    valid: bool
    reason: Optional[str] = None  # None on success, reason code on failure


def verify_gist_against_evidence(
    gist: str,
    raw: str,
    category: str,
    summary: Optional[str] = None,
) -> GistVerificationResult:
    """
    Verify gist is grounded in raw evidence.

    Rule-based checks (fast, no LLM):
    1. Gist not empty
    2. Gist not longer than raw (compression must shrink, not hallucinate new content)
    3. Key entities in gist exist in raw (name/number overlap check)
    4. Gist doesn't introduce negation not present in raw (basic contradiction check)

    Returns GistVerificationResult(valid=True) if passes all checks.
    """
    # Check 1: non-empty
    if not gist or not gist.strip():
        return GistVerificationResult(valid=False, reason="gist_empty")

    # Check 2: if raw is short, gist must not be longer (expansion = hallucination risk)
    if len(raw) < 200 and len(gist) > len(raw) * 1.2:
        return GistVerificationResult(valid=False, reason="gist_longer_than_short_raw")

    # Check 3: key entity overlap
    # Extract capitalized words, numbers, quoted strings from gist
    gist_entities = set(re.findall(r'\b[A-Z][a-zA-Z0-9]+\b|\b\d+\b|"[^"]{2,30}"', gist))
    raw_lower = raw.lower()

    missing = []
    for entity in gist_entities:
        if entity.lower() not in raw_lower:
            missing.append(entity)

    # Allow up to 1 missing entity (might be abbreviation or minor variant)
    if len(missing) > 1:
        return GistVerificationResult(
            valid=False,
            reason=f"gist_entities_not_in_raw:{','.join(missing[:3])}"
        )

    # Check 4: negation contradiction
    # Simple check: if gist has negation words that raw doesn't have (and vice versa)
    # Extract negation phrases from gist
    gist_negations = re.findall(r'\b(?:not|never|no|don\'t|doesn\'t|won\'t|can\'t)\b', gist.lower())
    raw_negations = re.findall(r'\b(?:not|never|no|don\'t|doesn\'t|won\'t|can\'t)\b', raw.lower())

    # If gist has 2+ negations but raw has none, likely contradiction
    if len(gist_negations) >= 2 and len(raw_negations) == 0:
        return GistVerificationResult(
            valid=False,
            reason="gist_contradicts_raw:added_negations"
        )

    # If raw has no negations but gist says "never" or "not" prominently, flag it
    if len(raw_negations) == 0 and any(word in gist.lower() for word in ['never use', 'not work', 'don\'t use']):
        return GistVerificationResult(
            valid=False,
            reason="gist_contradicts_raw:inverted_meaning"
        )

    return GistVerificationResult(valid=True)


async def verify_with_retry(
    content: str,
    category: str,
    max_retries: int = 2,
) -> tuple[dict, GistVerificationResult]:
    """
    Compress + verify with up to max_retries attempts.

    Returns (final_gist, verification_result).
    If all retries fail, returns (best_gist, last_failure_result).
    """
    from circus.services.gist_compression import compress_to_gist

    best_compressed = None
    last_result = None

    for attempt in range(max_retries + 1):
        compressed = await compress_to_gist(content, category)
        result = verify_gist_against_evidence(
            gist=compressed['gist'],
            raw=compressed['raw'],
            category=category,
            summary=compressed.get('summary'),
        )

        if result.valid:
            return compressed, result

        best_compressed = compressed
        last_result = result

    # All retries failed — return best attempt with failure result
    return best_compressed, last_result
