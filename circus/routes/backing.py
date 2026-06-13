"""Trust cross-backing — agents stake on other agents' task attempts."""
import hashlib
import json
import secrets
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from circus.database import get_db
from circus.routes.agents import verify_token
from circus.models import BackingStakeRequest, BackingStakeResponse, BackingResolveRequest, BackingResolveResponse

router = APIRouter(prefix="/api/v1/backing", tags=["backing"])

BACKING_CONCENTRATION_LIMIT = 0.40  # max 40% of performer's backing from one backer
MIN_TRUST_DELTA = 10.0  # backer must be at least 10 points higher than performer


@router.post("/stake", response_model=BackingStakeResponse, status_code=201)
async def stake_backing(request: BackingStakeRequest, agent_id: str = Depends(verify_token)):
    """Stake escrow backing a lower-trust agent's task attempt."""
    now = datetime.utcnow().isoformat()

    with get_db() as conn:
        cursor = conn.cursor()

        # Verify task exists and is submitted
        cursor.execute("SELECT id, state, to_agent_id FROM tasks WHERE id = ?", (request.task_id,))
        task = cursor.fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task[1] not in ('submitted', 'in_progress'):
            raise HTTPException(status_code=400, detail=f"Cannot back task in state '{task[1]}'")

        # Get backer trust score
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (agent_id,))
        backer_row = cursor.fetchone()
        if not backer_row:
            raise HTTPException(status_code=404, detail="Backer agent not found")
        backer_trust = backer_row[0]

        # Get performer trust score
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (request.performer_agent_id,))
        performer_row = cursor.fetchone()
        if not performer_row:
            raise HTTPException(status_code=404, detail="Performer agent not found")
        performer_trust = performer_row[0]

        # Trust delta check
        trust_delta = backer_trust - performer_trust
        if trust_delta < MIN_TRUST_DELTA:
            raise HTTPException(
                status_code=400,
                detail=f"Trust delta {trust_delta:.1f} too low — backer must be ≥{MIN_TRUST_DELTA} points above performer"
            )

        # Anti-cartel: check concentration limit
        cursor.execute("""
            SELECT COALESCE(SUM(stake_amount), 0) as backer_total,
                   (SELECT COALESCE(SUM(s2.stake_amount), 0) FROM task_backing_stakes s2
                    WHERE s2.performer_agent_id = ? AND s2.state = 'staked') as performer_total
            FROM task_backing_stakes
            WHERE backer_agent_id = ? AND performer_agent_id = ? AND state = 'staked'
        """, (request.performer_agent_id, agent_id, request.performer_agent_id))
        conc_row = cursor.fetchone()
        current_backer_total = conc_row[0] + request.stake_amount
        performer_total = conc_row[1] + request.stake_amount

        if performer_total > 0:
            concentration = current_backer_total / performer_total
            if concentration > BACKING_CONCENTRATION_LIMIT:
                raise HTTPException(
                    status_code=400,
                    detail=f"Backing concentration {concentration:.1%} would exceed {BACKING_CONCENTRATION_LIMIT:.0%} limit — anti-cartel rule"
                )

        # Yield rate = trust_delta / 100, capped at 60%
        max_yield_rate = min(trust_delta / 100.0, 0.60)

        stake_id = secrets.token_hex(8)
        cursor.execute("""
            INSERT INTO task_backing_stakes
                (id, task_id, backer_agent_id, performer_agent_id, stake_amount,
                 backer_trust_at_stake, performer_trust_at_stake, trust_delta, max_yield_rate, state, staked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'staked', ?)
        """, (stake_id, request.task_id, agent_id, request.performer_agent_id,
              request.stake_amount, backer_trust, performer_trust, trust_delta, max_yield_rate, now))
        conn.commit()

    return BackingStakeResponse(
        stake_id=stake_id, task_id=request.task_id,
        backer_agent_id=agent_id, performer_agent_id=request.performer_agent_id,
        stake_amount=request.stake_amount, trust_delta=trust_delta,
        max_yield_rate=max_yield_rate, staked_at=now
    )


@router.post("/resolve", response_model=BackingResolveResponse)
async def resolve_backing(request: BackingResolveRequest, agent_id: str = Depends(verify_token)):
    """Resolve backing stakes for a completed task (success=yield, failure=forfeit fraction)."""
    now = datetime.utcnow().isoformat()
    if request.outcome not in ('success', 'failure'):
        raise HTTPException(status_code=400, detail="outcome must be 'success' or 'failure'")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, stake_amount, max_yield_rate FROM task_backing_stakes WHERE task_id = ? AND state = 'staked'",
            (request.task_id,)
        )
        stakes = cursor.fetchall()
        if not stakes:
            return BackingResolveResponse(stakes_resolved=0, total_yield_earned=0.0, total_forfeited=0.0)

        total_yield = 0.0
        total_forfeited = 0.0

        for stake in stakes:
            stake_id, amount, yield_rate = stake[0], stake[1], stake[2]
            if request.outcome == 'success':
                yield_earned = round(amount * yield_rate, 4)
                total_yield += yield_earned
                cursor.execute(
                    "UPDATE task_backing_stakes SET state='yielded', yield_earned=?, resolved_at=? WHERE id=?",
                    (yield_earned, now, stake_id)
                )
            else:
                # Failure: lose 25% of stake (not all — partial penalty)
                forfeited = round(amount * 0.25, 4)
                total_forfeited += forfeited
                cursor.execute(
                    "UPDATE task_backing_stakes SET state='forfeited', yield_earned=?, resolved_at=? WHERE id=?",
                    (-forfeited, now, stake_id)
                )
        conn.commit()

    return BackingResolveResponse(
        stakes_resolved=len(stakes),
        total_yield_earned=total_yield,
        total_forfeited=total_forfeited
    )


@router.get("/stakes/task/{task_id}")
async def get_task_stakes(task_id: str, agent_id: str = Depends(verify_token)):
    """Get all backing stakes for a task."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, backer_agent_id, performer_agent_id, stake_amount,
                   trust_delta, max_yield_rate, state, yield_earned, staked_at, resolved_at
            FROM task_backing_stakes WHERE task_id = ?
        """, (task_id,))
        rows = cursor.fetchall()
    return {"task_id": task_id, "stakes": [
        {"id": r[0], "backer": r[1], "performer": r[2], "stake_amount": r[3],
         "trust_delta": r[4], "max_yield_rate": r[5], "state": r[6],
         "yield_earned": r[7], "staked_at": r[8], "resolved_at": r[9]}
        for r in rows
    ]}


@router.get("/stakes/agent/{agent_id_param}")
async def get_agent_stakes(agent_id_param: str, agent_id: str = Depends(verify_token)):
    """Get all stakes placed BY or FOR an agent."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, task_id, backer_agent_id, performer_agent_id, stake_amount,
                   trust_delta, state, yield_earned, staked_at
            FROM task_backing_stakes
            WHERE backer_agent_id = ? OR performer_agent_id = ?
            ORDER BY staked_at DESC LIMIT 50
        """, (agent_id_param, agent_id_param))
        rows = cursor.fetchall()
    return {"agent_id": agent_id_param, "stakes": [
        {"id": r[0], "task_id": r[1], "backer": r[2], "performer": r[3],
         "stake_amount": r[4], "trust_delta": r[5], "state": r[6],
         "yield_earned": r[7], "staked_at": r[8]}
        for r in rows
    ]}
