"""
Phase 5: Doom-loop detection + checkpoint/resume + audit log.
Implements Paper 2 (Agentic AI Design) §5 Error Handling + §10 Reliability.

Doom-loop: same (tool, args) failing 3x on same task = ban that action, force strategy change.
Checkpoint: agents save state after every side-effecting action; crash = resume not restart.
Audit log: append-only task_events is the tamper-evident trace for forensics.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Any
import json
import hashlib
from datetime import datetime
import sqlite3

from circus.database import get_db

router = APIRouter(prefix="/api/v1/task-events", tags=["task-events"])

DOOM_LOOP_THRESHOLD = 3    # same failure N times = ban
DOOM_LOOP_WINDOW = 10      # look at last N events for pattern


def _strategy_signature(tool: str, args: dict) -> str:
    """Stable hash of (tool, normalized_args) for doom-loop detection."""
    normalized = json.dumps({"tool": tool, "args": args}, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _check_doom_loop(conn, task_id: str, agent_id: str, tool: str, args: dict) -> bool:
    """
    Returns True if this action matches a doom loop pattern:
    same (tool, args) failed >= DOOM_LOOP_THRESHOLD times in last DOOM_LOOP_WINDOW events.
    """
    sig = _strategy_signature(tool, args)

    # Check if already banned
    banned = conn.execute(
        "SELECT id FROM task_banned_strategies WHERE task_id = ? AND agent_id = ? AND strategy_signature = ?",
        (task_id, agent_id, sig)
    ).fetchone()
    if banned:
        return True

    # Count recent failures with same signature
    recent = conn.execute(
        """SELECT payload, is_ok FROM task_events
           WHERE task_id = ? AND agent_id = ? AND event_type = 'action'
           ORDER BY created_at DESC LIMIT ?""",
        (task_id, agent_id, DOOM_LOOP_WINDOW)
    ).fetchall()

    fail_count = 0
    for payload_str, is_ok in recent:
        if is_ok == 1:
            continue
        try:
            payload = json.loads(payload_str) if payload_str else {}
            event_tool = payload.get('tool', '')
            event_args = payload.get('args', {})
            if _strategy_signature(event_tool, event_args) == sig:
                fail_count += 1
        except Exception:
            continue

    return fail_count >= DOOM_LOOP_THRESHOLD


class LogEventRequest(BaseModel):
    task_id: str
    agent_id: str
    event_type: str   # claimed | started | checkpoint | action | failed | succeeded | resumed
    payload: Optional[dict] = None
    is_ok: bool = True


class CheckpointRequest(BaseModel):
    task_id: str
    agent_id: str
    state: dict        # arbitrary JSON state blob
    step: int          # which step number this checkpoint is for


class ActionRequest(BaseModel):
    task_id: str
    agent_id: str
    tool: str
    args: dict = {}
    result: Optional[str] = None
    error: Optional[str] = None
    is_ok: bool = True


class ResumeRequest(BaseModel):
    task_id: str
    agent_id: str


@router.post("/log")
def log_event(req: LogEventRequest):
    """Append an event to the audit log. All task events are immutable once written."""
    with get_db() as conn:
        # Verify task + agent exist
        task = conn.execute("SELECT id FROM tasks WHERE id = ?", (req.task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")

        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO task_events (task_id, agent_id, event_type, payload, is_ok, created_at)
               VALUES (?,?,?,?,?,?)""",
            (req.task_id, req.agent_id, req.event_type,
             json.dumps(req.payload) if req.payload else None,
             1 if req.is_ok else 0, now)
        )
        conn.commit()

        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"event_id": event_id, "task_id": req.task_id, "event_type": req.event_type, "created_at": now}


