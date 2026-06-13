"""
Memory Commons v2 — atomic claims, triage, 4-channel recall, consolidation.
Implements Paper 1 (Universal Memory Architecture).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import uuid
from datetime import datetime
import sqlite3
import json

from circus.database import get_db
from circus.memory_engine import (
    triage_importance, extract_atomic_claims, check_near_duplicate,
    reinforce_claim, contradict_claim, run_consolidation, recall_claims,
    generate_embedding
)

router = APIRouter(prefix="/api/v1/memory", tags=["memory-v2"])


class PublishClaimRequest(BaseModel):
    agent_id: str
    content: str          # raw content — engine extracts atomic claims
    claim_type: str = "semantic"   # episodic | semantic | procedural | identity
    subject: Optional[str] = None  # override auto-detected subject
    namespace_id: Optional[str] = None  # defaults to agent's vouch_chain namespace


class ContradictRequest(BaseModel):
    agent_id: str
    contradicting_claim_id: str   # the NEW claim being asserted
    old_claim_id: str             # the existing claim being contradicted
    evidence: Optional[str] = None


class RecallRequest(BaseModel):
    query: str
    namespace_id: Optional[str] = None
    limit: int = 10
    min_confidence: float = 0.25


@router.post("/claims/publish")
def publish_claims(req: PublishClaimRequest):
    """
    Publish content as atomic memory claims.
    Triage filters low-importance content.
    Near-duplicates are reinforced, not duplicated.
    Returns list of created/reinforced claim IDs.
    """
    with get_db() as conn:
        # Resolve namespace
        if req.namespace_id:
            namespace_id = req.namespace_id
        else:
            # Use agent's ancestry_chain root or agent_id as namespace
            agent = conn.execute(
                "SELECT sponsor_id, vouch_depth FROM agents WHERE id = ?",
                (req.agent_id,)
            ).fetchone()
            namespace_id = agent[0] if agent and agent[0] else req.agent_id

        claims = extract_atomic_claims(req.content, req.agent_id, namespace_id, req.claim_type)

        if not claims:
            return {"published": 0, "claim_ids": [], "reason": "below_importance_floor"}

        created = []
        reinforced = []

        for claim in claims:
            # Override subject if provided
            if req.subject:
                claim['subject'] = req.subject

            # Check near-duplicate
            dup_id = check_near_duplicate(conn, claim['claim_text'], namespace_id)
            if dup_id:
                reinforce_claim(conn, dup_id)
                reinforced.append(dup_id)
                continue

            # Generate embedding for claim
            embedding = generate_embedding(claim['claim_text'])
            embedding_json = json.dumps(embedding) if embedding else None

            # Insert new claim
            conn.execute(
                """INSERT INTO memory_claims
                   (id, namespace_id, agent_id, claim_text, subject, claim_type,
                    importance, confidence, status, source, episode_id,
                    created_at, last_accessed, access_count, decay_rate, embedding)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    claim['id'], claim['namespace_id'], claim['agent_id'],
                    claim['claim_text'], claim['subject'], claim['claim_type'],
                    claim['importance'], claim['confidence'], claim['status'],
                    claim['source'], claim['episode_id'],
                    claim['created_at'], claim['last_accessed'],
                    claim['access_count'], claim['decay_rate'], embedding_json,
                )
            )
            conn.commit()
            created.append(claim['id'])

        return {
            "published": len(created),
            "reinforced": len(reinforced),
            "claim_ids": created,
            "reinforced_ids": reinforced,
        }


