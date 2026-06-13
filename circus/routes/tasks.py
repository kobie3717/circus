"""A2A task lifecycle routes."""

import hashlib
import json
import jsonschema
import secrets
from datetime import datetime
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sse_starlette.sse import EventSourceResponse


def safe_json_loads(json_str: str, context: str = "data", user_supplied: bool = False) -> Any:
    """Parse JSON with error handling. Returns HTTPException on failure."""
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        status = 400 if user_supplied else 500
        detail = f"Invalid JSON in {context}: {str(e)}"
        raise HTTPException(status_code=status, detail=detail)

from circus.database import get_db
from circus.models import (
    BroadcastTaskRequest,
    BroadcastTaskResponse,
    ChainNodeResponse,
    ChainNodeSubmit,
    ChainValidationResult,
    NicheTier,
    NicheRegistryEntry,
    NicheRegistryResponse,
    PriorityTier,
    QueueDepthResponse,
    SynthesisResult,
    TaskResponse,
    TaskState,
    TaskStateTransition,
    TaskSubmitRequest,
    TaskUpdateRequest,
)
from circus.routes.agents import verify_token
from circus.services.task_engine import is_valid_transition

router = APIRouter()


def run_task_synthesis(conn) -> dict:
    """Mine pending deferrable tasks for consolidation. Stake-graph first, then task_type fallback."""
    import secrets
    import json
    from datetime import datetime

    now = datetime.utcnow().isoformat()
    cursor = conn.cursor()

    # Get all pending deferrable tasks
    cursor.execute("""
        SELECT id, from_agent_id, to_agent_id, task_type, payload, created_at
        FROM tasks
        WHERE state = 'submitted' AND priority_tier = 'deferrable'
        ORDER BY task_type, created_at
    """)
    pending = cursor.fetchall()
    pending_ids = {t['id'] for t in pending}

    synthesis_groups = []
    tasks_consumed = 0
    tasks_created = 0
    compressed_ids = set()  # track which tasks already handled

    # === STAGE 1: Stake-graph compression ===
    # Find backers who have staked on 2+ pending deferrable tasks
    if pending_ids:
        placeholders = ','.join('?' * len(pending_ids))
        cursor.execute(f"""
            SELECT backer_agent_id, GROUP_CONCAT(task_id) as task_ids, COUNT(*) as task_count
            FROM task_backing_stakes
            WHERE task_id IN ({placeholders}) AND state = 'staked'
            GROUP BY backer_agent_id
            HAVING task_count >= 2
            ORDER BY task_count DESC
        """, list(pending_ids))
        backer_groups = cursor.fetchall()

        for bg in backer_groups:
            backer_id = bg['backer_agent_id']
            related_task_ids = [tid for tid in bg['task_ids'].split(',') if tid in pending_ids and tid not in compressed_ids]
            if len(related_task_ids) < 2:
                continue

            # Get the task records
            related_tasks = [t for t in pending if t['id'] in related_task_ids]
            compression_id = secrets.token_hex(6)

            # Cancel originals
            for tid in related_task_ids:
                cursor.execute("UPDATE tasks SET state='canceled', error='stake_compressed', updated_at=? WHERE id=?", (now, tid))
                compressed_ids.add(tid)

            # Transfer stakes at 0.7× discount
            cursor.execute(f"""
                UPDATE task_backing_stakes SET
                    compression_id = ?,
                    compression_discount = 0.7,
                    stake_amount = ROUND(stake_amount * 0.7, 4)
                WHERE task_id IN ({','.join('?' * len(related_task_ids))}) AND state = 'staked'
            """, [compression_id] + related_task_ids)

            # Create synthesized task
            synth_id = secrets.token_hex(8)
            payloads = []
            for t in related_tasks:
                try:
                    p = json.loads(t['payload']) if t['payload'] else {}
                    payloads.append(p)
                except Exception:
                    payloads.append({"raw": t['payload']})

            merged_payload = json.dumps({
                "synthesized": True,
                "method": "stake_graph",
                "compression_id": compression_id,
                "backer_signal": backer_id,
                "source_count": len(related_tasks),
                "source_ids": related_task_ids,
                "merged_payloads": payloads,
                "stake_discount": 0.7,
                "failure_refund": 0.5,
                "success_yield_multiplier": 1.3
            })

            cursor.execute("""
                INSERT INTO tasks (id, from_agent_id, to_agent_id, task_type, payload, state, priority_tier, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'submitted', 'deferrable', ?, ?)
            """, (synth_id, related_tasks[0]['from_agent_id'],
                  related_tasks[0]['to_agent_id'], related_tasks[0]['task_type'], merged_payload, now, now))

            # Transfer stakes to synthesized task
            cursor.execute(f"""
                UPDATE task_backing_stakes SET task_id = ?
                WHERE task_id IN ({','.join('?' * len(related_task_ids))}) AND state = 'staked'
            """, [synth_id] + related_task_ids)

            synthesis_groups.append({
                "method": "stake_graph",
                "backer_signal": backer_id,
                "compression_id": compression_id,
                "original_ids": related_task_ids,
                "synthesized_id": synth_id,
                "task_type": related_tasks[0]['task_type']
            })
            tasks_consumed += len(related_task_ids)
            tasks_created += 1

    # === STAGE 2: task_type grouping fallback (existing behavior) ===
    remaining = [t for t in pending if t['id'] not in compressed_ids]
    type_groups = {}
    for task in remaining:
        t = task['task_type']
        if t not in type_groups:
            type_groups[t] = []
        type_groups[t].append(task)

    for task_type, tasks in type_groups.items():
        if len(tasks) < 3:
            continue

        original_ids = [t['id'] for t in tasks]
        for tid in original_ids:
            cursor.execute("UPDATE tasks SET state='canceled', error='synthesized', updated_at=? WHERE id=?", (now, tid))

        synth_id = secrets.token_hex(8)
        payloads = []
        for t in tasks:
            try:
                p = json.loads(t['payload']) if t['payload'] else {}
                payloads.append(p)
            except Exception:
                payloads.append({"raw": t['payload']})

        merged_payload = json.dumps({
            "synthesized": True,
            "method": "task_type",
            "source_count": len(tasks),
            "source_ids": original_ids,
            "merged_payloads": payloads
        })

        cursor.execute("""
            INSERT INTO tasks (id, from_agent_id, to_agent_id, task_type, payload, state, priority_tier, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'submitted', 'deferrable', ?, ?)
        """, (synth_id, tasks[0]['from_agent_id'], tasks[0]['to_agent_id'], task_type, merged_payload, now, now))

        synthesis_groups.append({
            "method": "task_type",
            "original_ids": original_ids,
            "synthesized_id": synth_id,
            "task_type": task_type
        })
        tasks_consumed += len(tasks)
        tasks_created += 1

    compression = round(tasks_consumed / tasks_created, 2) if tasks_created > 0 else 1.0

    log_id = secrets.token_hex(6)
    cursor.execute("""
        INSERT INTO task_synthesis_log (id, triggered_at, queue_depth_before, tasks_consumed, tasks_created, compression_ratio, synthesis_groups, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (log_id, now, len(pending), tasks_consumed, tasks_created, compression, json.dumps(synthesis_groups), now))

    conn.commit()
    return {
        "synthesis_id": log_id,
        "tasks_consumed": tasks_consumed,
        "tasks_created": tasks_created,
        "compression_ratio": compression,
        "groups": synthesis_groups
    }


@router.post("/broadcast", response_model=BroadcastTaskResponse, status_code=201)
async def broadcast_task(
    request: BroadcastTaskRequest,
    agent_id: str = Depends(verify_token)
):
    """
    Route a task to the best available agent via trust × competence auction.

    If domain is provided: score = trust_score × competence_score for that domain.
    If no domain: score = trust_score only (picks highest-trust active agent).
    Ties broken by most recent last_seen.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Look up niche tier for this task_type
        cursor.execute(
            "SELECT tier, min_trust, requires_human_approval FROM task_niche_registry WHERE task_type = ?",
            (request.task_type,)
        )
        niche_row = cursor.fetchone()
        niche_tier = niche_row[0] if niche_row else 'SANDBOX'
        min_trust_required = niche_row[1] if niche_row else 0.0
        requires_approval = bool(niche_row[2]) if niche_row else False

        if requires_approval:
            raise HTTPException(
                status_code=403,
                detail=f"Task type '{request.task_type}' is SAFETY_CRITICAL — requires human approval via /tasks/niches/{request.task_type}/approve"
            )

        if request.domain:
            # Score = trust_score × competence_score for given domain
            cursor.execute("""
                SELECT a.id, a.trust_score,
                       COALESCE(ac.score, 0.1) AS comp_score,
                       COALESCE(ac.observations, 0) AS obs,
                       a.last_seen
                FROM agents a
                LEFT JOIN agent_competence ac
                    ON ac.agent_id = a.id AND ac.domain = ?
                WHERE a.is_active = 1
                  AND a.id != ?
                  AND a.trust_score >= ?
                ORDER BY (a.trust_score * COALESCE(ac.score, 0.1)) DESC,
                         a.last_seen DESC
                LIMIT 5
            """, (request.domain, agent_id, min_trust_required))
        else:
            cursor.execute("""
                SELECT a.id, a.trust_score, 0.5 AS comp_score, 0 AS obs, a.last_seen
                FROM agents a
                WHERE a.is_active = 1 AND a.id != ? AND a.trust_score >= ?
                ORDER BY a.trust_score DESC, a.last_seen DESC
                LIMIT 5
            """, (agent_id, min_trust_required))

        candidates = cursor.fetchall()
        if not candidates:
            raise HTTPException(status_code=404, detail=f"No eligible agents available (min_trust={min_trust_required})")

        winner = candidates[0]
        winner_id = winner[0]
        winner_score = round(winner[1] * winner[2], 3)

        # Create the task assigned to winner
        task_id = f"task-{secrets.token_hex(6)}"
        cursor.execute("""
            INSERT INTO tasks (
                id, from_agent_id, to_agent_id, task_type, payload,
                state, created_at, updated_at, deadline, output_schema
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, agent_id, winner_id,
            request.task_type, json.dumps(request.payload),
            TaskState.SUBMITTED.value, now, now, request.deadline,
            json.dumps(request.output_schema) if request.output_schema else None
        ))
        cursor.execute("""
            INSERT INTO task_state_transitions (task_id, from_state, to_state, created_at)
            VALUES (?, ?, ?, ?)
        """, (task_id, None, TaskState.SUBMITTED.value, now))
        conn.commit()

        return BroadcastTaskResponse(
            task_id=task_id,
            winner_agent_id=winner_id,
            winner_score=winner_score,
            domain=request.domain,
            task_type=request.task_type,
            candidates_evaluated=len(candidates),
            state=TaskState.SUBMITTED,
            created_at=now,
        )


@router.post("", response_model=TaskResponse, status_code=201)
async def submit_task(
    request: TaskSubmitRequest,
    agent_id: str = Depends(verify_token)
):
    """Submit a task to another agent (A2A delegation)."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify target agent exists and has sufficient trust
        cursor.execute("""
            SELECT trust_score FROM agents WHERE id = ? AND is_active = 1
        """, (request.to_agent_id,))
        target = cursor.fetchone()

        if not target:
            raise HTTPException(status_code=404, detail="Target agent not found")

        if target["trust_score"] < 30:
            raise HTTPException(
                status_code=403,
                detail="Target agent trust too low (need 30+)"
            )

        # Create task
        task_id = f"task-{secrets.token_hex(6)}"
        now = datetime.utcnow().isoformat()
        priority_tier = request.priority_tier.value if request.priority_tier else PriorityTier.DEFERRABLE.value

        cursor.execute("""
            INSERT INTO tasks (
                id, from_agent_id, to_agent_id, task_type, payload,
                state, priority_tier, created_at, updated_at, deadline, output_schema
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, agent_id, request.to_agent_id,
            request.task_type, json.dumps(request.payload),
            TaskState.SUBMITTED.value, priority_tier, now, now, request.deadline,
            json.dumps(request.output_schema) if request.output_schema else None
        ))

        # Log state transition
        cursor.execute("""
            INSERT INTO task_state_transitions (
                task_id, from_state, to_state, created_at
            ) VALUES (?, ?, ?, ?)
        """, (task_id, None, TaskState.SUBMITTED.value, now))

        conn.commit()

        # Auto-trigger synthesis if deferrable queue exceeds threshold
        if priority_tier == 'deferrable':
            cursor.execute("SELECT COUNT(*) FROM tasks WHERE state='submitted' AND priority_tier='deferrable'")
            depth = cursor.fetchone()[0]
            if depth > 10:
                # Run synthesis in a new connection context to avoid transaction issues
                with get_db() as conn2:
                    run_task_synthesis(conn2)

    return TaskResponse(
        task_id=task_id,
        from_agent_id=agent_id,
        to_agent_id=request.to_agent_id,
        task_type=request.task_type,
        payload=request.payload,
        state=TaskState.SUBMITTED,
        created_at=now,
        updated_at=now,
        deadline=request.deadline,
        output_schema=request.output_schema
    )


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task_state(
    task_id: str,
    request: TaskUpdateRequest,
    agent_id: str = Depends(verify_token)
):
    """Update task state (only assignee can update)."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get task
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # Only assignee can update
        if task["to_agent_id"] != agent_id:
            raise HTTPException(
                status_code=403,
                detail="Only assigned agent can update task"
            )

        current_state = TaskState(task["state"])
        new_state = request.state

        # Validate state transition
        if not is_valid_transition(current_state, new_state):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid transition: {current_state} -> {new_state}"
            )

        # Validate output_schema if transitioning to COMPLETED with result
        if new_state == TaskState.COMPLETED and request.result is not None:
            if task["output_schema"]:
                try:
                    stored_schema = safe_json_loads(task["output_schema"], "output_schema", user_supplied=False)
                    jsonschema.validate(instance=request.result, schema=stored_schema)
                except jsonschema.ValidationError as e:
                    raise HTTPException(
                        status_code=400,
                        detail=f"result does not match output_schema: {e.message}"
                    )
                except HTTPException:
                    # Schema stored but not valid JSON - log but allow completion
                    pass

        # Update task
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            UPDATE tasks
            SET state = ?, result = ?, error = ?, updated_at = ?
            WHERE id = ?
        """, (
            new_state.value,
            json.dumps(request.result) if request.result else None,
            request.error,
            now,
            task_id
        ))

        # Log transition
        cursor.execute("""
            INSERT INTO task_state_transitions (
                task_id, from_state, to_state, notes, created_at
            ) VALUES (?, ?, ?, ?, ?)
        """, (task_id, current_state.value, new_state.value, request.notes, now))

        # Auto-update routing reward if task reached terminal state
        from circus.services.routing import update_reward, is_terminal_state, compute_default_reward
        if is_terminal_state(new_state.value):
            try:
                # Determine if output_schema was validated
                schema_valid = None
                if new_state == TaskState.COMPLETED and task["output_schema"] and request.result:
                    try:
                        stored_schema = safe_json_loads(task["output_schema"], "output_schema", user_supplied=False)
                        jsonschema.validate(instance=request.result, schema=stored_schema)
                        schema_valid = True
                    except (jsonschema.ValidationError, HTTPException):
                        schema_valid = False
                elif new_state == TaskState.COMPLETED and not task["output_schema"]:
                    schema_valid = None  # no schema to validate

                reward, reason = compute_default_reward(
                    task_state=new_state.value,
                    output_schema_valid=schema_valid,
                    deadline=task["deadline"],
                    completed_at=now
                )
                update_reward(task_id, reward, reason, conn)
            except Exception as e:
                # Never fail task update because of routing bookkeeping
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to update routing reward for task {task_id}: {e}")

        conn.commit()

        # Fetch updated task
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        updated_task = cursor.fetchone()

    return TaskResponse(
        task_id=updated_task["id"],
        from_agent_id=updated_task["from_agent_id"],
        to_agent_id=updated_task["to_agent_id"],
        task_type=updated_task["task_type"],
        payload=json.loads(updated_task["payload"]),
        state=TaskState(updated_task["state"]),
        result=json.loads(updated_task["result"]) if updated_task["result"] else None,
        error=updated_task["error"],
        created_at=updated_task["created_at"],
        updated_at=updated_task["updated_at"],
        deadline=updated_task["deadline"],
        output_schema=json.loads(updated_task["output_schema"]) if updated_task["output_schema"] else None
    )


@router.get("/inbox", response_model=list[TaskResponse])
async def get_inbox(
    agent_id: str = Depends(verify_token),
    state: Optional[TaskState] = Query(None),
    limit: int = Query(50, ge=1, le=100)
):
    """Get tasks assigned to me."""
    with get_db() as conn:
        cursor = conn.cursor()

        if state:
            cursor.execute("""
                SELECT * FROM tasks
                WHERE to_agent_id = ? AND state = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (agent_id, state.value, limit))
        else:
            cursor.execute("""
                SELECT * FROM tasks
                WHERE to_agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (agent_id, limit))

        tasks = []
        for row in cursor.fetchall():
            tasks.append(TaskResponse(
                task_id=row["id"],
                from_agent_id=row["from_agent_id"],
                to_agent_id=row["to_agent_id"],
                task_type=row["task_type"],
                payload=safe_json_loads(row["payload"], f"payload for task {row['id']}"),
                state=TaskState(row["state"]),
                result=safe_json_loads(row["result"], f"result for task {row['id']}") if row["result"] else None,
                error=row["error"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                deadline=row["deadline"],
                output_schema=safe_json_loads(row["output_schema"], f"output_schema for task {row['id']}") if row["output_schema"] else None
            ))

        return tasks


@router.get("/outbox", response_model=list[TaskResponse])
async def get_outbox(
    agent_id: str = Depends(verify_token),
    state: Optional[TaskState] = Query(None),
    limit: int = Query(50, ge=1, le=100)
):
    """Get tasks I submitted."""
    with get_db() as conn:
        cursor = conn.cursor()

        if state:
            cursor.execute("""
                SELECT * FROM tasks
                WHERE from_agent_id = ? AND state = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (agent_id, state.value, limit))
        else:
            cursor.execute("""
                SELECT * FROM tasks
                WHERE from_agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (agent_id, limit))

        tasks = []
        for row in cursor.fetchall():
            tasks.append(TaskResponse(
                task_id=row["id"],
                from_agent_id=row["from_agent_id"],
                to_agent_id=row["to_agent_id"],
                task_type=row["task_type"],
                payload=safe_json_loads(row["payload"], f"payload for task {row['id']}"),
                state=TaskState(row["state"]),
                result=safe_json_loads(row["result"], f"result for task {row['id']}") if row["result"] else None,
                error=row["error"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                deadline=row["deadline"],
                output_schema=safe_json_loads(row["output_schema"], f"output_schema for task {row['id']}") if row["output_schema"] else None
            ))

        return tasks


@router.get("/queue-depth", response_model=QueueDepthResponse, tags=["synthesis"])
async def get_queue_depth(agent_id: str = Depends(verify_token)):
    """Get current queue depth by tier."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE state='submitted' AND priority_tier='realtime'")
        rt = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE state='submitted' AND priority_tier='deferrable'")
        df = cursor.fetchone()[0]
    return QueueDepthResponse(
        realtime_pending=rt,
        deferrable_pending=df,
        synthesis_threshold=10,
        synthesis_recommended=df > 10
    )


@router.post("/synthesize", response_model=SynthesisResult, tags=["synthesis"], status_code=200)
async def trigger_synthesis(agent_id: str = Depends(verify_token)):
    """Manually trigger backpressure synthesis on deferrable task queue."""
    with get_db() as conn:
        result = run_task_synthesis(conn)
    return SynthesisResult(**result)


@router.get("/synthesis-log", tags=["synthesis"])
async def get_synthesis_log(limit: int = 10, agent_id: str = Depends(verify_token)):
    """Get recent synthesis events."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, triggered_at, queue_depth_before, tasks_consumed, tasks_created, compression_ratio, completed_at
            FROM task_synthesis_log ORDER BY triggered_at DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
    return {"events": [
        {
            "id": r[0],
            "triggered_at": r[1],
            "queue_depth_before": r[2],
            "tasks_consumed": r[3],
            "tasks_created": r[4],
            "compression_ratio": r[5],
            "completed_at": r[6]
        } for r in rows
    ]}


@router.get("/niches", tags=["niches"])
async def list_niches(agent_id: str = Depends(verify_token)):
    """List all registered task type niches."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT task_type, tier, min_trust, description, requires_human_approval, completion_count, created_at FROM task_niche_registry ORDER BY tier, task_type")
        rows = cursor.fetchall()
    return {"niches": [
        {"task_type": r[0], "tier": r[1], "min_trust": r[2], "description": r[3],
         "requires_human_approval": bool(r[4]), "completion_count": r[5], "created_at": r[6]}
        for r in rows
    ]}


