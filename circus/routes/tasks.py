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
    TaskResponse,
    TaskState,
    TaskStateTransition,
    TaskSubmitRequest,
    TaskUpdateRequest,
)
from circus.routes.agents import verify_token
from circus.services.task_engine import is_valid_transition

router = APIRouter()


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
                ORDER BY (a.trust_score * COALESCE(ac.score, 0.1)) DESC,
                         a.last_seen DESC
                LIMIT 5
            """, (request.domain, agent_id))
        else:
            cursor.execute("""
                SELECT a.id, a.trust_score, 0.5 AS comp_score, 0 AS obs, a.last_seen
                FROM agents a
                WHERE a.is_active = 1 AND a.id != ?
                ORDER BY a.trust_score DESC, a.last_seen DESC
                LIMIT 5
            """, (agent_id,))

        candidates = cursor.fetchall()
        if not candidates:
            raise HTTPException(status_code=404, detail="No eligible agents available")

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

        cursor.execute("""
            INSERT INTO tasks (
                id, from_agent_id, to_agent_id, task_type, payload,
                state, created_at, updated_at, deadline, output_schema
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, agent_id, request.to_agent_id,
            request.task_type, json.dumps(request.payload),
            TaskState.SUBMITTED.value, now, now, request.deadline,
            json.dumps(request.output_schema) if request.output_schema else None
        ))

        # Log state transition
        cursor.execute("""
            INSERT INTO task_state_transitions (
                task_id, from_state, to_state, created_at
            ) VALUES (?, ?, ?, ?)
        """, (task_id, None, TaskState.SUBMITTED.value, now))

        conn.commit()

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
