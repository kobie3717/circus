"""LinUCB bandit-based task routing with contextual learning.

Pure logic + DB I/O. No FastAPI dependencies.
"""

import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from circus.services.bandit import ArmState, pick, is_cold_start, alpha_schedule

logger = logging.getLogger(__name__)

# Feature dimensionality (per spec)
FEATURE_DIM = 32

# Global decision counter for alpha schedule
_decision_counter = 0


def _time_of_day_bucket(dt: datetime) -> int:
    """Convert datetime to time-of-day bucket (0-3).

    0: night (00-06)
    1: morning (06-12)
    2: afternoon (12-18)
    3: evening (18-24)
    """
    hour = dt.hour
    if 0 <= hour < 6:
        return 0
    elif 6 <= hour < 12:
        return 1
    elif 12 <= hour < 18:
        return 2
    else:
        return 3


def _payload_size_bucket(payload_bytes: int) -> int:
    """Convert payload size to bucket (0-2).

    0: small (<1KB)
    1: mid (1-10KB)
    2: large (>10KB)
    """
    if payload_bytes < 1024:
        return 0
    elif payload_bytes < 10240:
        return 1
    else:
        return 2


def _trust_bucket(trust_score: float) -> int:
    """Convert trust score to bucket (0-3).

    0: low (<30)
    1: mid (30-60)
    2: high (60-85)
    3: super (>=85)
    """
    if trust_score < 30:
        return 0
    elif trust_score < 60:
        return 1
    elif trust_score < 85:
        return 2
    else:
        return 3


def _context_hash(x: np.ndarray) -> str:
    """Compute SHA-256 hash of context vector for deduplication."""
    return hashlib.sha256(x.astype(np.float32).tobytes()).hexdigest()


def _pca_payload_embedding(payload: dict, task_type: str) -> np.ndarray:
    """Embed task payload summary and extract first 8 dims.

    Uses sentence-transformers via embeddings service.
    If PCA not available yet, just truncate to first 8 dims.

    TODO: Train PCA model on historical payloads.
    """
    from circus.services.embeddings import get_embedding_model

    try:
        model = get_embedding_model()
        # Create text summary of payload
        payload_summary = f"{task_type}: {json.dumps(payload)[:200]}"
        embedding = model.encode(payload_summary, normalize_embeddings=True)

        # For now, just take first 8 dims (TODO: proper PCA)
        return embedding[:8].astype(np.float64)
    except Exception as e:
        logger.warning(f"Failed to embed payload, using zeros: {e}")
        return np.zeros(8, dtype=np.float64)


def build_context(
    task_type: str,
    payload: dict,
    requester_agent_id: str,
    deadline: Optional[str],
    db_conn: sqlite3.Connection
) -> np.ndarray:
    """Build 32-dim context vector for routing decision.

    Feature layout (per spec):
    - [0:8]   Task type one-hot (top-8 task types)
    - [8:16]  Task embedding (PCA of payload)
    - [16:20] Requester trust bucket (one-hot 4)
    - [20:24] Time-of-day (one-hot 4)
    - [24]    Task urgency
    - [25:28] Payload size bucket (one-hot 3)
    - [28]    Bias term (1.0)
    - [29:32] Reserved (zeros)
    """
    x = np.zeros(FEATURE_DIM, dtype=np.float64)

    # Task type one-hot (simplified: hash to 0-7 for now, TODO: learn from history)
    task_type_idx = hash(task_type) % 8
    x[task_type_idx] = 1.0

    # Task embedding (8 dims)
    x[8:16] = _pca_payload_embedding(payload, task_type)

    # Requester trust bucket
    cursor = db_conn.cursor()
    cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (requester_agent_id,))
    row = cursor.fetchone()
    trust_score = row[0] if row else 30.0
    trust_idx = 16 + _trust_bucket(trust_score)
    x[trust_idx] = 1.0

    # Time-of-day
    now = datetime.now(timezone.utc)
    tod_idx = 20 + _time_of_day_bucket(now)
    x[tod_idx] = 1.0

    # Task urgency
    if deadline:
        try:
            deadline_dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
            hours_to_deadline = max(0.1, (deadline_dt - now).total_seconds() / 3600)
            urgency = min(1.0, 24.0 / hours_to_deadline)
        except Exception:
            urgency = 0.5
    else:
        urgency = 0.5
    x[24] = urgency

    # Payload size bucket
    payload_bytes = len(json.dumps(payload).encode('utf-8'))
    size_idx = 25 + _payload_size_bucket(payload_bytes)
    x[size_idx] = 1.0

    # Bias term
    x[28] = 1.0

    # Reserved [29:32] stay zero

    return x


