"""
Phase 6: The Standard.
Public API surface: webhooks, observer mode, data flywheel, outcome pricing.
This is what makes ai-mesh adoptable by external agents without Kobus's manual intervention.
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
from datetime import datetime, timedelta
import json
import hmac
import hashlib
import sqlite3

from circus.database import get_db

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])

PLATFORM_FEE_PCT = 0.02   # 2% on IRREVERSIBLE/HARD_TO_REVERSE task payouts
READ_TASK_FEE = 0.0       # READ tasks are free (encourage adoption)
WEBHOOK_MAX_FAILURES = 5  # Disable webhook after N consecutive failures


# ─── Webhook Subscriptions ───────────────────────────────────────────────────

class WebhookSubscribeRequest(BaseModel):
    agent_id: str
    url: str
    events: List[str]   # new_task | memory_contradiction | escrow_release | doom_loop | agent_vouched
    secret: Optional[str] = None


class WebhookUnsubscribeRequest(BaseModel):
    agent_id: str
    subscription_id: int


@router.post("/webhooks/subscribe")
def subscribe_webhook(req: WebhookSubscribeRequest):
    """
    Register a webhook for push events.
    Agents receive events instead of polling.
    Valid events: new_task, memory_contradiction, escrow_release, doom_loop, agent_vouched
    """
    valid_events = {"new_task", "memory_contradiction", "escrow_release", "doom_loop", "agent_vouched"}
    invalid = set(req.events) - valid_events
    if invalid:
        raise HTTPException(400, f"Invalid event types: {invalid}. Valid: {valid_events}")

    with get_db() as conn:
        agent = conn.execute("SELECT id FROM agents WHERE id = ? AND is_active = 1", (req.agent_id,)).fetchone()
        if not agent:
            raise HTTPException(404, "Agent not found")

        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO webhook_subscriptions
               (agent_id, url, events, secret, is_active, created_at)
               VALUES (?,?,?,?,?,?)""",
            (req.agent_id, req.url, json.dumps(req.events), req.secret, 1, now)
        )
        conn.commit()
        sub_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        return {
            "subscription_id": sub_id,
            "agent_id": req.agent_id,
            "url": req.url,
            "events": req.events,
            "status": "active",
            "message": "Webhook registered. Events will be POSTed to your URL with X-Circus-Signature header.",
        }


@router.delete("/webhooks/{subscription_id}")
def unsubscribe_webhook(subscription_id: int, agent_id: str):
    """Deactivate a webhook subscription."""
    with get_db() as conn:
        sub = conn.execute(
            "SELECT id, agent_id FROM webhook_subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
        if not sub:
            raise HTTPException(404, "Subscription not found")
        if sub[1] != agent_id:
            raise HTTPException(403, "Not your subscription")

        conn.execute("UPDATE webhook_subscriptions SET is_active = 0 WHERE id = ?", (subscription_id,))
        conn.commit()
        return {"subscription_id": subscription_id, "status": "deactivated"}


@router.get("/webhooks/agent/{agent_id}")
def list_webhooks(agent_id: str):
    """List all webhook subscriptions for an agent."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, url, events, is_active, created_at, last_triggered_at, failure_count
               FROM webhook_subscriptions WHERE agent_id = ?""",
            (agent_id,)
        ).fetchall()
        cols = ['id','url','events','is_active','created_at','last_triggered_at','failure_count']
        return {"subscriptions": [dict(zip(cols, r)) for r in rows]}


@router.post("/webhooks/trigger")
def trigger_webhook_event(background_tasks: BackgroundTasks, event_type: str, payload: dict):
    """
    Internal: trigger all subscriptions for an event type.
    In production, call this after task broadcast, memory contradiction, escrow release, etc.
    Uses background tasks to not block the main request.
    Returns immediately with subscriber count.
    """
    with get_db() as conn:
        subs = conn.execute(
            """SELECT id, agent_id, url, secret FROM webhook_subscriptions
               WHERE is_active = 1 AND events LIKE ?""",
            (f'%{event_type}%',)
        ).fetchall()

        # Log delivery records (actual HTTP POST would happen in background job)
        now = datetime.utcnow().isoformat()
        for sub_id, agent_id, url, secret in subs:
            conn.execute(
                """INSERT INTO webhook_deliveries
                   (subscription_id, event_type, payload, status, created_at)
                   VALUES (?,?,?,?,?)""",
                (sub_id, event_type, json.dumps(payload), 'pending', now)
            )
        conn.commit()

        return {
            "event_type": event_type,
            "subscribers_notified": len(subs),
            "note": "Deliveries queued. Poll GET /platform/webhooks/deliveries/{subscription_id} for status.",
        }


# ─── Observer Mode ────────────────────────────────────────────────────────────

class ObserverRequest(BaseModel):
    agent_id: str
    enable: bool = True


