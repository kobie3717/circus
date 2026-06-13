"""
Memory engine: triage, atomic extraction, scoring, consolidation.
Paper 1 (Universal Memory Architecture) implementation.
"""
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Optional, List

# Half-lives in days per claim type
HALF_LIFE_DAYS = {'episodic': 14, 'semantic': 180, 'procedural': 365, 'identity': 3650}
ALPHA = 0.15      # reinforcement rate
BETA = 0.35       # contradiction penalty
DUP_THRESHOLD = 0.92  # cosine similarity for near-duplicate detection
MIN_SCORE = 0.35  # recall gate
MIN_CONFIDENCE = 0.25  # minimum confidence to surface
IMPORTANCE_FLOOR = 0.30  # below this = leave in episodic only

# Embedding model singleton (lazy-loaded)
_embedding_model = None


def get_embedding_model():
    """Lazy-load sentence-transformers model for semantic embeddings."""
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            # Use all-MiniLM-L6-v2: small (80MB), fast, good quality
            _embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
        except ImportError:
            # Fallback: no embeddings if library not available
            _embedding_model = False
    return _embedding_model if _embedding_model is not False else None


def generate_embedding(text: str) -> Optional[List[float]]:
    """
    Generate semantic embedding for text using sentence-transformers.
    Returns 384-dimensional vector (all-MiniLM-L6-v2) or None if model unavailable.
    """
    model = get_embedding_model()
    if model is None:
        return None

    try:
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()
    except Exception:
        return None


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    magnitude_a = math.sqrt(sum(a * a for a in vec_a))
    magnitude_b = math.sqrt(sum(b * b for b in vec_b))

    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0

    return dot_product / (magnitude_a * magnitude_b)


def compute_decay_rate(claim_type: str) -> float:
    """λ = ln(2) / half_life_days"""
    h = HALF_LIFE_DAYS.get(claim_type, 180)
    return math.log(2) / h


def compute_recency(claim_type: str, days_since_access: float) -> float:
    """Exponential decay: recency = exp(-λ * days)"""
    lam = compute_decay_rate(claim_type)
    return math.exp(-lam * days_since_access)


def compute_final_score(
    confidence: float,
    importance: float,
    recency: float,
    cosine_sim: float = 0.0,
    bm25_norm: float = 0.0,
) -> float:
    """
    final_score = 0.45*cosine + 0.20*bm25 + 0.15*recency + 0.10*importance + 0.10*confidence
    Used for recall ranking.
    """
    return (
        0.45 * cosine_sim
        + 0.20 * bm25_norm
        + 0.15 * recency
        + 0.10 * importance
        + 0.10 * confidence
    )


def triage_importance(content: str, context: dict | None = None) -> float:
    """
    Assign importance score [0,1] based on signal heuristics.
    No LLM — deterministic keyword + pattern matching.

    Priority order (from Paper 1 §4.2):
    - Explicit instruction keywords >= 0.9
    - Corrections >= 0.85
    - Decisions/commitments >= 0.8
    - Identity/preferences >= 0.7
    - Outcomes >= 0.6
    - Surprises >= 0.6
    - Recurrence (handled separately)
    """
    text = content.lower()

    # Explicit instructions
    if any(k in text for k in ['remember this', 'always ', 'never ', 'from now on', 'important:']):
        return 0.92

    # Corrections
    if any(k in text for k in ['correction:', 'actually', 'wrong:', 'fixed:', 'mistake']):
        return 0.85

    # Decisions/commitments
    if any(k in text for k in ['decided', 'committed', 'will always', 'policy:', 'rule:', 'constraint:']):
        return 0.80

    # Identity/preferences
    if any(k in text for k in ['prefer', 'like ', 'dislike', 'my style', 'our approach', 'team norm']):
        return 0.72

    # Outcomes
    if any(k in text for k in ['succeeded', 'failed', 'completed', 'result:', 'outcome:']):
        return 0.62

    # Surprises
    if any(k in text for k in ['unexpected', 'surprising', 'turns out', 'actually found']):
        return 0.62

    # Default
    return 0.45


