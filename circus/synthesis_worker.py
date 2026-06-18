"""
Reflective Experience Synthesis Worker — Circus AI commons.
Clusters bot experiences → synthesizes meta-experiences → writes back.
Runs nightly or on-demand.
"""
import json
import sqlite3
import os
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

CIRCUS_DB = os.environ.get('CIRCUS_DB_PATH', '/root/.circus/circus.db')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
SYNTHESIS_AGENT_NAME = 'circus-synthesis'
MIN_CLUSTER_SIZE = 3
LOOKBACK_DAYS = 7


@contextmanager
def get_db():
    """Get database connection (context manager for automatic cleanup)."""
    conn = sqlite3.connect(CIRCUS_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def ensure_synthesis_agent() -> str:
    """Get or create the synthesis agent ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM agents WHERE name = ?", (SYNTHESIS_AGENT_NAME,))
        row = cursor.fetchone()
        if row:
            return row['id']

        # Create synthesis agent
        agent_id = f"synth-{os.urandom(3).hex()}"
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash,
                               trust_score, trust_tier, registered_at, last_seen, is_active)
            VALUES (?, ?, 'synthesizer', '[]', 'circus-commons', ?, ?,
                    85.0, 'Trusted', ?, ?, 1)
        """, (agent_id, SYNTHESIS_AGENT_NAME, f"hash-{agent_id}", f"token-{agent_id}", now, now))
        conn.commit()
        return agent_id


def fetch_recent_experiences(days: int = LOOKBACK_DAYS):
    """Fetch non-synthesis experiences from last N days."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT e.id, e.agent_id, a.name as agent_name, e.environment, e.task_type,
                   e.what_worked, e.what_failed, e.outcome, e.confidence, e.observations,
                   e.created_at
            FROM agent_experiences e
            JOIN agents a ON a.id = e.agent_id
            WHERE e.created_at > datetime('now', ?)
              AND (e.is_synthesis IS NULL OR e.is_synthesis = 0)
              AND (e.what_worked IS NOT NULL OR e.what_failed IS NOT NULL)
            ORDER BY e.task_type, e.environment, e.created_at DESC
        """, (f'-{days} days',))
        return [dict(r) for r in cursor.fetchall()]


def cluster_experiences(experiences):
    """Group by (task_type, environment). Returns dict of key -> list of experiences."""
    clusters = {}
    for exp in experiences:
        key = (exp['task_type'] or 'general', exp['environment'] or 'general')
        clusters.setdefault(key, []).append(exp)
    return {k: v for k, v in clusters.items() if len(v) >= MIN_CLUSTER_SIZE}


