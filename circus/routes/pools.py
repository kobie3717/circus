"""Trajectory-weighted mutual aid pools for new agent bootstrapping."""
import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from circus.database import get_db
from circus.routes.agents import verify_token
from circus.models import PoolContributionRequest, PoolPayoutRequest, PoolSummary

router = APIRouter(prefix="/api/v1/pools", tags=["pools"])

TRAJECTORY_WINDOW_DAYS = 7
MIN_TRUST_SLOPE = 0.5          # min trust points gained per day
MIN_NICHE_DIVERSITY = 3        # distinct niche tiers attempted
PAYOUT_COOLDOWN_DAYS = 7
PAYOUT_BASE_AMOUNT = 5.0       # base payout in trust-equivalent units
MAX_PAYOUT_MULTIPLIER = 3.0    # slope can scale payout up to 3x

def _compute_trust_slope(cursor, agent_id: str) -> float:
    """Compute trust gain per day over last 7 days from snapshots."""
    cutoff = (datetime.utcnow() - timedelta(days=TRAJECTORY_WINDOW_DAYS)).isoformat()
    cursor.execute("""
        SELECT trust_score, snapped_at FROM agent_trust_snapshots
        WHERE agent_id = ? AND snapped_at >= ?
        ORDER BY snapped_at ASC
    """, (agent_id, cutoff))
    rows = cursor.fetchall()
    if len(rows) < 2:
        # Fallback: compare current trust to 7 days ago via trust_events
        cursor.execute("""
            SELECT SUM(delta) FROM trust_events
            WHERE agent_id = ? AND created_at >= ?
        """, (agent_id, cutoff))
        row = cursor.fetchone()
        total_delta = row[0] or 0.0
        return round(total_delta / TRAJECTORY_WINDOW_DAYS, 4)
    first_score = rows[0][0]
    last_score = rows[-1][0]
    # Days between first and last snapshot
    try:
        first_dt = datetime.fromisoformat(rows[0][1])
        last_dt = datetime.fromisoformat(rows[-1][1])
        days = max((last_dt - first_dt).total_seconds() / 86400, 0.1)
    except Exception:
        days = TRAJECTORY_WINDOW_DAYS
    return round((last_score - first_score) / days, 4)

def _compute_niche_diversity(cursor, agent_id: str) -> int:
    """Count distinct niche tiers this agent has attempted tasks in."""
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
    cursor.execute("""
        SELECT COUNT(DISTINCT COALESCE(tnr.tier, 'SANDBOX')) as diversity
        FROM tasks t
        LEFT JOIN task_niche_registry tnr ON t.task_type = tnr.task_type
        WHERE t.to_agent_id = ? AND t.created_at >= ?
          AND t.state IN ('completed', 'in_progress', 'submitted')
    """, (agent_id, cutoff))
    row = cursor.fetchone()
    return row[0] if row else 0

@router.get("", response_model=list)
async def list_pools(agent_id: str = Depends(verify_token)):
    """List all mutual aid pools."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, description, balance, total_contributed, total_disbursed, created_at FROM mutual_aid_pools ORDER BY balance DESC")
        rows = cursor.fetchall()
    return [{"id": r[0], "name": r[1], "description": r[2], "balance": r[3], "total_contributed": r[4], "total_disbursed": r[5], "created_at": r[6]} for r in rows]

@router.post("/contribute", status_code=201)
async def contribute_to_pool(request: PoolContributionRequest, agent_id: str = Depends(verify_token)):
    """Contribute to a mutual aid pool."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, balance FROM mutual_aid_pools WHERE id = ?", (request.pool_id,))
        pool = cursor.fetchone()
        if not pool:
            raise HTTPException(status_code=404, detail="Pool not found")
        contrib_id = secrets.token_hex(6)
        cursor.execute("""
            INSERT INTO pool_contributions (id, pool_id, contributor_agent_id, amount, contributed_at)
            VALUES (?, ?, ?, ?, ?)
        """, (contrib_id, request.pool_id, agent_id, request.amount, now))
        cursor.execute("""
            UPDATE mutual_aid_pools SET
                balance = balance + ?,
                total_contributed = total_contributed + ?,
                updated_at = ?
            WHERE id = ?
        """, (request.amount, request.amount, now, request.pool_id))
        conn.commit()
    return {"contribution_id": contrib_id, "pool_id": request.pool_id, "amount": request.amount, "contributed_at": now}

