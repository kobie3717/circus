"""Agent experience sharing routes — narrative memory layer for the bot mesh."""
import json
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from circus.config import settings
from circus.routes.agents import verify_token

router = APIRouter()


def get_db():
    return sqlite3.connect(str(settings.database_path))


class LogExperienceRequest(BaseModel):
    environment: str          # "hydra-note", "whatsauction-backend", "circus"
    task_type: str            # "debug", "code-review", "deployment", "analyze"
    what_worked: Optional[str] = None
    what_failed: Optional[str] = None
    context_snapshot: Optional[dict] = None
    outcome: float = 0.5      # 0.0-1.0
    confidence: float = 0.5


class ConfirmExperienceRequest(BaseModel):
    confirming_agent_id: str  # Ignored - kept for backwards compatibility


class BatchLogRequest(BaseModel):
    experiences: list[LogExperienceRequest]  # max 10


def _log_single_experience(cursor, agent_id: str, req: LogExperienceRequest) -> dict:
    """Shared logic for logging a single experience (used by /log and /batch)."""
    # Check for existing similar experience (merge duplicates)
    cursor.execute("""
        SELECT id, observations, confidence, confirmed_by
        FROM agent_experiences
        WHERE agent_id=? AND environment=? AND task_type=? AND what_worked=?
    """, (agent_id, req.environment, req.task_type, req.what_worked))
    existing = cursor.fetchone()

    if existing:
        exp_id, obs, conf, confirmed_by_json = existing
        # Update: increment observations, Bayesian update confidence
        new_obs = obs + 1
        new_conf = min(0.99, conf + (req.outcome - conf) / new_obs)
        cursor.execute("""
            UPDATE agent_experiences
            SET observations=?, confidence=?, outcome=?, updated_at=datetime('now')
            WHERE id=?
        """, (new_obs, new_conf, req.outcome, exp_id))
        return {"id": exp_id, "merged": True, "observations": new_obs, "confidence": round(new_conf, 3)}

    exp_id = str(uuid.uuid4())
    cursor.execute("""
        INSERT INTO agent_experiences
            (id, agent_id, environment, task_type, what_worked, what_failed,
             context_snapshot, outcome, confidence, observations, confirmed_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '[]')
    """, (
        exp_id, agent_id, req.environment, req.task_type,
        req.what_worked, req.what_failed,
        json.dumps(req.context_snapshot or {}),
        req.outcome, req.confidence
    ))
    return {"id": exp_id, "merged": False, "observations": 1, "confidence": req.confidence}


@router.post("/log")
async def log_experience(req: LogExperienceRequest, agent_id: str = Depends(verify_token)):
    """Log an experience after completing a task."""
    db = get_db()
    try:
        cursor = db.cursor()
        result = _log_single_experience(cursor, agent_id, req)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/batch")
async def log_experience_batch(req: BatchLogRequest, agent_id: str = Depends(verify_token)):
    """Batch log experiences (up to 10). Returns accepted/rejected counts."""
    if len(req.experiences) > 10:
        raise HTTPException(status_code=400, detail="Max 10 experiences per batch")

    results = {"accepted": 0, "rejected": 0, "ids": [], "reasons": []}
    db = get_db()
    try:
        cursor = db.cursor()
        for exp in req.experiences:
            try:
                result = _log_single_experience(cursor, agent_id, exp)
                results["accepted"] += 1
                results["ids"].append(result["id"])
            except Exception as e:
                results["rejected"] += 1
                results["reasons"].append(str(e))
        db.commit()
        return results
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/query")
async def query_experiences(
    environment: Optional[str] = Query(None),
    task_type: Optional[str] = Query(None),
    min_confidence: float = Query(0.5),
    limit: int = Query(5),
    agent_id: str = Depends(verify_token)
):
    """Query experiences for a given environment. Used by bots before starting a task."""
    db = get_db()
    try:
        cursor = db.cursor()

        # Build WHERE clause dynamically based on provided filters
        conditions = ["e.confidence >= ?"]
        params = [min_confidence]

        if environment:
            conditions.append("e.environment=?")
            params.append(environment)

        if task_type:
            conditions.append("e.task_type=?")
            params.append(task_type)

        where_clause = " AND ".join(conditions)
        params.append(limit)

        cursor.execute(f"""
            SELECT e.id, e.agent_id, e.environment, e.task_type,
                   e.what_worked, e.what_failed, e.outcome, e.confidence,
                   e.observations, e.confirmed_by, e.created_at,
                   a.name as agent_name, a.trust_score
            FROM agent_experiences e
            JOIN agents a ON e.agent_id = a.id
            WHERE {where_clause}
            ORDER BY (e.confidence * (a.trust_score / 100.0)) DESC
            LIMIT ?
        """, params)
        rows = cursor.fetchall()
        experiences = []
        for row in rows:
            exp_id, agent_id, env, task_type_r, worked, failed, outcome, conf, obs, confirmed_json, created_at, agent_name, trust_score = row
            # Weight confidence by agent trust score
            weighted_conf = round(conf * (trust_score / 100.0), 3)
            experiences.append({
                "id": exp_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "agent_trust_score": trust_score,
                "environment": env,
                "task_type": task_type_r,
                "what_worked": worked,
                "what_failed": failed,
                "outcome": outcome,
                "confidence": conf,
                "weighted_confidence": weighted_conf,
                "observations": obs,
                "confirmed_by": json.loads(confirmed_json or "[]"),
                "created_at": created_at,
            })
        return {
            "environment": environment,
            "task_type": task_type,
            "experiences": experiences,
            "count": len(experiences),
            "queried_at": datetime.utcnow().isoformat() + "Z"
        }
    finally:
        db.close()