def standardize_context(x: np.ndarray, db_conn: sqlite3.Connection) -> np.ndarray:
    """Z-score normalize context vector using running statistics.

    Updates running mean/std in routing_feature_stats table using Welford's algorithm.
    """
    cursor = db_conn.cursor()

    x_norm = x.copy()

    for i in range(FEATURE_DIM):
        # Load current stats
        cursor.execute(
            "SELECT running_mean, running_std, n_samples FROM routing_feature_stats WHERE feature_idx = ?",
            (i,)
        )
        row = cursor.fetchone()

        if row:
            mean, std, n = row
        else:
            # Initialize
            mean, std, n = 0.0, 1.0, 0

        # Update using Welford's online algorithm
        n += 1
        delta = x[i] - mean
        mean += delta / n
        # For std, use simple running estimate (not full Welford M2)
        if n > 1:
            std = np.sqrt(((n - 1) * std ** 2 + delta ** 2) / n)
        else:
            std = 1.0

        # Clamp std to avoid division by zero
        std = max(std, 1e-6)

        # Save updated stats
        cursor.execute("""
            INSERT OR REPLACE INTO routing_feature_stats (feature_idx, running_mean, running_std, n_samples)
            VALUES (?, ?, ?, ?)
        """, (i, mean, std, n))

        # Standardize this feature
        x_norm[i] = (x[i] - mean) / std

    return x_norm


def get_candidate_agents(
    task_type: str,
    min_trust: float,
    exclude_agents: list[str],
    db_conn: sqlite3.Connection
) -> list[tuple[str, ArmState]]:
    """Load candidate agents matching capability + trust + active filters.

    Returns list of (agent_id, arm_state) tuples.
    Loads arm state from DB or creates empty() if new.
    """
    cursor = db_conn.cursor()

    # Build exclusion filter
    exclude_placeholder = ','.join('?' * len(exclude_agents)) if exclude_agents else "''"
    exclude_filter = f"AND id NOT IN ({exclude_placeholder})" if exclude_agents else ""

    # Query agents with matching capability
    # capabilities stored as JSON array: ["summarize", "code-review", ...]
    query = f"""
        SELECT id, trust_score
        FROM agents
        WHERE is_active = 1
          AND trust_score >= ?
          AND json_array_length(capabilities) > 0
          {exclude_filter}
    """
    params = [min_trust] + exclude_agents
    cursor.execute(query, params)

    candidates = []

    for row in cursor.fetchall():
        agent_id = row[0]

        # Check if agent has this task_type in capabilities
        cursor.execute("SELECT capabilities FROM agents WHERE id = ?", (agent_id,))
        caps_row = cursor.fetchone()
        if not caps_row:
            continue

        try:
            caps = json.loads(caps_row[0])
            if task_type not in caps:
                continue
        except (json.JSONDecodeError, TypeError):
            continue

        # Load or create arm state
        cursor.execute("""
            SELECT A_blob, b_blob, n_samples, cumulative_reward
            FROM routing_arms
            WHERE agent_id = ? AND task_type = ?
        """, (agent_id, task_type))

        arm_row = cursor.fetchone()
        if arm_row:
            A_blob, b_blob, n_samples, cum_reward = arm_row
            arm = ArmState.deserialize(A_blob, b_blob, d=FEATURE_DIM, n=n_samples, cum_r=cum_reward)
        else:
            arm = ArmState.empty(FEATURE_DIM)

        candidates.append((agent_id, arm))

    return candidates