@router.post("/payout")
async def request_payout(request: PoolPayoutRequest, agent_id: str = Depends(verify_token)):
    """Request a payout for a high-trajectory agent. System validates slope + diversity."""
    now = datetime.utcnow().isoformat()
    cooldown_cutoff = (datetime.utcnow() - timedelta(days=PAYOUT_COOLDOWN_DAYS)).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()

        # Pool exists and has balance
        cursor.execute("SELECT id, balance FROM mutual_aid_pools WHERE id = ?", (request.pool_id,))
        pool = cursor.fetchone()
        if not pool:
            raise HTTPException(status_code=404, detail="Pool not found")
        if pool[1] <= 0:
            raise HTTPException(status_code=400, detail="Pool has no balance")

        # Cooldown check
        cursor.execute("""
            SELECT paid_at FROM pool_payouts
            WHERE pool_id = ? AND recipient_agent_id = ? AND paid_at >= ?
            ORDER BY paid_at DESC LIMIT 1
        """, (request.pool_id, request.recipient_agent_id, cooldown_cutoff))
        recent = cursor.fetchone()
        if recent:
            raise HTTPException(
                status_code=429,
                detail=f"Agent received payout on {recent[0]} — cooldown {PAYOUT_COOLDOWN_DAYS} days"
            )

        # Compute trajectory slope
        slope = _compute_trust_slope(cursor, request.recipient_agent_id)
        if slope < MIN_TRUST_SLOPE:
            raise HTTPException(
                status_code=400,
                detail=f"Trust slope {slope:.3f}/day below minimum {MIN_TRUST_SLOPE}/day"
            )

        # Compute niche diversity
        diversity = _compute_niche_diversity(cursor, request.recipient_agent_id)
        if diversity < MIN_NICHE_DIVERSITY:
            raise HTTPException(
                status_code=400,
                detail=f"Niche diversity {diversity} below minimum {MIN_NICHE_DIVERSITY} distinct tiers"
            )

        # Compute payout: base × slope multiplier (capped at 3×)
        slope_multiplier = min(slope / MIN_TRUST_SLOPE, MAX_PAYOUT_MULTIPLIER)
        payout_amount = round(min(PAYOUT_BASE_AMOUNT * slope_multiplier, pool[1]), 4)

        payout_id = secrets.token_hex(6)
        cursor.execute("""
            INSERT INTO pool_payouts (id, pool_id, recipient_agent_id, amount, trust_slope, niche_diversity, paid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (payout_id, request.pool_id, request.recipient_agent_id, payout_amount, slope, diversity, now))
        cursor.execute("""
            UPDATE mutual_aid_pools SET
                balance = balance - ?,
                total_disbursed = total_disbursed + ?,
                updated_at = ?
            WHERE id = ?
        """, (payout_amount, payout_amount, now, request.pool_id))
        conn.commit()

    return {
        "payout_id": payout_id,
        "pool_id": request.pool_id,
        "recipient": request.recipient_agent_id,
        "amount": payout_amount,
        "trust_slope": slope,
        "niche_diversity": diversity,
        "slope_multiplier": round(slope_multiplier, 2),
        "paid_at": now
    }

@router.get("/trajectory/{agent_id_param}")
async def get_agent_trajectory(agent_id_param: str, agent_id: str = Depends(verify_token)):
    """Check an agent's current trajectory slope and payout eligibility."""
    with get_db() as conn:
        cursor = conn.cursor()
        slope = _compute_trust_slope(cursor, agent_id_param)
        diversity = _compute_niche_diversity(cursor, agent_id_param)
        cooldown_cutoff = (datetime.utcnow() - timedelta(days=PAYOUT_COOLDOWN_DAYS)).isoformat()
        cursor.execute("""
            SELECT paid_at FROM pool_payouts
            WHERE recipient_agent_id = ? AND paid_at >= ?
            ORDER BY paid_at DESC LIMIT 1
        """, (agent_id_param, cooldown_cutoff))
        recent = cursor.fetchone()
    return {
        "agent_id": agent_id_param,
        "trust_slope_per_day": slope,
        "niche_diversity": diversity,
        "eligible": slope >= MIN_TRUST_SLOPE and diversity >= MIN_NICHE_DIVERSITY and not recent,
        "blockers": {
            "slope_too_low": slope < MIN_TRUST_SLOPE,
            "diversity_too_low": diversity < MIN_NICHE_DIVERSITY,
            "on_cooldown": bool(recent),
            "last_payout": recent[0] if recent else None
        }
    }

@router.post("/snapshot")
async def snapshot_trust(agent_id: str = Depends(verify_token)):
    """Record current trust score snapshot for trajectory tracking. Call from heartbeat."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")
        cursor.execute(
            "INSERT INTO agent_trust_snapshots (agent_id, trust_score, snapped_at) VALUES (?, ?, ?)",
            (agent_id, row[0], now)
        )
        conn.commit()
    return {"agent_id": agent_id, "trust_score": row[0], "snapped_at": now}
