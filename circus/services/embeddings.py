"""Vector embeddings for semantic agent discovery."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Optional

# Lazy import for sentence-transformers to make it optional
_model = None


def get_embedding_model():
    """Get cached embedding model (lazy load)."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            # Use all-MiniLM-L6-v2 (384 dimensions, same as AI-IQ)
            _model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
        except ImportError:
            raise RuntimeError(
                "sentence-transformers not installed. Install with: pip install sentence-transformers"
            )
    return _model


async def embed_text(text: str) -> list[float]:
    """
    Generate embedding for text.

    Args:
        text: Input text

    Returns:
        384-dimension float vector
    """
    model = get_embedding_model()
    embedding = await asyncio.to_thread(model.encode, text, normalize_embeddings=True)
    return embedding.tolist()


async def embed_agent_profile(
    name: str,
    role: str,
    capabilities: list[str]
) -> list[float]:
    """
    Generate embedding for agent profile.

    Args:
        name: Agent name
        role: Agent role
        capabilities: List of capabilities

    Returns:
        384-dimension float vector
    """
    # Concatenate profile text
    profile_text = f"{name} {role} {' '.join(capabilities)}"
    return await embed_text(profile_text)


async def search_similar_agents_vector(
    query: str,
    db_path: Path,
    limit: int = 20,
    min_score: float = 0.5
) -> list[tuple[str, float]]:
    """
    Search for similar agents using sqlite-vec vector similarity.

    Args:
        query: Search query text
        db_path: Path to SQLite database
        limit: Maximum results
        min_score: Minimum cosine similarity (0-1)

    Returns:
        List of (agent_id, similarity_score) tuples
    """
    model = get_embedding_model()

    # Embed query (offload to thread pool to avoid blocking)
    query_embedding = await asyncio.to_thread(model.encode, query, normalize_embeddings=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Try to load sqlite-vec extension
    has_vec = False
    try:
        conn.enable_load_extension(True)
        try:
            conn.load_extension("vec0")
            has_vec = True
        except sqlite3.OperationalError:
            # Try alternative paths
            for path in ["/usr/local/lib/vec0.so", "/usr/lib/vec0.so"]:
                try:
                    conn.load_extension(path)
                    has_vec = True
                    break
                except sqlite3.OperationalError:
                    continue
        conn.enable_load_extension(False)
    except Exception:
        pass

    cursor = conn.cursor()

    if has_vec:
        # Use sqlite-vec for fast vector search
        query_bytes = query_embedding.tobytes()
        cursor.execute("""
            SELECT agent_id, 1.0 - vec_distance_cosine(embedding, ?) as similarity
            FROM agent_embeddings
            WHERE similarity >= ?
            ORDER BY similarity DESC
            LIMIT ?
        """, (query_bytes, min_score, limit))
    else:
        # Fallback: compute cosine similarity in Python
        return search_similar_agents_fallback(query_embedding, db_path, limit, min_score)

    results = []
    for row in cursor.fetchall():
        results.append((row["agent_id"], row["similarity"]))

    conn.close()
    return results


def search_similar_agents_fallback(
    query_embedding,
    db_path: Path,
    limit: int,
    min_score: float
) -> list[tuple[str, float]]:
    """
    Fallback semantic search using Python cosine similarity.

    Args:
        query_embedding: Query embedding vector
        db_path: Database path
        limit: Max results
        min_score: Min similarity

    Returns:
        List of (agent_id, similarity) tuples
    """
    import numpy as np

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all embeddings
    cursor.execute("SELECT agent_id, embedding_json FROM agent_embeddings")
    rows = cursor.fetchall()

    results = []
    for row in rows:
        agent_id = row["agent_id"]
        embedding = np.array(json.loads(row["embedding_json"]))

        # Compute cosine similarity
        similarity = float(np.dot(query_embedding, embedding))

        if similarity >= min_score:
            results.append((agent_id, similarity))

    # Sort by similarity descending
    results.sort(key=lambda x: x[1], reverse=True)

    conn.close()
    return results[:limit]
