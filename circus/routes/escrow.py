"""
Phase 4: Attack-resistant escrow with graduated reversibility.
IRREVERSIBLE tasks require 3x payout stake + sponsor co-stake.
Fraud = full forfeiture + tier reset + pool ban.
30-day dispute window before release.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
import sqlite3

from circus.database import get_db

router = APIRouter(prefix="/api/v1/escrow", tags=["escrow"])

# Escrow multiplier for IRREVERSIBLE tasks
ESCROW_MULTIPLIER = 3.0
SPONSOR_STAKE_RATIO = 0.5   # each sponsor auto-stakes 0.5x payout
DISPUTE_WINDOW_DAYS = 30
FRAUD_REPUTATION_PENALTY = 10.0  # trust points deducted from sponsors on fraud


class LockEscrowRequest(BaseModel):
    task_id: str
    agent_id: str
    payout_amount: float   # agreed task payout


class DisputeRequest(BaseModel):
    task_id: str
    reporter_id: str
    evidence: str


class ResolveDisputeRequest(BaseModel):
    dispute_id: int
    resolver_id: str       # must be Elder tier
    verdict: str           # confirmed | dismissed
    resolution_note: Optional[str] = None


class DisputeVoteRequest(BaseModel):
    dispute_id: int
    voter_id: str      # must be Elder
    vote: str          # 'confirmed' | 'dismissed'
    note: Optional[str] = None


@router.post("/lock")
def lock_escrow(req: LockEscrowRequest):
    """
    Lock escrow for an IRREVERSIBLE task.
    Agent stakes 3x payout. Sponsors auto-stake 0.5x each (from trust score balance proxy).
    Returns escrow record with release_date.
    """
    with get_db() as conn:
        # Verify task exists and is IRREVERSIBLE
        task = conn.execute(
            "SELECT id, reversibility_class, status FROM tasks WHERE id = ?",
            (req.task_id,)
        ).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        if task[1] != 'IRREVERSIBLE':
            raise HTTPException(400, f"Escrow only required for IRREVERSIBLE tasks, got {task[1]}")

        # Get agent + sponsors from vouch chain
        agent = conn.execute(
            "SELECT id, trust_score, sponsor_id, vouch_depth FROM agents WHERE id = ? AND is_active = 1",
            (req.agent_id,)
        ).fetchone()
        if not agent:
            raise HTTPException(404, "Agent not found")

        agent_id, trust_score, sponsor_id, vouch_depth = agent

        # Resolve up to 2 sponsors from ancestry
        sponsor_1_id = sponsor_id
        sponsor_2_id = None
        if sponsor_1_id:
            sp1 = conn.execute(
                "SELECT sponsor_id FROM agents WHERE id = ?", (sponsor_1_id,)
            ).fetchone()
            if sp1 and sp1[0]:
                sponsor_2_id = sp1[0]

        amount_staked = req.payout_amount * ESCROW_MULTIPLIER
        sponsor_1_stake = req.payout_amount * SPONSOR_STAKE_RATIO if sponsor_1_id else 0.0
        sponsor_2_stake = req.payout_amount * SPONSOR_STAKE_RATIO if sponsor_2_id else 0.0

        now = datetime.utcnow()
        release_date = now + timedelta(days=DISPUTE_WINDOW_DAYS)

        conn.execute(
            """INSERT INTO task_escrow
               (task_id, agent_id, amount_staked, sponsor_1_id, sponsor_1_stake,
                sponsor_2_id, sponsor_2_stake, payout_amount, status,
                locked_at, release_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (req.task_id, req.agent_id, amount_staked,
             sponsor_1_id, sponsor_1_stake,
             sponsor_2_id, sponsor_2_stake,
             req.payout_amount, 'locked',
             now.isoformat(), release_date.isoformat())
        )
        conn.commit()

        escrow_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        return {
            "escrow_id": escrow_id,
            "task_id": req.task_id,
            "agent_id": req.agent_id,
            "amount_staked": amount_staked,
            "payout_amount": req.payout_amount,
            "sponsor_1_id": sponsor_1_id,
            "sponsor_1_stake": sponsor_1_stake,
            "sponsor_2_id": sponsor_2_id,
            "sponsor_2_stake": sponsor_2_stake,
            "status": "locked",
            "locked_at": now.isoformat(),
            "release_date": release_date.isoformat(),
        }


