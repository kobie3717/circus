"""
LLM-as-judge for eval scoring.
Uses Claude Haiku to score agent answers against rubric criteria.
Falls back to keyword matching if API call fails.
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Cache: (eval_id, answer_hash) -> score result
# In-memory only — resets on restart. Good enough (evals don't change often)
_score_cache: dict[str, dict] = {}


def _keyword_score(answer: str, rubric: list[str]) -> tuple[float, list[str], list[str]]:
    """Fallback keyword scoring."""
    answer_lower = answer.lower()
    matched, missed = [], []
    for item in rubric:
        key_terms = item.lower().split()
        if any(term in answer_lower for term in key_terms if len(term) > 3):
            matched.append(item)
        else:
            missed.append(item)
    score = len(matched) / len(rubric) if rubric else 0.0
    return score, matched, missed


def score_answer(
    eval_id: str,
    task_input: str,
    task_description: str,
    answer: str,
    rubric: list[str],
) -> dict:
    """
    Score an agent's answer against eval rubric using Claude Haiku.

    Returns:
        {
            "score": float,           # 0.0 - 1.0
            "matched_items": [...],   # rubric items satisfied
            "missed_items": [...],    # rubric items not satisfied
            "reasoning": str,         # brief judge reasoning
            "method": "llm" | "keyword"  # which method was used
        }
    """
    import hashlib
    cache_key = f"{eval_id}:{hashlib.sha256(answer.encode()).hexdigest()[:16]}"
    if cache_key in _score_cache:
        return _score_cache[cache_key]

    # Try LLM scoring first
    try:
        result = _llm_score(task_input, task_description, answer, rubric)
        result["method"] = "llm"
        _score_cache[cache_key] = result
        return result
    except Exception as e:
        logger.warning(f"LLM judge failed, falling back to keyword: {e}")
        score, matched, missed = _keyword_score(answer, rubric)
        result = {
            "score": score,
            "matched_items": matched,
            "missed_items": missed,
            "reasoning": f"Keyword fallback (LLM failed: {str(e)[:100]})",
            "method": "keyword"
        }
        _score_cache[cache_key] = result
        return result


def _llm_score(task_input: str, task_description: str, answer: str, rubric: list[str]) -> dict:
    """Call Claude Haiku to score the answer."""
    import anthropic

    # Build rubric as numbered list
    rubric_text = "\n".join(f"{i+1}. {item}" for i, item in enumerate(rubric))

    prompt = f"""You are an expert evaluator. Score this answer against the rubric criteria.

TASK: {task_description}

INPUT:
{task_input}

AGENT'S ANSWER:
{answer}

RUBRIC CRITERIA (each must be satisfied):
{rubric_text}

For each rubric criterion, determine if the answer satisfies it.
Be strict but fair — the answer must actually address the criterion, not just mention related words.

Respond with ONLY valid JSON in this exact format:
{{
  "criteria": [
    {{"item": "criterion text", "satisfied": true/false, "reason": "1 sentence why"}}
  ],
  "overall_reasoning": "1-2 sentences summary"
}}"""

    # Get API key — try env first, then OAuth proxy token file
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        token_file = os.path.expanduser("~/.config/claude-oauth-proxy/token")
        if os.path.exists(token_file):
            api_key = open(token_file).read().strip()

    if not api_key:
        raise ValueError("No Anthropic API key found")

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text.strip()

    # Parse JSON response
    # Strip markdown code blocks if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    data = json.loads(text)

    matched = [c["item"] for c in data["criteria"] if c.get("satisfied")]
    missed = [c["item"] for c in data["criteria"] if not c.get("satisfied")]
    score = len(matched) / len(rubric) if rubric else 0.0

    return {
        "score": score,
        "matched_items": matched,
        "missed_items": missed,
        "reasoning": data.get("overall_reasoning", ""),
    }