def extract_atomic_claims(content: str, agent_id: str, namespace_id: str, claim_type: str = 'semantic') -> list[dict]:
    """
    Split compound content into atomic claims.
    Each claim = one assertable statement about one subject.

    Simple heuristic splitting — no LLM (keep it deterministic and cheap).
    For real deployments, replace with LLM extraction pass.
    """
    claims = []

    # Split on sentence boundaries
    import re
    sentences = re.split(r'[.!?]\s+', content.strip())

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 10:  # skip fragments
            continue

        importance = triage_importance(sentence)
        if importance < IMPORTANCE_FLOOR:
            continue  # leave in episodic only

        # Extract subject (first noun phrase heuristic — first capitalized word or "it"/"the system")
        words = sentence.split()
        subject = None
        for word in words[:5]:
            clean = word.strip('.,;:').capitalize()
            if clean and clean[0].isupper() and len(clean) > 2:
                subject = clean
                break

        claim_id = str(uuid.uuid4())
        claims.append({
            'id': claim_id,
            'namespace_id': namespace_id,
            'agent_id': agent_id,
            'claim_text': sentence,
            'subject': subject,
            'claim_type': claim_type,
            'importance': importance,
            'confidence': 0.6,
            'status': 'candidate',
            'source': f'agent:{agent_id}',
            'episode_id': None,
            'created_at': datetime.utcnow().isoformat(),
            'last_accessed': datetime.utcnow().isoformat(),
            'access_count': 0,
            'decay_rate': compute_decay_rate(claim_type),
        })

    return claims


