"""A2A task lifecycle routes."""

import json
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from circus.database import get_db
from circus.models import (
    TaskResponse,
    TaskState,
    TaskStateTransition,
    TaskSubmitRequest,
    TaskUpdateRequest,
)
from circus.routes.agents import verify_token
from circus.services.task_engine import is_valid_transition

router = APIRouter()


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
                state, created_at, updated_at, deadline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, agent_id, request.to_agent_id,
            request.task_type, json.dumps(request.payload),
            TaskState.SUBMITTED.value, now, now, request.deadline
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
        deadline=request.deadline
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
        deadline=updated_task["deadline"]
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
                payload=json.loads(row["payload"]),
                state=TaskState(row["state"]),
                result=json.loads(row["result"]) if row["result"] else None,
                error=row["error"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                deadline=row["deadline"]
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
                payload=json.loads(row["payload"]),
                state=TaskState(row["state"]),
                result=json.loads(row["result"]) if row["result"] else None,
                error=row["error"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                deadline=row["deadline"]
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
            deadline=task["deadline"]
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
                        "result": json.loads(task_row["result"]) if task_row["result"] else None,
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