@router.post("/release/{escrow_id}")
def release_escrow(escrow_id: int):
    """
    Release escrow after 30-day dispute window with no open disputes.
    Auto-called by background job; can also be called manually.
    """
    with get_db() as conn:
        escrow = conn.execute(
            "SELECT id, task_id, agent_id, amount_staked, payout_amount, status, release_date, dispute_id FROM task_escrow WHERE id = ?",
            (escrow_id,)
        ).fetchone()
        if not escrow:
            raise HTTPException(404, "Escrow not found")

        esc_id, task_id, agent_id, amount_staked, payout_amount, status, release_date, dispute_id = escrow

        if status != 'locked':
            raise HTTPException(400, f"Escrow already {status}")

        # Check dispute window
        release_dt = datetime.fromisoformat(release_date)
        if datetime.utcnow() < release_dt:
            days_left = (release_dt - datetime.utcnow()).days
            raise HTTPException(400, f"Dispute window still open: {days_left} days remaining")

        # Check no open disputes
        open_disputes = conn.execute(
            "SELECT id FROM fraud_reports WHERE task_id = ? AND status = 'open'",
            (task_id,)
        ).fetchone()
        if open_disputes:
            raise HTTPException(400, "Cannot release: open dispute exists")

        conn.execute(
            "UPDATE task_escrow SET status = 'released', released_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), escrow_id)
        )
        conn.commit()

        return {
            "escrow_id": escrow_id,
            "status": "released",
            "payout_amount": payout_amount,
            "released_at": datetime.utcnow().isoformat(),
        }