def synthesize_cluster(task_type: str, environment: str, experiences: list) -> Optional[str]:
    """Call Claude Haiku to synthesize a cluster into a meta-experience."""
    if not ANTHROPIC_API_KEY:
        # Fallback: rule-based synthesis without LLM
        worked = [e['what_worked'] for e in experiences if e.get('what_worked')]
        failed = [e['what_failed'] for e in experiences if e.get('what_failed')]
        outcomes = [e['outcome'] for e in experiences if e.get('outcome') is not None]
        avg_outcome = sum(outcomes) / len(outcomes) if outcomes else 0.5

        parts = []
        if worked:
            parts.append(f"What worked ({len(worked)}/{len(experiences)} times): {worked[0][:100]}")
        if failed:
            parts.append(f"What to avoid: {failed[0][:100]}")
        parts.append(f"Average success rate: {avg_outcome:.0%}")
        return '. '.join(parts) if parts else None

    # Use Claude Haiku API for synthesis
    try:
        import anthropic
    except ImportError:
        print("[synthesis] anthropic library not installed, using fallback")
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Format evidence
    evidence_lines = []
    for e in experiences[:10]:  # cap at 10 to keep prompt small
        parts = []
        if e.get('what_worked'):
            parts.append(f"✓ {e['what_worked'][:100]}")
        if e.get('what_failed'):
            parts.append(f"✗ {e['what_failed'][:100]}")
        outcome = e.get('outcome', 0.5)
        agent = e.get('agent_name', 'unknown')
        evidence_lines.append(f"- [{agent}, outcome={outcome:.0%}] {' | '.join(parts)}")

    evidence = '\n'.join(evidence_lines)

    prompt = f"""Synthesize these {len(experiences)} agent experiences for task_type="{task_type}" in environment="{environment}" into ONE concise meta-experience.

Evidence:
{evidence}

Write a single paragraph (2-3 sentences max) that captures:
1. What approach works most reliably (cite success rate if clear)
2. What to avoid (if pattern exists)

Be specific and actionable. No preamble. Write directly."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"[synthesis] Haiku API failed: {e}")
        return None


def already_synthesized_recently(task_type: str, environment: str, days: int = 1) -> bool:
    """Avoid re-synthesizing same cluster within N days."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM agent_experiences
            WHERE is_synthesis = 1
              AND task_type = ? AND environment = ?
              AND created_at > datetime('now', ?)
        """, (task_type, environment, f'-{days} days'))
        return cursor.fetchone()[0] > 0


def write_meta_experience(synthesis_agent_id: str, task_type: str, environment: str,
                           synthesis: str, source_ids: list, avg_outcome: float, avg_confidence: float):
    """Write the synthesized meta-experience to DB."""
    exp_id = f"synth-{hashlib.md5(f'{task_type}{environment}{synthesis[:50]}'.encode()).hexdigest()[:12]}"
    start_date = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    end_date = datetime.utcnow().strftime('%Y-%m-%d')
    period = f"{start_date}/{end_date}"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO agent_experiences
            (id, agent_id, environment, task_type, what_worked, outcome, confidence,
             observations, confirmed_by, is_synthesis, source_experience_ids, synthesis_period, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', 1, ?, ?, datetime('now'))
        """, (
            exp_id, synthesis_agent_id, environment, task_type,
            synthesis, round(avg_outcome, 3), round(min(avg_confidence * 0.9, 0.95), 3),
            len(source_ids), json.dumps(source_ids), period
        ))
        conn.commit()
        return exp_id


def run_synthesis(lookback_days: int = LOOKBACK_DAYS) -> dict:
    """Main synthesis run. Returns stats."""
    stats = {'clusters_found': 0, 'synthesized': 0, 'skipped': 0, 'errors': 0}

    synthesis_agent_id = ensure_synthesis_agent()
    experiences = fetch_recent_experiences(lookback_days)

    if not experiences:
        print("[synthesis] No experiences to synthesize")
        return stats

    clusters = cluster_experiences(experiences)
    stats['clusters_found'] = len(clusters)
    print(f"[synthesis] Found {len(clusters)} clusters from {len(experiences)} experiences")

    for (task_type, environment), cluster_exps in clusters.items():
        if already_synthesized_recently(task_type, environment):
            print(f"[synthesis] Skip {task_type}/{environment} — synthesized recently")
            stats['skipped'] += 1
            continue

        synthesis = synthesize_cluster(task_type, environment, cluster_exps)
        if not synthesis:
            stats['errors'] += 1
            continue

        avg_outcome = sum(e.get('outcome', 0.5) for e in cluster_exps) / len(cluster_exps)
        avg_confidence = sum(e.get('confidence', 0.7) for e in cluster_exps) / len(cluster_exps)
        source_ids = [e['id'] for e in cluster_exps]

        exp_id = write_meta_experience(
            synthesis_agent_id, task_type, environment,
            synthesis, source_ids, avg_outcome, avg_confidence
        )
        print(f"[synthesis] ✅ {task_type}/{environment} → {exp_id}: {synthesis[:80]}...")
        stats['synthesized'] += 1

    return stats


if __name__ == '__main__':
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else LOOKBACK_DAYS
    result = run_synthesis(days)
    print(f"\n[synthesis] Done: {result}")