@router.post("/claims/contradict")
def contradict(req: ContradictRequest):
    """
    Assert that a new claim contradicts an existing one.
    Hysteresis: high-confidence old claims require importance > 0.5 in new claim.
    """
    with get_db() as conn:
        # Verify both claims exist
        old = conn.execute("SELECT id, confidence, status FROM memory_claims WHERE id = ?", (req.old_claim_id,)).fetchone()
        new = conn.execute("SELECT id, status FROM memory_claims WHERE id = ?", (req.contradicting_claim_id,)).fetchone()

        if not old:
            raise HTTPException(404, f"Claim {req.old_claim_id} not found")
        if not new:
            raise HTTPException(404, f"Claim {req.contradicting_claim_id} not found")

        # Log contradiction
        conn.execute(
            """INSERT INTO memory_contradictions
               (claim_id, contradicting_claim_id, contradicting_agent_id, evidence, status, created_at)
               VALUES (?,?,?,?,?,?)""",
            (req.old_claim_id, req.contradicting_claim_id, req.agent_id,
             req.evidence, 'pending', datetime.utcnow().isoformat())
        )
        conn.commit()

        # Attempt supersede with hysteresis
        contradict_claim(conn, req.old_claim_id, req.contradicting_claim_id)

        # Check updated status
        updated = conn.execute("SELECT status FROM memory_claims WHERE id = ?", (req.old_claim_id,)).fetchone()

        return {
            "old_claim_id": req.old_claim_id,
            "new_claim_id": req.contradicting_claim_id,
            "old_status": updated[0] if updated else "unknown",
            "action": "superseded" if updated and updated[0] == "superseded" else "pending_confirmation",
        }


@router.post("/recall")
def recall(req: RecallRequest):
    """
    4-channel hybrid recall: lexical(BM25/FTS) + subject-entity + temporal.
    Returns ranked claims with scores.
    """
    with get_db() as conn:
        results = recall_claims(
            conn,
            req.query,
            namespace_id=req.namespace_id,
            limit=req.limit,
            min_confidence=req.min_confidence,
        )

        # Reinforce retrieved claims (use-as-evidence strengthens them)
        for r in results:
            reinforce_claim(conn, r['id'])

        return {"query": req.query, "results": results, "count": len(results)}


@router.post("/consolidate")
def consolidate():
    """
    Run nightly consolidation pass manually.
    Promotes candidates, archives low-score claims, episodic->semantic.
    In production: call from cron/heartbeat.
    """
    with get_db() as conn:
        stats = run_consolidation(conn)
        return {"consolidation": stats, "timestamp": datetime.utcnow().isoformat()}


@router.get("/claims/{claim_id}")
def get_claim(claim_id: str):
    """Get a specific memory claim with full provenance."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, namespace_id, agent_id, claim_text, subject, claim_type,
                      importance, confidence, status, source, episode_id,
                      created_at, last_accessed, access_count, superseded_by, decay_rate
               FROM memory_claims WHERE id = ?""",
            (claim_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Claim not found")

        cols = ['id','namespace_id','agent_id','claim_text','subject','claim_type',
                'importance','confidence','status','source','episode_id',
                'created_at','last_accessed','access_count','superseded_by','decay_rate']
        claim = dict(zip(cols, row))

        # Fetch contradiction history
        contradictions = conn.execute(
            "SELECT * FROM memory_contradictions WHERE claim_id = ?",
            (claim_id,)
        ).fetchall()
        claim['contradictions'] = [dict(zip(['id','claim_id','contradicting_claim_id','contradicting_agent_id','evidence','status','created_at','resolved_at'], c)) for c in contradictions]

        # Reinforce on access
        reinforce_claim(conn, claim_id)

        return claim


@router.get("/claims")
def list_claims(
    namespace_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    claim_type: Optional[str] = None,
    status: str = "active",
    limit: int = 50,
):
    """List memory claims with filters."""
    with get_db() as conn:
        query = "SELECT id, claim_text, subject, claim_type, importance, confidence, status, agent_id, namespace_id, created_at FROM memory_claims WHERE status = ?"
        params = [status]

        if namespace_id:
            query += " AND namespace_id = ?"
            params.append(namespace_id)
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if claim_type:
            query += " AND claim_type = ?"
            params.append(claim_type)

        query += " ORDER BY importance DESC, confidence DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        cols = ['id','claim_text','subject','claim_type','importance','confidence','status','agent_id','namespace_id','created_at']
        return {"claims": [dict(zip(cols, r)) for r in rows], "count": len(rows)}