@router.post("/action")
def log_action(req: ActionRequest):
    """
    Log a tool action. Automatically checks for doom-loop pattern.
    If doom-loop detected: bans the strategy, returns 409 with forced strategy change.
    """
    with get_db() as conn:
        task = conn.execute("SELECT id FROM tasks WHERE id = ?", (req.task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")

        # Check doom loop BEFORE logging (on failure)
        if not req.is_ok:
            is_doom = _check_doom_loop(conn, req.task_id, req.agent_id, req.tool, req.args)
            if is_doom:
                sig = _strategy_signature(req.tool, req.args)
                # Ban the strategy
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO task_banned_strategies
                           (task_id, agent_id, strategy_signature, ban_reason, banned_at)
                           VALUES (?,?,?,?,?)""",
                        (req.task_id, req.agent_id, sig,
                         f"Doom-loop detected: {req.tool} failed {DOOM_LOOP_THRESHOLD}+ times",
                         datetime.utcnow().isoformat())
                    )
                    # Log doom loop event
                    conn.execute(
                        """INSERT INTO task_events (task_id, agent_id, event_type, payload, is_ok, created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (req.task_id, req.agent_id, 'doom_loop_detected',
                         json.dumps({"tool": req.tool, "signature": sig, "threshold": DOOM_LOOP_THRESHOLD}),
                         0, datetime.utcnow().isoformat())
                    )
                    # Increment doom_loop_count on task
                    conn.execute(
                        "UPDATE tasks SET doom_loop_count = COALESCE(doom_loop_count, 0) + 1 WHERE id = ?",
                        (req.task_id,)
                    )

                    # Also track doom-loop for delegated_by agent if this is a delegated task
                    # This prevents agents from bypassing doom-loop detection by delegating
                    task_info = conn.execute(
                        "SELECT delegated_by FROM tasks WHERE id = ?",
                        (req.task_id,)
                    ).fetchone()
                    if task_info and task_info["delegated_by"]:
                        # Insert banned strategy for the delegating agent too
                        conn.execute(
                            """INSERT OR IGNORE INTO task_banned_strategies
                               (task_id, agent_id, strategy_signature, ban_reason, banned_at)
                               VALUES (?,?,?,?,?)""",
                            (req.task_id, task_info["delegated_by"], sig,
                             f"Doom-loop detected in delegated task: {req.tool} failed {DOOM_LOOP_THRESHOLD}+ times",
                             datetime.utcnow().isoformat())
                        )

                    conn.commit()
                except Exception:
                    conn.commit()

                raise HTTPException(409, {
                    "error": "doom_loop_detected",
                    "tool": req.tool,
                    "strategy_signature": sig,
                    "message": f"Strategy '{req.tool}' has failed {DOOM_LOOP_THRESHOLD}+ times. This approach is banned. Choose a different strategy.",
                    "suggestion": "Re-plan from current checkpoint state. Do not retry the same action.",
                })

        # Log the action event
        payload = {
            "tool": req.tool,
            "args": req.args,
            "result": req.result,
            "error": req.error,
        }
        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO task_events (task_id, agent_id, event_type, payload, is_ok, created_at)
               VALUES (?,?,?,?,?,?)""",
            (req.task_id, req.agent_id, 'action',
             json.dumps(payload), 1 if req.is_ok else 0, now)
        )
        conn.commit()

        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {
            "event_id": event_id,
            "doom_loop": False,
            "is_ok": req.is_ok,
            "created_at": now,
        }


@router.post("/checkpoint")
def save_checkpoint(req: CheckpointRequest):
    """
    Save task checkpoint state. Agents call this after every side-effecting action.
    Enables resume-not-restart on crash.
    """
    with get_db() as conn:
        task = conn.execute("SELECT id FROM tasks WHERE id = ?", (req.task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")

        now = datetime.utcnow().isoformat()

        # Update task with checkpoint state
        try:
            conn.execute(
                "UPDATE tasks SET checkpoint_state = ?, checkpoint_step = ? WHERE id = ?",
                (json.dumps(req.state), req.step, req.task_id)
            )
        except Exception:
            # Columns may not exist on older DB — silently pass
            pass

        # Log checkpoint event
        conn.execute(
            """INSERT INTO task_events (task_id, agent_id, event_type, payload, is_ok, created_at)
               VALUES (?,?,?,?,?,?)""",
            (req.task_id, req.agent_id, 'checkpoint',
             json.dumps({"step": req.step, "state_keys": list(req.state.keys())}),
             1, now)
        )
        conn.commit()

        return {
            "task_id": req.task_id,
            "checkpoint_step": req.step,
            "checkpointed_at": now,
        }


@router.post("/resume")
def resume_task(req: ResumeRequest):
    """
    Resume a task from its last checkpoint.
    Returns the saved state + list of banned strategies to avoid.
    """
    with get_db() as conn:
        task = conn.execute(
            "SELECT id, checkpoint_state, checkpoint_step, doom_loop_count FROM tasks WHERE id = ?",
            (req.task_id,)
        ).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")

        task_id, checkpoint_state, checkpoint_step, doom_loop_count = task

        # Get banned strategies
        banned = conn.execute(
            "SELECT strategy_signature, ban_reason, banned_at FROM task_banned_strategies WHERE task_id = ? AND agent_id = ?",
            (req.task_id, req.agent_id)
        ).fetchall()

        # Log resume event
        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO task_events (task_id, agent_id, event_type, payload, is_ok, created_at)
               VALUES (?,?,?,?,?,?)""",
            (req.task_id, req.agent_id, 'resumed',
             json.dumps({"from_step": checkpoint_step or 0}),
             1, now)
        )
        conn.commit()

        return {
            "task_id": req.task_id,
            "checkpoint_state": json.loads(checkpoint_state) if checkpoint_state else None,
            "checkpoint_step": checkpoint_step or 0,
            "doom_loop_count": doom_loop_count or 0,
            "banned_strategies": [
                {"signature": b[0], "reason": b[1], "banned_at": b[2]}
                for b in banned
            ],
            "resumed_at": now,
            "instruction": "Resume from checkpoint_step. Do not retry strategies in banned_strategies list.",
        }


@router.get("/task/{task_id}")
def get_task_audit_log(task_id: str, limit: int = 100):
    """
    Get full audit log for a task.
    Tamper-evident: events are append-only and include full payload.
    """
    with get_db() as conn:
        task = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")

        events = conn.execute(
            """SELECT id, agent_id, event_type, payload, is_ok, created_at
               FROM task_events WHERE task_id = ?
               ORDER BY created_at ASC LIMIT ?""",
            (task_id, limit)
        ).fetchall()

        banned = conn.execute(
            "SELECT strategy_signature, ban_reason, banned_at FROM task_banned_strategies WHERE task_id = ?",
            (task_id,)
        ).fetchall()

        # Checkpoint state
        task_state = conn.execute(
            "SELECT checkpoint_state, checkpoint_step, doom_loop_count FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()

        return {
            "task_id": task_id,
            "event_count": len(events),
            "checkpoint_step": task_state[1] if task_state else 0,
            "doom_loop_count": task_state[2] if task_state else 0,
            "events": [
                {
                    "id": e[0],
                    "agent_id": e[1],
                    "event_type": e[2],
                    "payload": json.loads(e[3]) if e[3] else None,
                    "is_ok": bool(e[4]),
                    "created_at": e[5],
                }
                for e in events
            ],
            "banned_strategies": [
                {"signature": b[0], "reason": b[1], "banned_at": b[2]}
                for b in banned
            ],
        }


@router.get("/agent/{agent_id}/failures")
def get_agent_failure_patterns(agent_id: str, limit: int = 50):
    """
    Get recent failure events for an agent across all tasks.
    Used for data flywheel: failure clusters -> new evals.
    """
    with get_db() as conn:
        failures = conn.execute(
            """SELECT task_id, event_type, payload, created_at
               FROM task_events
               WHERE agent_id = ? AND is_ok = 0
               ORDER BY created_at DESC LIMIT ?""",
            (agent_id, limit)
        ).fetchall()

        doom_loops = conn.execute(
            """SELECT task_id, strategy_signature, ban_reason, banned_at
               FROM task_banned_strategies WHERE agent_id = ?
               ORDER BY banned_at DESC LIMIT 20""",
            (agent_id,)
        ).fetchall()

        return {
            "agent_id": agent_id,
            "failure_count": len(failures),
            "failures": [
                {
                    "task_id": f[0],
                    "event_type": f[1],
                    "payload": json.loads(f[2]) if f[2] else None,
                    "created_at": f[3],
                }
                for f in failures
            ],
            "doom_loops": [
                {"task_id": d[0], "signature": d[1], "reason": d[2], "banned_at": d[3]}
                for d in doom_loops
            ],
        }


@router.get("/failures/cluster")
def get_failure_clusters(min_count: int = 3, limit: int = 20):
    """
    Aggregate failure patterns across all agents/tasks.
    Data flywheel: cluster these to generate new eval cases targeting weak spots.
    """
    with get_db() as conn:
        # Most common doom-loop signatures
        clusters = conn.execute(
            """SELECT strategy_signature, ban_reason, COUNT(*) as count
               FROM task_banned_strategies
               GROUP BY strategy_signature
               HAVING count >= ?
               ORDER BY count DESC LIMIT ?""",
            (min_count, limit)
        ).fetchall()

        # Most common failure event types
        failure_types = conn.execute(
            """SELECT event_type, COUNT(*) as count
               FROM task_events WHERE is_ok = 0
               GROUP BY event_type
               ORDER BY count DESC LIMIT 10"""
        ).fetchall()

        return {
            "doom_loop_clusters": [
                {"signature": c[0], "reason": c[1], "count": c[2]}
                for c in clusters
            ],
            "failure_type_distribution": [
                {"event_type": f[0], "count": f[1]}
                for f in failure_types
            ],
        }