def route_task(
    task_type: str,
    payload: dict,
    requester: str,
    deadline: Optional[str],
    min_trust: float,
    exclude_agents: list[str],
    alpha_override: Optional[float],
    db_conn: sqlite3.Connection
) -> dict[str, Any]:
    """Full routing pipeline: build context → pick agent → persist decision.

    Returns dict with:
    - decision_id: UUID
    - agent_id: picked agent
    - score: UCB score
    - ucb: upper confidence bound
    - candidates: number of candidates
    - fallback: "bandit" | "semantic"
    - context_hash: SHA-256 of context
    """
    global _decision_counter

    # Build and standardize context
    x_raw = build_context(task_type, payload, requester, deadline, db_conn)
    x = standardize_context(x_raw, db_conn)

    # Load candidates
    candidates = get_candidate_agents(task_type, min_trust, exclude_agents, db_conn)

    if not candidates:
        raise ValueError(f"No agents found matching task_type={task_type}, min_trust={min_trust}")

    # Determine alpha (exploration weight)
    _decision_counter += 1
    alpha = alpha_override if alpha_override is not None else alpha_schedule(_decision_counter)

    # Check cold start
    cold_start = is_cold_start(candidates, threshold=5)

    if cold_start:
        # Fallback to semantic similarity
        picked_agent_id = _semantic_fallback(task_type, payload, candidates, db_conn)
        fallback = "semantic"
        ucb_score = 0.0
    else:
        # Use bandit
        idx, mean, ucb, all_ucbs = pick(candidates, x, alpha=alpha)
        picked_agent_id = candidates[idx][0]
        fallback = "bandit"
        ucb_score = ucb

    # Persist decision
    decision_id = f"decision-{secrets.token_hex(8)}"
    now = datetime.utcnow().isoformat()
    ctx_hash = _context_hash(x_raw)

    cursor = db_conn.cursor()
    cursor.execute("""
        INSERT INTO routing_decisions (
            id, task_id, picked_agent_id, context_hash, context_blob,
            candidates_considered, ucb_score, fallback, alpha, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        decision_id,
        None,  # task_id filled in after task creation
        picked_agent_id,
        ctx_hash,
        x_raw.astype(np.float32).tobytes(),
        len(candidates),
        ucb_score,
        fallback,
        alpha,
        now
    ))

    return {
        "decision_id": decision_id,
        "agent_id": picked_agent_id,
        "score": ucb_score,
        "ucb": ucb_score,
        "candidates": len(candidates),
        "fallback": fallback,
        "context_hash": ctx_hash
    }


def _semantic_fallback(
    task_type: str,
    payload: dict,
    candidates: list[tuple[str, ArmState]],
    db_conn: sqlite3.Connection
) -> str:
    """Fallback to semantic similarity when cold-starting.

    Use existing embeddings infrastructure to rank candidates by similarity.
    """
    from circus.services.embeddings import get_embedding_model

    try:
        model = get_embedding_model()
        query = f"{task_type}: {json.dumps(payload)[:200]}"
        query_emb = model.encode(query, normalize_embeddings=True)

        # Get embeddings for all candidates
        cursor = db_conn.cursor()
        best_agent = None
        best_score = -1.0

        for agent_id, _ in candidates:
            cursor.execute("SELECT embedding_json FROM agent_embeddings WHERE agent_id = ?", (agent_id,))
            row = cursor.fetchone()
            if row:
                agent_emb = np.array(json.loads(row[0]))
                score = float(np.dot(query_emb, agent_emb))
                if score > best_score:
                    best_score = score
                    best_agent = agent_id

        if best_agent:
            return best_agent
    except Exception as e:
        logger.warning(f"Semantic fallback failed, using random: {e}")

    # Last resort: random
    return candidates[0][0]


def update_reward(
    task_id: str,
    reward: float,
    reason: str,
    db_conn: sqlite3.Connection
) -> None:
    """Update arm with observed reward for a completed task.

    Finds decision row by task_id, loads arm, updates with context+reward, saves back.
    No-op if already rewarded or if task was self-routed (sybil guard).
    """
    cursor = db_conn.cursor()

    # Find decision
    cursor.execute("""
        SELECT id, picked_agent_id, context_blob, reward, fallback
        FROM routing_decisions
        WHERE task_id = ?
    """, (task_id,))
    row = cursor.fetchone()

    if not row:
        logger.debug(f"No routing decision found for task {task_id}, skipping reward update")
        return

    decision_id, agent_id, context_blob, existing_reward, fallback = row

    if existing_reward is not None:
        logger.debug(f"Task {task_id} already rewarded, skipping")
        return

    # Sybil guard: check if task is self-routed
    cursor.execute("SELECT from_agent_id, to_agent_id, task_type FROM tasks WHERE id = ?", (task_id,))
    task_row = cursor.fetchone()
    if not task_row:
        logger.warning(f"Task {task_id} not found, cannot update reward")
        return

    from_agent, to_agent, task_type = task_row
    if from_agent == to_agent:
        logger.warning(f"Self-routed task {task_id} ({from_agent} -> {to_agent}), skipping reward update")
        # Still mark as rewarded to avoid re-processing
        cursor.execute("""
            UPDATE routing_decisions SET reward = ?, reward_reason = ?
            WHERE id = ?
        """, (reward, f"self_routed: {reason}", decision_id))
        return

    # Skip reward update for semantic fallback (no learning from non-bandit decisions)
    if fallback == "semantic":
        cursor.execute("""
            UPDATE routing_decisions SET reward = ?, reward_reason = ?
            WHERE id = ?
        """, (reward, f"semantic_fallback: {reason}", decision_id))
        logger.debug(f"Task {task_id} used semantic fallback, reward recorded but arm not updated")
        return

    # Load context
    context = np.frombuffer(context_blob, dtype=np.float32).astype(np.float64)

    # Load arm
    cursor.execute("""
        SELECT A_blob, b_blob, n_samples, cumulative_reward
        FROM routing_arms
        WHERE agent_id = ? AND task_type = ?
    """, (agent_id, task_type))
    arm_row = cursor.fetchone()

    if arm_row:
        A_blob, b_blob, n_samples, cum_reward = arm_row
        arm = ArmState.deserialize(A_blob, b_blob, d=FEATURE_DIM, n=n_samples, cum_r=cum_reward)
    else:
        # Shouldn't happen, but handle gracefully
        arm = ArmState.empty(FEATURE_DIM)

    # Update arm
    arm.update(context, reward)

    # Save arm
    A_blob_new, b_blob_new = arm.serialize()
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT OR REPLACE INTO routing_arms (
            agent_id, task_type, A_blob, b_blob, n_samples, cumulative_reward, last_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (agent_id, task_type, A_blob_new, b_blob_new, arm.n_samples, arm.cumulative_reward, now))

    # Mark decision as rewarded
    cursor.execute("""
        UPDATE routing_decisions SET reward = ?, reward_reason = ?
        WHERE id = ?
    """, (reward, reason, decision_id))

    logger.info(f"Updated arm ({agent_id}, {task_type}) with reward {reward:.3f} for task {task_id}")


def compute_default_reward(
    task_state: str,
    output_schema_valid: bool,
    deadline: Optional[str],
    completed_at: Optional[str]
) -> tuple[float, str]:
    """Compute default reward from task outcome.

    Per spec reward table:
    - COMPLETED + schema valid: +1.0
    - COMPLETED + no schema: +0.8
    - COMPLETED + schema failed: +0.4
    - FAILED: 0.0
    - EXPIRED/TIMED_OUT: 0.0
    - CANCELLED by requester: 0.5
    - CANCELLED by assignee: 0.0

    Plus latency bonus: +0.1 * (1 - actual/deadline), clamped [0, 0.1].
    """
    reward = 0.0
    reason = task_state

    if task_state == "completed":
        if output_schema_valid:
            reward = 1.0
            reason = "completed_schema_valid"
        elif output_schema_valid is None:
            reward = 0.8
            reason = "completed_no_schema"
        else:
            reward = 0.4
            reason = "completed_schema_invalid"

        # Latency bonus
        if deadline and completed_at:
            try:
                deadline_dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
                completed_dt = datetime.fromisoformat(completed_at.replace('Z', '+00:00'))
                # Assume task was created near deadline_dt - 24h (simplified)
                # In reality, should use task.created_at, but this is good enough
                actual_seconds = (completed_dt - deadline_dt).total_seconds()
                deadline_seconds = 86400.0  # assume 24h deadline window
                if actual_seconds > 0:
                    bonus = 0.1 * max(0, 1 - actual_seconds / deadline_seconds)
                    reward += min(0.1, bonus)
                    reason += f"_latency_bonus_{bonus:.3f}"
            except Exception:
                pass

    elif task_state == "failed":
        reward = 0.0
        reason = "failed"

    elif task_state in ("expired", "timed_out"):
        reward = 0.0
        reason = task_state

    elif task_state == "canceled":
        # TODO: distinguish requester vs assignee cancellation
        # For now, assume neutral
        reward = 0.5
        reason = "canceled_neutral"

    # Clamp to [0, 1]
    reward = max(0.0, min(1.0, reward))

    return reward, reason


def is_terminal_state(state: str) -> bool:
    """Check if task state is terminal (no further transitions expected)."""
    return state in ("completed", "failed", "canceled", "expired", "timed_out")
