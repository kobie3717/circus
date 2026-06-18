"""
Gist compression service — DeLM Phase 1.
Compresses memory content to ~100-token gist, with optional summary layer.
Short content (<200 chars) is stored as-is (no compression needed).
"""
import re
from typing import Optional


def _approx_tokens(text: str) -> int:
    """Approximate token count (rough heuristic: 4 chars per token)."""
    return len(text) // 4


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to max_tokens (approximate)."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


async def compress_to_gist(
    content: str,
    category: str,
    source_type: str = "text",
    feedback: Optional[str] = None
) -> dict:
    """
    Compress content to gist, with optional summary layer.

    Returns dict with keys:
    - gist: ~100 token compressed content (always present)
    - summary: ~300-500 token summary (only for long content)
    - raw: full original content
    - source_type: content type (text, code, etc.)
    """
    raw = content

    # Short content — no compression (threshold: 50 tokens ~200 chars)
    if _approx_tokens(content) <= 50:
        return {"gist": content, "summary": None, "raw": raw, "source_type": source_type}

    # Medium content (50-500 tokens) — compress to gist only
    if _approx_tokens(content) <= 500:
        gist = _compress_medium(content, category)
        return {"gist": gist, "summary": None, "raw": raw, "source_type": source_type}

    # Long content — build summary then compress to gist
    summary = _summarize_long(content, category)
    gist = _compress_medium(summary, category)
    return {"gist": gist, "summary": summary, "raw": raw, "source_type": source_type}


def _compress_medium(content: str, category: str) -> str:
    """
    Compress medium-length content to ~100 token gist.
    Rule-based: take first 1-2 sentences depending on category.
    """
    # Normalize whitespace
    content = re.sub(r'\s+', ' ', content.strip())

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', content)

    # Category-specific rules
    if category in ('learning', 'error', 'decision'):
        # More context needed for these categories
        gist = ' '.join(sentences[:2])
    else:
        # Default: first sentence
        gist = sentences[0] if sentences else content

    return _truncate_to_tokens(gist, 100)


def _summarize_long(content: str, category: str) -> str:
    """
    Summarize long content to ~300-500 token summary.
    Rule-based: extract key sentences.
    """
    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', content)

    # Extract key sentences: first + every 5th + ensure we get at least 3-5 sentences
    key = [sentences[0]] if sentences else []
    for i in range(1, len(sentences)):
        if i % 5 == 0 or len(key) < 3:
            key.append(sentences[i])
        if len(key) >= 5:
            break

    return _truncate_to_tokens(' '.join(key), 400)