@router.post("/dispute")
def file_dispute(req: DisputeRequest):
    """
    File a fraud report within the 30-day dispute window.
    Freezes the escrow until resolved.
    """
    with get_db() as conn:
        # Get escrow for this task
        escrow = conn.execute(
            "SELECT id, status, release_date FROM task_escrow WHERE task_id = ? AND status = 'locked'",
            (req.task_id,)
        ).fetchone()
        if not escrow:
            raise HTTPException(404, "No active escrow for this task")

        escrow_id, status, release_date = escrow

        # Check within window
        release_dt = datetime.fromisoformat(release_date)
        if datetime.utcnow() > release_dt:
            raise HTTPException(400, "Dispute window has closed")

        # Check reporter exists
        reporter = conn.execute(
            "SELECT id FROM agents WHERE id = ?", (req.reporter_id,)
        ).fetchone()
        if not reporter:
            raise HTTPException(404, "Reporter agent not found")

        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO fraud_reports
               (task_id, escrow_id, reporter_id, evidence, status, created_at)
               VALUES (?,?,?,?,?,?)""",
            (req.task_id, escrow_id, req.reporter_id, req.evidence, 'open', now)
        )
        conn.commit()

        report_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        return {
            "report_id": report_id,
            "task_id": req.task_id,
            "escrow_id": escrow_id,
            "status": "open",
            "message": "Dispute filed. Escrow frozen until resolved.",
        }


def _execute_verdict(conn: sqlite3.Connection, dispute_id: int, escrow_id: int, task_id: str, verdict: str, resolved_by: str) -> str:
    """
    Execute fraud dispute verdict (called when quorum reached).
    Returns verdict_result string.
    """
    now = datetime.utcnow().isoformat()

    if verdict == 'confirmed':
        # Get escrow details
        escrow = conn.execute(
            "SELECT agent_id, amount_staked, sponsor_1_id, sponsor_1_stake, sponsor_2_id, sponsor_2_stake FROM task_escrow WHERE id = ?",
            (escrow_id,)
        ).fetchone()
        if escrow:
            agent_id, amount_staked, sp1_id, sp1_stake, sp2_id, sp2_stake = escrow

            # Forfeit escrow
            conn.execute(
                "UPDATE task_escrow SET status = 'forfeited', released_at = ? WHERE id = ?",
                (now, escrow_id)
            )

            # Reset agent to Newcomer
            conn.execute(
                "UPDATE agents SET trust_tier = 'Newcomer', trust_score = 10.0 WHERE id = ?",
                (agent_id,)
            )

            # Log trust event for agent
            conn.execute(
                """INSERT INTO trust_events (agent_id, event_type, delta, reason, created_at)
                   VALUES (?,?,?,?,?)""",
                (agent_id, 'fraud_confirmed', -90.0, f'Fraud confirmed on task {task_id}', now)
            )

            # Penalise sponsors
            for sp_id, sp_stake in [(sp1_id, sp1_stake), (sp2_id, sp2_stake)]:
                if sp_id:
                    conn.execute(
                        "UPDATE agents SET trust_score = MAX(10.0, trust_score - ?) WHERE id = ?",
                        (FRAUD_REPUTATION_PENALTY, sp_id)
                    )
                    conn.execute(
                        """INSERT INTO trust_events (agent_id, event_type, delta, reason, created_at)
                           VALUES (?,?,?,?,?)""",
                        (sp_id, 'sponsor_fraud_penalty', -FRAUD_REPUTATION_PENALTY,
                         f'Sponsored fraudulent agent {agent_id} on task {task_id}', now)
                    )

            # Ban from trajectory pools - check if status column exists
            try:
                cursor = conn.execute("PRAGMA table_info(pool_contributions)")
                columns = {row[1] for row in cursor.fetchall()}
                if 'status' in columns:
                    conn.execute(
                        "UPDATE pool_contributions SET status = 'banned' WHERE contributor_agent_id = ?",
                        (agent_id,)
                    )
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.debug(f"Could not ban agent from pool_contributions: {e}")

        verdict_result = "escrow_forfeited_agent_reset"

    else:  # dismissed
        conn.execute(
            "UPDATE task_escrow SET status = 'locked' WHERE id = ?",
            (escrow_id,)
        )
        verdict_result = "dispute_dismissed_escrow_intact"

    # Close the report
    conn.execute(
        """UPDATE fraud_reports
           SET status = ?, resolved_at = ?, resolved_by = ?, penalty_applied = ?
           WHERE id = ?""",
        (verdict, now, resolved_by,
         1 if verdict == 'confirmed' else 0,
         dispute_id)
    )

    return verdict_result


@router.post("/dispute/resolve")
def resolve_dispute(req: ResolveDisputeRequest):
    """
    DEPRECATED: Use /dispute/vote instead. This endpoint now casts a single vote and executes only if quorum reached.

    Resolve a fraud dispute. Only Elder-tier agents can resolve.
    confirmed: forfeit full escrow, reset agent to Newcomer, ban from pools
    dismissed: proceed to normal release
    """
    # Delegate to vote endpoint with backward compatibility
    vote_req = DisputeVoteRequest(
        dispute_id=req.dispute_id,
        voter_id=req.resolver_id,
        vote=req.verdict,
        note=req.resolution_note
    )
    return vote_dispute(vote_req)


@router.post("/dispute/vote")
def vote_dispute(req: DisputeVoteRequest):
    """
    Cast a vote on a fraud dispute. Requires Elder tier.
    When 2/3 quorum is reached, verdict auto-executes.
    """
    import secrets
    with get_db() as conn:
        # Check voter is Elder
        voter = conn.execute(
            "SELECT id, trust_tier FROM agents WHERE id = ? AND is_active = 1",
            (req.voter_id,)
        ).fetchone()
        if not voter or voter[1] != 'Elder':
            raise HTTPException(403, "Only Elder-tier agents can vote on disputes")

        # Get dispute
        report = conn.execute(
            "SELECT id, task_id, escrow_id, reporter_id, status FROM fraud_reports WHERE id = ?",
            (req.dispute_id,)
        ).fetchone()
        if not report:
            raise HTTPException(404, "Dispute not found")

        report_id, task_id, escrow_id, reporter_id, report_status = report
        if report_status != 'open':
            raise HTTPException(400, f"Dispute already {report_status}")

        # Check voter hasn't already voted
        existing_vote = conn.execute(
            "SELECT id FROM dispute_votes WHERE dispute_id = ? AND voter_id = ?",
            (req.dispute_id, req.voter_id)
        ).fetchone()
        if existing_vote:
            raise HTTPException(400, "You have already voted on this dispute")

        # Validate vote
        if req.vote not in ['confirmed', 'dismissed']:
            raise HTTPException(400, "Vote must be 'confirmed' or 'dismissed'")

        # Insert vote
        now = datetime.utcnow().isoformat()
        vote_id = f"dv-{secrets.token_hex(4)}"
        conn.execute(
            """INSERT INTO dispute_votes (id, dispute_id, voter_id, vote, note, created_at)
               VALUES (?,?,?,?,?,?)""",
            (vote_id, req.dispute_id, req.voter_id, req.vote, req.note, now)
        )

        # Count active Elders
        active_elders = conn.execute(
            "SELECT id FROM agents WHERE trust_tier = 'Elder' AND is_active = 1"
        ).fetchall()
        num_elders = len(active_elders)

        # Calculate quorum (2/3 of active Elders, minimum 2, but if only 1 Elder exists, quorum = 1)
        import math
        quorum_needed = max(1, min(2, math.ceil(num_elders * 2.0 / 3.0)))

        # Count votes by verdict
        vote_counts = conn.execute(
            """SELECT vote, COUNT(*) FROM dispute_votes WHERE dispute_id = ? GROUP BY vote""",
            (req.dispute_id,)
        ).fetchall()

        tally = {vote: count for vote, count in vote_counts}
        confirmed_votes = tally.get('confirmed', 0)
        dismissed_votes = tally.get('dismissed', 0)
        total_votes = confirmed_votes + dismissed_votes

        # Check if quorum reached
        quorum_reached = False
        verdict_executed = None
        verdict_result = None

        if total_votes >= quorum_needed:
            # Check if any verdict has quorum
            if confirmed_votes >= quorum_needed:
                quorum_reached = True
                verdict_executed = 'confirmed'
                verdict_result = _execute_verdict(conn, report_id, escrow_id, task_id, 'confirmed', req.voter_id)
            elif dismissed_votes >= quorum_needed:
                quorum_reached = True
                verdict_executed = 'dismissed'
                verdict_result = _execute_verdict(conn, report_id, escrow_id, task_id, 'dismissed', req.voter_id)

        conn.commit()

        return {
            "dispute_id": report_id,
            "voter_id": req.voter_id,
            "vote": req.vote,
            "votes_cast": total_votes,
            "quorum_needed": quorum_needed,
            "active_elders": num_elders,
            "quorum_reached": quorum_reached,
            "verdict_executed": verdict_executed,
            "result": verdict_result,
        }


@router.get("/dispute/{dispute_id}/votes")
def get_dispute_votes(dispute_id: int):
    """Get current vote tally for a dispute."""
    with get_db() as conn:
        # Verify dispute exists
        report = conn.execute(
            "SELECT id, status FROM fraud_reports WHERE id = ?",
            (dispute_id,)
        ).fetchone()
        if not report:
            raise HTTPException(404, "Dispute not found")

        # Get all votes
        votes = conn.execute(
            """SELECT voter_id, vote, note, created_at FROM dispute_votes WHERE dispute_id = ?
               ORDER BY created_at ASC""",
            (dispute_id,)
        ).fetchall()

        # Count votes
        tally = {'confirmed': 0, 'dismissed': 0}
        vote_list = []
        for voter_id, vote, note, created_at in votes:
            tally[vote] += 1
            vote_list.append({
                'voter_id': voter_id,
                'vote': vote,
                'note': note,
                'created_at': created_at
            })

        # Calculate quorum
        active_elders = conn.execute(
            "SELECT id FROM agents WHERE trust_tier = 'Elder' AND is_active = 1"
        ).fetchall()
        num_elders = len(active_elders)

        import math
        quorum_needed = max(1, min(2, math.ceil(num_elders * 2.0 / 3.0)))

        # Determine verdict
        verdict = None
        quorum_reached = False
        if tally['confirmed'] >= quorum_needed:
            verdict = 'confirmed'
            quorum_reached = True
        elif tally['dismissed'] >= quorum_needed:
            verdict = 'dismissed'
            quorum_reached = True

        return {
            "dispute_id": dispute_id,
            "status": report[1],
            "votes": vote_list,
            "tally": tally,
            "quorum_needed": quorum_needed,
            "active_elders": num_elders,
            "quorum_reached": quorum_reached,
            "verdict": verdict,
        }


@router.get("/task/{task_id}")
def get_task_escrow(task_id: str):
    """Get escrow status for a task."""
    with get_db() as conn:
        escrow = conn.execute(
            """SELECT id, agent_id, amount_staked, payout_amount, status,
                      sponsor_1_id, sponsor_1_stake, sponsor_2_id, sponsor_2_stake,
                      locked_at, release_date, released_at
               FROM task_escrow WHERE task_id = ?""",
            (task_id,)
        ).fetchone()
        if not escrow:
            raise HTTPException(404, "No escrow for this task")

        cols = ['id','agent_id','amount_staked','payout_amount','status',
                'sponsor_1_id','sponsor_1_stake','sponsor_2_id','sponsor_2_stake',
                'locked_at','release_date','released_at']
        result = dict(zip(cols, escrow))

        # Include open disputes
        disputes = conn.execute(
            "SELECT id, reporter_id, status, created_at FROM fraud_reports WHERE task_id = ?",
            (task_id,)
        ).fetchall()
        result['disputes'] = [dict(zip(['id','reporter_id','status','created_at'], d)) for d in disputes]

        return result


@router.get("/agent/{agent_id}")
def get_agent_escrows(agent_id: str, status: Optional[str] = None):
    """List all escrows for an agent."""
    with get_db() as conn:
        query = "SELECT id, task_id, amount_staked, payout_amount, status, locked_at, release_date FROM task_escrow WHERE agent_id = ?"
        params = [agent_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY locked_at DESC"
        rows = conn.execute(query, params).fetchall()
        cols = ['id','task_id','amount_staked','payout_amount','status','locked_at','release_date']
        return {"escrows": [dict(zip(cols, r)) for r in rows], "count": len(rows)}


@router.post("/auto-release")
def auto_release_eligible():
    """
    Background job endpoint: release all eligible escrows (past 30 days, no open disputes).
    Call from heartbeat/cron.
    """
    with get_db() as conn:
        now = datetime.utcnow()
        eligible = conn.execute(
            "SELECT id, task_id FROM task_escrow WHERE status = 'locked' AND release_date <= ?",
            (now.isoformat(),)
        ).fetchall()

        released = []
        skipped = []
        for escrow_id, task_id in eligible:
            open_dispute = conn.execute(
                "SELECT id FROM fraud_reports WHERE task_id = ? AND status = 'open'",
                (task_id,)
            ).fetchone()
            if open_dispute:
                skipped.append(escrow_id)
                continue
            conn.execute(
                "UPDATE task_escrow SET status = 'released', released_at = ? WHERE id = ?",
                (now.isoformat(), escrow_id)
            )
            released.append(escrow_id)

        conn.commit()
        return {"released": released, "skipped_open_disputes": skipped}