@router.post("/{experience_id}/confirm")
async def confirm_experience(
    experience_id: str,
    req: ConfirmExperienceRequest,
    agent_id: str = Depends(verify_token)
):
    """Confirm another agent's experience (peer validation raises confidence)."""
    db = get_db()
    try:
        cursor = db.cursor()
        cursor.execute("SELECT agent_id, confidence, observations, confirmed_by FROM agent_experiences WHERE id=?", (experience_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Experience not found")
        experience_agent_id, conf, obs, confirmed_json = row

        # Security: prevent agents from confirming their own experiences
        if agent_id == experience_agent_id:
            raise HTTPException(status_code=403, detail="Cannot confirm your own experience")

        confirmed_by = json.loads(confirmed_json or "[]")

        # Security: limit confirmations to prevent unbounded confidence inflation
        if len(confirmed_by) >= 10:
            raise HTTPException(status_code=400, detail="Maximum confirmations reached")

        # Use agent_id from token, not from request body
        if agent_id in confirmed_by:
            return {"message": "Already confirmed by this agent", "confidence": conf}
        confirmed_by.append(agent_id)
        # Each peer confirmation boosts confidence (Bayesian-style)
        new_conf = min(0.99, conf + (1.0 - conf) * 0.15)
        cursor.execute("""
            UPDATE agent_experiences
            SET confirmed_by=?, confidence=?, observations=observations+1, updated_at=datetime('now')
            WHERE id=?
        """, (json.dumps(confirmed_by), new_conf, experience_id))
        db.commit()
        return {"confirmed": True, "confidence": round(new_conf, 3), "confirmed_by_count": len(confirmed_by)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/agent/{agent_id}")
async def get_agent_experiences(agent_id: str, limit: int = Query(20)):
    """Get all experiences logged by a specific agent (TRQP endpoint)."""
    db = get_db()
    try:
        cursor = db.cursor()
        cursor.execute("""
            SELECT id, environment, task_type, what_worked, what_failed,
                   outcome, confidence, observations, confirmed_by, created_at
            FROM agent_experiences
            WHERE agent_id=?
            ORDER BY confidence DESC, observations DESC
            LIMIT ?
        """, (agent_id, limit))
        rows = cursor.fetchall()
        return {
            "agent_id": agent_id,
            "experiences": [
                {
                    "id": r[0], "environment": r[1], "task_type": r[2],
                    "what_worked": r[3], "what_failed": r[4], "outcome": r[5],
                    "confidence": r[6], "observations": r[7],
                    "confirmed_by": json.loads(r[8] or "[]"), "created_at": r[9]
                }
                for r in rows
            ],
            "count": len(rows)
        }
    finally:
        db.close()


@router.get("/synthesis")
async def get_synthesis_experiences(
    task_type: Optional[str] = Query(None),
    environment: Optional[str] = Query(None),
    limit: int = Query(10),
    agent_id: str = Depends(verify_token)
):
    """Query only meta-synthesized experiences."""
    db = get_db()
    try:
        cursor = db.cursor()
        where = ["e.is_synthesis = 1"]
        params = []
        if task_type:
            where.append("e.task_type = ?")
            params.append(task_type)
        if environment:
            where.append("e.environment = ?")
            params.append(environment)
        params.append(limit)

        cursor.execute(f"""
            SELECT e.id, e.environment, e.task_type, e.what_worked,
                   e.outcome, e.confidence, e.observations, e.synthesis_period,
                   e.source_experience_ids, e.created_at, a.name as agent_name
            FROM agent_experiences e
            JOIN agents a ON a.id = e.agent_id
            WHERE {' AND '.join(where)}
            ORDER BY e.confidence DESC
            LIMIT ?
        """, params)
        rows = cursor.fetchall()
        syntheses = []
        for row in rows:
            syntheses.append({
                "id": row[0],
                "environment": row[1],
                "task_type": row[2],
                "what_worked": row[3],
                "outcome": row[4],
                "confidence": row[5],
                "observations": row[6],
                "synthesis_period": row[7],
                "source_experience_ids": json.loads(row[8] or "[]"),
                "created_at": row[9],
                "agent_name": row[10]
            })
        return {"syntheses": syntheses, "count": len(syntheses)}
    finally:
        db.close()


@router.post("/synthesize")
async def trigger_synthesis(
    lookback_days: int = Query(7),
    agent_id: str = Depends(verify_token)
):
    """Manually trigger synthesis run (admin use)."""
    try:
        from circus.synthesis_worker import run_synthesis
        stats = run_synthesis(lookback_days)
        return {"status": "ok", "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