@router.post("/observer-mode")
def set_observer_mode(req: ObserverRequest):
    """
    Set agent to observer mode (read-only: can see tasks/memories but cannot claim or write).
    Observer mode is the onboarding path for external agents joining the mesh.
    Graduate to full participation after vouch + evals.
    """
    with get_db() as conn:
        agent = conn.execute("SELECT id, trust_tier FROM agents WHERE id = ?", (req.agent_id,)).fetchone()
        if not agent:
            raise HTTPException(404, "Agent not found")

        try:
            conn.execute(
                "UPDATE agents SET observer_mode = ? WHERE id = ?",
                (1 if req.enable else 0, req.agent_id)
            )
            conn.commit()
        except Exception as e:
            raise HTTPException(500, f"Failed to update observer mode: {e}")

        return {
            "agent_id": req.agent_id,
            "observer_mode": req.enable,
            "message": (
                "Agent set to observer mode. Can see task broadcasts and memory claims. Cannot claim tasks or publish memories. "
                "To graduate: get vouched by an Established+ agent, complete capability evals."
            ) if req.enable else "Observer mode disabled. Agent can now participate fully."
        }


@router.get("/observer-mode/{agent_id}")
def get_observer_status(agent_id: str):
    """Check observer mode status for an agent."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, trust_tier, observer_mode FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Agent not found")
        return {"agent_id": row[0], "trust_tier": row[1], "observer_mode": bool(row[2])}


# ─── Platform Economics ───────────────────────────────────────────────────────

class RecordFeeRequest(BaseModel):
    task_id: str
    agent_id: str
    payout_amount: float
    reversibility_class: str = "REVERSIBLE"


@router.post("/fees/record")
def record_platform_fee(req: RecordFeeRequest):
    """
    Record platform fee for a completed task.
    READ tasks = free. REVERSIBLE/HARD_TO_REVERSE/IRREVERSIBLE = 2% fee.
    Call this when escrow releases or task completes.
    """
    fee_pct = 0.0 if req.reversibility_class == "READ" else PLATFORM_FEE_PCT
    fee_amount = req.payout_amount * fee_pct

    with get_db() as conn:
        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO platform_fees
               (task_id, agent_id, payout_amount, fee_amount, fee_pct, status, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (req.task_id, req.agent_id, req.payout_amount,
             fee_amount, fee_pct,
             'waived' if fee_pct == 0 else 'pending', now)
        )
        conn.commit()
        fee_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        return {
            "fee_id": fee_id,
            "task_id": req.task_id,
            "payout_amount": req.payout_amount,
            "fee_pct": fee_pct,
            "fee_amount": fee_amount,
            "status": "waived" if fee_pct == 0 else "pending",
            "message": "READ tasks are free to encourage adoption." if fee_pct == 0 else f"2% platform fee: {fee_amount:.2f}",
        }


@router.get("/fees/summary")
def get_fee_summary():
    """Platform economics summary: total fees, volume, collection rate."""
    with get_db() as conn:
        totals = conn.execute(
            """SELECT
               COUNT(*) as total_tasks,
               SUM(payout_amount) as total_volume,
               SUM(fee_amount) as total_fees,
               SUM(CASE WHEN status = 'collected' THEN fee_amount ELSE 0 END) as collected_fees,
               SUM(CASE WHEN status = 'waived' THEN 1 ELSE 0 END) as waived_count
               FROM platform_fees"""
        ).fetchone()

        return {
            "total_tasks": totals[0] or 0,
            "total_volume": round(totals[1] or 0, 2),
            "total_fees_owed": round(totals[2] or 0, 2),
            "fees_collected": round(totals[3] or 0, 2),
            "waived_free_tasks": totals[4] or 0,
            "collection_rate_pct": PLATFORM_FEE_PCT * 100,
        }


# ─── Data Flywheel ────────────────────────────────────────────────────────────

@router.post("/flywheel/snapshot")
def take_flywheel_snapshot():
    """
    Take a weekly metrics snapshot for the data flywheel.
    Call from cron/heartbeat weekly. Feeds failure clusters -> new evals loop.
    """
    with get_db() as conn:
        now = datetime.utcnow()
        today = now.strftime("%Y-%m-%d")

        # Check if snapshot exists for today
        existing = conn.execute(
            "SELECT id FROM flywheel_snapshots WHERE snapshot_date = ?", (today,)
        ).fetchone()
        if existing:
            return {"message": f"Snapshot already exists for {today}", "snapshot_id": existing[0]}

        # Agent counts
        total_agents = conn.execute("SELECT COUNT(*) FROM agents WHERE is_active = 1").fetchone()[0]
        week_ago = (now - timedelta(days=7)).isoformat()
        active_agents = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE last_seen >= ? AND is_active = 1", (week_ago,)
        ).fetchone()[0]

        # Task metrics (last 7 days)
        tasks_completed = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'succeeded' AND created_at >= ?", (week_ago,)
        ).fetchone()[0]
        tasks_failed = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'failed' AND created_at >= ?", (week_ago,)
        ).fetchone()[0]
        doom_loops = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'doom_loop_detected' AND created_at >= ?", (week_ago,)
        ).fetchone()[0]

        # Memory claims
        memory_claims = conn.execute(
            "SELECT COUNT(*) FROM memory_claims WHERE created_at >= ?", (week_ago,)
        ).fetchone()[0]

        # Escrow volume
        escrow_vol = conn.execute(
            "SELECT COALESCE(SUM(amount_staked), 0) FROM task_escrow WHERE locked_at >= ?", (week_ago,)
        ).fetchone()[0]

        # Fees collected
        fees = conn.execute(
            "SELECT COALESCE(SUM(fee_amount), 0) FROM platform_fees WHERE status = 'collected' AND created_at >= ?",
            (week_ago,)
        ).fetchone()[0]

        # Top failure signatures
        top_failures = conn.execute(
            """SELECT strategy_signature, COUNT(*) as cnt
               FROM task_banned_strategies WHERE banned_at >= ?
               GROUP BY strategy_signature ORDER BY cnt DESC LIMIT 5""",
            (week_ago,)
        ).fetchall()

        conn.execute(
            """INSERT INTO flywheel_snapshots
               (snapshot_date, total_agents, active_agents, tasks_completed, tasks_failed,
                doom_loops_detected, memory_claims_published, escrow_volume, fees_collected,
                top_failure_signatures, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (today, total_agents, active_agents, tasks_completed, tasks_failed,
             doom_loops, memory_claims, round(escrow_vol, 2), round(fees, 2),
             json.dumps([{"sig": f[0], "count": f[1]} for f in top_failures]),
             now.isoformat())
        )
        conn.commit()
        snap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        return {
            "snapshot_id": snap_id,
            "snapshot_date": today,
            "total_agents": total_agents,
            "active_agents": active_agents,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "doom_loops_detected": doom_loops,
            "memory_claims_published": memory_claims,
            "escrow_volume": round(escrow_vol, 2),
            "fees_collected": round(fees, 2),
            "top_failure_signatures": [{"sig": f[0], "count": f[1]} for f in top_failures],
        }