def check_near_duplicate(conn: sqlite3.Connection, claim_text: str, namespace_id: str) -> Optional[str]:
    """
    Check if a similar claim already exists (simple lexical overlap — no vectors yet).
    Returns existing claim_id if near-duplicate found, else None.
    Uses FTS5 to find candidates, then computes rough word-overlap ratio.
    """
    cursor = conn.cursor()

    # FTS search for candidates
    try:
        rows = cursor.execute(
            "SELECT id, claim_text FROM memory_claims WHERE namespace_id = ? AND status IN ('candidate', 'active') LIMIT 20",
            (namespace_id,)
        ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    # Simple Jaccard similarity
    words_new = set(claim_text.lower().split())
    for row_id, row_text in rows:
        words_existing = set(row_text.lower().split())
        if not words_new or not words_existing:
            continue
        intersection = words_new & words_existing
        union = words_new | words_existing
        jaccard = len(intersection) / len(union) if union else 0
        if jaccard >= 0.7:  # ~DUP_THRESHOLD proxy without vectors
            return row_id

    return None


def reinforce_claim(conn: sqlite3.Connection, claim_id: str) -> None:
    """c_new = c + ALPHA * (1 - c)  — diminishing returns reinforcement"""
    cursor = conn.cursor()
    row = cursor.execute("SELECT confidence FROM memory_claims WHERE id = ?", (claim_id,)).fetchone()
    if not row:
        return
    c = row[0]
    c_new = c + ALPHA * (1 - c)
    cursor.execute(
        "UPDATE memory_claims SET confidence = ?, access_count = access_count + 1, last_accessed = ? WHERE id = ?",
        (min(c_new, 1.0), datetime.utcnow().isoformat(), claim_id)
    )
    conn.commit()


def contradict_claim(conn: sqlite3.Connection, old_claim_id: str, new_claim_id: str) -> None:
    """
    Supersede old claim with new one.
    Hysteresis: only supersede if new claim importance > 0.5 OR old confidence < 0.7.
    old.confidence *= (1 - BETA), old.status = 'superseded', old.superseded_by = new_id
    """
    cursor = conn.cursor()
    old = cursor.execute(
        "SELECT confidence, importance FROM memory_claims WHERE id = ?", (old_claim_id,)
    ).fetchone()
    new = cursor.execute(
        "SELECT importance FROM memory_claims WHERE id = ?", (new_claim_id,)
    ).fetchone()

    if not old or not new:
        return

    old_confidence, old_importance = old
    new_importance = new[0]

    # Hysteresis: require confirmation to displace high-confidence claims
    if old_confidence >= 0.7 and new_importance < 0.5:
        # Don't supersede yet — mark contradiction as pending
        return

    # Supersede
    new_confidence = old_confidence * (1 - BETA)
    cursor.execute(
        """UPDATE memory_claims
           SET confidence = ?, status = 'superseded', superseded_by = ?
           WHERE id = ?""",
        (new_confidence, new_claim_id, old_claim_id)
    )
    # Activate the new claim
    cursor.execute(
        "UPDATE memory_claims SET status = 'active' WHERE id = ?",
        (new_claim_id,)
    )
    conn.commit()


def run_consolidation(conn: sqlite3.Connection) -> dict:
    """
    Nightly consolidation pass:
    1. Promote candidates seen 2+ times (or high importance) to active
    2. Archive active claims whose composite score < ARCHIVE_THRESHOLD for 2 consecutive checks
    3. Mine recurring patterns: episodic -> semantic (seen 3+ times)

    Returns stats dict.
    """
    cursor = conn.cursor()
    stats = {'promoted': 0, 'archived': 0, 'episodic_to_semantic': 0}

    now = datetime.utcnow()

    # 1. Promote candidates
    candidates = cursor.execute(
        "SELECT id, access_count, importance FROM memory_claims WHERE status = 'candidate'"
    ).fetchall()

    for claim_id, access_count, importance in candidates:
        if access_count >= 2 or importance >= 0.8:
            cursor.execute(
                "UPDATE memory_claims SET status = 'active' WHERE id = ?",
                (claim_id,)
            )
            stats['promoted'] += 1

    # 2. Archive low-score active claims
    active = cursor.execute(
        "SELECT id, confidence, importance, last_accessed, claim_type FROM memory_claims WHERE status = 'active'"
    ).fetchall()

    for claim_id, confidence, importance, last_accessed, claim_type in active:
        try:
            last = datetime.fromisoformat(last_accessed) if last_accessed else now - timedelta(days=30)
        except Exception:
            last = now - timedelta(days=30)
        days_since = (now - last).days
        recency = compute_recency(claim_type, days_since)
        composite = 0.4 * confidence + 0.3 * importance + 0.3 * recency
        if composite < 0.2:
            cursor.execute(
                "UPDATE memory_claims SET status = 'archived' WHERE id = ?",
                (claim_id,)
            )
            stats['archived'] += 1

    # 3. Episodic -> semantic promotion (seen 3+ times = recurring pattern)
    episodic_recurring = cursor.execute(
        """SELECT id, claim_text, namespace_id, agent_id
           FROM memory_claims
           WHERE claim_type = 'episodic' AND status = 'active' AND access_count >= 3"""
    ).fetchall()

    for claim_id, claim_text, namespace_id, agent_id in episodic_recurring:
        cursor.execute(
            "UPDATE memory_claims SET claim_type = 'semantic' WHERE id = ?",
            (claim_id,)
        )
        stats['episodic_to_semantic'] += 1

    conn.commit()
    return stats


def recall_claims(
    conn: sqlite3.Connection,
    query: str,
    namespace_id: Optional[str] = None,
    limit: int = 10,
    min_confidence: float = MIN_CONFIDENCE,
) -> list[dict]:
    """
    4-channel hybrid recall: semantic (vector) + lexical (BM25/FTS) + subject + temporal.
    Returns top claims ranked by final_score with cosine similarity.
    """
    cursor = conn.cursor()
    now = datetime.utcnow()
    candidates = {}

    # Generate query embedding for semantic search
    query_embedding = generate_embedding(query)

    # Channel 1: FTS lexical (BM25)
    try:
        fts_rows = cursor.execute(
            """SELECT mc.id, mc.claim_text, mc.subject, mc.confidence, mc.importance,
                      mc.last_accessed, mc.claim_type, mc.namespace_id, mc.agent_id, mc.status, mc.embedding
               FROM fts_memory_claims fts
               JOIN memory_claims mc ON mc.rowid = fts.rowid
               WHERE fts_memory_claims MATCH ? AND mc.status IN ('candidate', 'active')
               AND mc.confidence >= ?
               LIMIT 40""",
            (query, min_confidence)
        ).fetchall()
        for row in fts_rows:
            candidates[row[0]] = tuple(row) + (0.6,)  # Convert Row to tuple, add bm25_norm
    except Exception:
        pass

    # Channel 2: Subject match (entity-based)
    try:
        words = query.split()
        for word in words[:3]:
            subj_rows = cursor.execute(
                """SELECT id, claim_text, subject, confidence, importance,
                          last_accessed, claim_type, namespace_id, agent_id, status, embedding
                   FROM memory_claims
                   WHERE subject LIKE ? AND status IN ('candidate', 'active')
                   AND confidence >= ?
                   LIMIT 20""",
                (f'%{word}%', min_confidence)
            ).fetchall()
            for row in subj_rows:
                if row[0] not in candidates:
                    candidates[row[0]] = tuple(row) + (0.4,)  # Convert Row to tuple
    except Exception:
        pass

    # Channel 3: Temporal — recent claims always candidate
    try:
        recent_rows = cursor.execute(
            """SELECT id, claim_text, subject, confidence, importance,
                      last_accessed, claim_type, namespace_id, agent_id, status, embedding
               FROM memory_claims
               WHERE status IN ('candidate', 'active') AND confidence >= ?
               ORDER BY last_accessed DESC LIMIT 10""",
            (min_confidence,)
        ).fetchall()
        for row in recent_rows:
            if row[0] not in candidates:
                candidates[row[0]] = tuple(row) + (0.3,)  # Convert Row to tuple
    except Exception:
        pass

    # Score and rank
    results = []
    for claim_id, row in candidates.items():
        _, claim_text, subject, confidence, importance, last_accessed, claim_type, namespace_id_c, agent_id, status, embedding_json, bm25_norm = row

        # Compute cosine similarity if embeddings available
        cosine_sim = 0.0
        if query_embedding and embedding_json:
            try:
                claim_embedding = json.loads(embedding_json)
                cosine_sim = cosine_similarity(query_embedding, claim_embedding)
            except Exception:
                pass

        try:
            last = datetime.fromisoformat(last_accessed) if last_accessed else now - timedelta(days=30)
        except Exception:
            last = now - timedelta(days=30)
        days_since = max(0, (now - last).days)
        recency = compute_recency(claim_type, days_since)
        score = compute_final_score(confidence, importance, recency, cosine_sim=cosine_sim, bm25_norm=bm25_norm)

        if score >= MIN_SCORE:
            results.append({
                'id': claim_id,
                'claim_text': claim_text,
                'subject': subject,
                'confidence': confidence,
                'importance': importance,
                'claim_type': claim_type,
                'namespace_id': namespace_id_c,
                'agent_id': agent_id,
                'status': status,
                'score': round(score, 4),
                'recency': round(recency, 4),
                'cosine_sim': round(cosine_sim, 4),
            })

    # Namespace filter if specified
    if namespace_id:
        results = [r for r in results if r['namespace_id'] == namespace_id]

    # Sort by score desc, deduplicate by subject
    results.sort(key=lambda x: x['score'], reverse=True)
    seen_subjects = set()
    deduped = []
    for r in results:
        key = r.get('subject') or r['claim_text'][:40]
        if key not in seen_subjects:
            seen_subjects.add(key)
            deduped.append(r)

    return deduped[:limit]