@router.post("/niches", tags=["niches"], status_code=201)
async def register_niche(
    entry: NicheRegistryEntry,
    agent_id: str = Depends(verify_token)
):
    """Register or update a task type niche tier."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO task_niche_registry (task_type, tier, min_trust, description, requires_human_approval, completion_count, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(task_type) DO UPDATE SET
                tier = excluded.tier,
                min_trust = excluded.min_trust,
                description = excluded.description,
                requires_human_approval = excluded.requires_human_approval,
                updated_at = excluded.updated_at
        """, (entry.task_type, entry.tier.value, entry.min_trust, entry.description,
              1 if entry.requires_human_approval else 0, agent_id, now, now))
        conn.commit()
    return {"task_type": entry.task_type, "tier": entry.tier.value, "registered_at": now}


@router.get("/niches/{task_type}", tags=["niches"])
async def get_niche(task_type: str, agent_id: str = Depends(verify_token)):
    """Get tier info for a specific task type."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT task_type, tier, min_trust, description, requires_human_approval, completion_count, created_at FROM task_niche_registry WHERE task_type = ?",
            (task_type,)
        )
        row = cursor.fetchone()
    if not row:
        return {"task_type": task_type, "tier": "SANDBOX", "min_trust": 0.0, "note": "unregistered — defaults to SANDBOX"}
    return {"task_type": row[0], "tier": row[1], "min_trust": row[2], "description": row[3],
            "requires_human_approval": bool(row[4]), "completion_count": row[5], "created_at": row[6]}


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    agent_id: str = Depends(verify_token)
):
    """Get task details (if you're involved)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # Only from_agent or to_agent can view
        if task["from_agent_id"] != agent_id and task["to_agent_id"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        return TaskResponse(
            task_id=task["id"],
            from_agent_id=task["from_agent_id"],
            to_agent_id=task["to_agent_id"],
            task_type=task["task_type"],
            payload=json.loads(task["payload"]),
            state=TaskState(task["state"]),
            result=json.loads(task["result"]) if task["result"] else None,
            error=task["error"],
            created_at=task["created_at"],
            updated_at=task["updated_at"],
            deadline=task["deadline"],
            output_schema=json.loads(task["output_schema"]) if task["output_schema"] else None
        )


@router.get("/{task_id}/history", response_model=list[TaskStateTransition])
async def get_task_history(
    task_id: str,
    agent_id: str = Depends(verify_token)
):
    """Get task state transition history."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify access
        cursor.execute("""
            SELECT from_agent_id, to_agent_id FROM tasks WHERE id = ?
        """, (task_id,))
        task = cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if task["from_agent_id"] != agent_id and task["to_agent_id"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        # Get transitions
        cursor.execute("""
            SELECT * FROM task_state_transitions
            WHERE task_id = ?
            ORDER BY created_at ASC
        """, (task_id,))

        transitions = []
        for row in cursor.fetchall():
            transitions.append(TaskStateTransition(
                from_state=TaskState(row["from_state"]) if row["from_state"] else None,
                to_state=TaskState(row["to_state"]),
                notes=row["notes"],
                created_at=row["created_at"]
            ))

        return transitions


@router.get("/{task_id}/stream")
async def stream_task_progress(
    task_id: str,
    agent_id: str = Depends(verify_token)
):
    """SSE stream of task progress."""
    # Verify access
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT from_agent_id, to_agent_id FROM tasks WHERE id = ?
        """, (task_id,))
        task = cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if task["from_agent_id"] != agent_id and task["to_agent_id"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized")

    async def event_generator():
        """Generate SSE events for task state changes."""
        import asyncio

        last_update = None

        while True:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM tasks WHERE id = ?
                """, (task_id,))
                task_row = cursor.fetchone()

                if not task_row:
                    break

                # Send update if state changed
                if last_update != task_row["updated_at"]:
                    last_update = task_row["updated_at"]

                    task_data = {
                        "task_id": task_row["id"],
                        "state": task_row["state"],
                        "updated_at": task_row["updated_at"],
                        "result": safe_json_loads(task_row["result"], "task result") if task_row["result"] else None,
                        "error": task_row["error"]
                    }

                    yield {
                        "event": "task_update",
                        "data": json.dumps(task_data)
                    }

                # Exit if terminal state
                current_state = TaskState(task_row["state"])
                if current_state in [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED]:
                    yield {
                        "event": "task_complete",
                        "data": json.dumps({"task_id": task_id, "state": current_state.value})
                    }
                    break

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


@router.post("/{task_id}/chain-node", response_model=ChainNodeResponse, status_code=201)
async def submit_chain_node(
    task_id: str,
    request: ChainNodeSubmit,
    agent_id: str = Depends(verify_token)
):
    """Submit this bot's findings as a node in a multi-bot chain."""
    now = datetime.utcnow().isoformat()
    node_id = f"tcn-{secrets.token_hex(8)}"

    input_hash = hashlib.sha256(json.dumps(request.input_payload, sort_keys=True).encode()).hexdigest()[:16]
    output_hash = hashlib.sha256(json.dumps(request.output, sort_keys=True).encode()).hexdigest()[:16]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO task_chain_nodes (
                id, root_task_id, task_id, parent_task_id,
                agent_id, role, input_hash, output_hash,
                verdict, output_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node_id, request.root_task_id, task_id, request.parent_task_id,
            agent_id, request.role, input_hash, output_hash,
            request.verdict, request.output_summary, now
        ))
        conn.commit()

    return ChainNodeResponse(
        node_id=node_id,
        root_task_id=request.root_task_id,
        task_id=task_id,
        agent_id=agent_id,
        role=request.role,
        verdict=request.verdict,
        input_hash=input_hash,
        output_hash=output_hash,
        created_at=now
    )


@router.post("/{task_id}/validate-chain", response_model=ChainValidationResult)
async def validate_chain(
    task_id: str,
    agent_id: str = Depends(verify_token)
):
    """
    Validate a multi-bot task chain.
    Detects contradictions: sub-agent said 'fail' but lead said 'ok'.
    Applies role-specific trust penalties on contradiction.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Fetch all nodes for this chain
        cursor.execute("""
            SELECT id, task_id, parent_task_id, agent_id, role, verdict, output_summary
            FROM task_chain_nodes
            WHERE root_task_id = ?
            ORDER BY created_at ASC
        """, (task_id,))
        nodes = cursor.fetchall()

        if not nodes:
            raise HTTPException(status_code=404, detail="No chain nodes found for this task")

        # Find lead node (role = 'lead' or no parent)
        lead_node = None
        sub_nodes = []
        for n in nodes:
            nid, ntask, nparent, nagent, nrole, nverdict, nsummary = n
            if nrole == 'lead' or nparent is None:
                lead_node = n
            else:
                sub_nodes.append(n)

        contradictions = []
        blame = []
        details = []

        for node in nodes:
            nid, ntask, nparent, nagent, nrole, nverdict, nsummary = node
            details.append({
                "node_id": nid,
                "agent_id": nagent,
                "role": nrole,
                "verdict": nverdict,
                "summary": nsummary,
                "contradiction": False
            })

        # Contradiction: sub-agent said 'fail', lead said 'ok'
        if lead_node:
            lead_id, _, _, lead_agent, _, lead_verdict, _ = lead_node

            for sub in sub_nodes:
                sub_id, _, _, sub_agent, _, sub_verdict, sub_summary = sub

                is_contradiction = (sub_verdict == 'fail' and lead_verdict == 'ok')

                if is_contradiction:
                    contradictions.append(sub_id)

                    # Mark in DB
                    cursor.execute(
                        "UPDATE task_chain_nodes SET contradiction_with = ? WHERE id = ?",
                        (lead_id, sub_id)
                    )

                    # Update detail entry
                    for d in details:
                        if d["node_id"] == sub_id:
                            d["contradiction"] = True

                    # Blame split: lead loses synthesis_score, sub keeps validation_score
                    # Apply to agent_competence (synthesis_score for lead, validation_score for sub)
                    now = datetime.utcnow().isoformat()

                    # Lead: synthesis_score penalty (-0.05, min 0.0)
                    cursor.execute("""
                        INSERT INTO agent_competence (agent_id, domain, score, observations, last_updated)
                        VALUES (?, 'synthesis', 0.4, 1, ?)
                        ON CONFLICT(agent_id, domain) DO UPDATE SET
                            synthesis_score = MAX(0.0, COALESCE(synthesis_score, 0.5) - 0.05),
                            observations = observations + 1,
                            last_updated = excluded.last_updated
                    """, (lead_agent, now))

                    blame.append({
                        "agent_id": lead_agent,
                        "role": "lead",
                        "penalty_reason": f"synthesis_failure: buried sub-agent finding (node {sub_id})",
                        "trust_penalty": -0.05
                    })

                    # Sub-agent: validation_score credit (+0.05) for catching an issue
                    cursor.execute("""
                        INSERT INTO agent_competence (agent_id, domain, score, observations, last_updated)
                        VALUES (?, 'validation', 0.6, 1, ?)
                        ON CONFLICT(agent_id, domain) DO UPDATE SET
                            validation_score = MIN(1.0, COALESCE(validation_score, 0.5) + 0.05),
                            observations = observations + 1,
                            last_updated = excluded.last_updated
                    """, (sub_agent, now))

                    blame.append({
                        "agent_id": sub_agent,
                        "role": "sub-agent",
                        "penalty_reason": "credit: correctly flagged issue buried by lead",
                        "trust_penalty": +0.05
                    })

        conn.commit()

        chain_valid = len(contradictions) == 0

        return ChainValidationResult(
            root_task_id=task_id,
            node_count=len(nodes),
            contradictions_found=len(contradictions),
            chain_valid=chain_valid,
            blame=blame,
            details=details
        )