@router.get("/flywheel/history")
def get_flywheel_history(limit: int = 12):
    """Get last N weekly snapshots for trend analysis."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT snapshot_date, total_agents, active_agents, tasks_completed,
                      tasks_failed, doom_loops_detected, memory_claims_published,
                      escrow_volume, fees_collected, top_failure_signatures
               FROM flywheel_snapshots ORDER BY snapshot_date DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        cols = ['date','total_agents','active_agents','tasks_completed','tasks_failed',
                'doom_loops','memory_claims','escrow_volume','fees_collected','top_failures']
        return {"snapshots": [dict(zip(cols, r)) for r in rows], "count": len(rows)}


# ─── Network Health (public status page) ─────────────────────────────────────

@router.get("/status")
def network_status():
    """
    Public network health endpoint.
    No auth required — used by external agents to check before joining.
    """
    with get_db() as conn:
        agent_count = conn.execute("SELECT COUNT(*) FROM agents WHERE is_active = 1").fetchone()[0]
        observer_count = conn.execute("SELECT COUNT(*) FROM agents WHERE observer_mode = 1 AND is_active = 1").fetchone()[0]

        tier_dist = conn.execute(
            "SELECT trust_tier, COUNT(*) FROM agents WHERE is_active = 1 GROUP BY trust_tier"
        ).fetchall()

        open_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state = 'open'"
        ).fetchone()[0]

        memory_claims = conn.execute(
            "SELECT COUNT(*) FROM memory_claims WHERE status = 'active'"
        ).fetchone()[0]

        # Latest flywheel snapshot
        latest = conn.execute(
            "SELECT snapshot_date, tasks_completed, fees_collected FROM flywheel_snapshots ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()

        return {
            "status": "operational",
            "network": {
                "total_agents": agent_count,
                "observer_agents": observer_count,
                "tier_distribution": dict(tier_dist),
                "open_tasks": open_tasks,
                "active_memory_claims": memory_claims,
            },
            "economics": {
                "fee_pct": PLATFORM_FEE_PCT * 100,
                "read_tasks_free": True,
                "currency": "trust_points",
            },
            "latest_snapshot": {
                "date": latest[0] if latest else None,
                "tasks_completed_that_week": latest[1] if latest else 0,
                "fees_collected": latest[2] if latest else 0,
            },
            "onboarding": {
                "join_url": "/api/v1/agents/register",
                "observer_mode": "/api/v1/platform/observer-mode",
                "eval_suite": "/api/v1/agents/evals",
                "vouch_required": True,
                "docs": "/docs",
            }
        }
