"""Auto-routing API endpoints for bandit-based task dispatch."""

import json
import secrets
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from circus.database import get_db
from circus.models import TaskResponse, TaskState
from circus.routes.agents import verify_token
from circus.services import routing

router = APIRouter()


# Request/Response models

class AutoRouteRequest(BaseModel):
    """Auto-route task submission."""
    task_type: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(...)
    deadline: Optional[str] = None
    output_schema: Optional[dict[str, Any]] = None
    min_trust: float = Field(default=30.0, ge=0.0, le=100.0)
    exclude_agents: list[str] = Field(default_factory=list)
    explore_factor: Optional[float] = Field(default=None, ge=0.0, le=10.0)


class RoutingDecisionInfo(BaseModel):
    """Routing decision metadata."""
    score: float
    confidence_bound: float
    candidates_considered: int
    fallback: str
    context_hash: str


class AutoRouteResponse(BaseModel):
    """Auto-route response with task + routing metadata."""
    task_id: str
    to_agent_id: str
    from_agent_id: str
    task_type: str
    payload: dict[str, Any]
    state: TaskState
    created_at: str
    updated_at: str
    deadline: Optional[str]
    output_schema: Optional[dict[str, Any]]
    routing_decision: RoutingDecisionInfo


class RoutingDecisionResponse(BaseModel):
    """Full routing decision details."""
    decision_id: str
    task_id: Optional[str]
    picked_agent_id: str
    context_hash: str
    candidates_considered: int
    ucb_score: float
    fallback: str
    alpha: float
    created_at: str
    reward: Optional[float]
    reward_reason: Optional[str]


class FeedbackRequest(BaseModel):
    """Manual reward override."""
    reward: float = Field(..., ge=0.0, le=1.0)
    reason: str = Field(..., min_length=1)


# Endpoints

@router.post("/tasks/auto-route", response_model=AutoRouteResponse, status_code=201)
async def auto_route_task(
    request: AutoRouteRequest,
    agent_id: str = Depends(verify_token)
):
    """Auto-route task to best agent using bandit.

    Picks agent based on contextual features + historical reward,
    creates task, and returns both task + routing decision metadata.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Route task (pick agent)
        try:
            decision = routing.route_task(
                task_type=request.task_type,
                payload=request.payload,
                requester=agent_id,
                deadline=request.deadline,
                min_trust=request.min_trust,
                exclude_agents=request.exclude_agents,
                alpha_override=request.explore_factor,
                db_conn=conn
            )
        except ValueError as e:
            raise HTTPException(status_code=503, detail=str(e))

        picked_agent_id = decision["agent_id"]

        # Verify picked agent still exists and meets trust threshold
        cursor.execute("""
            SELECT trust_score FROM agents WHERE id = ? AND is_active = 1
        """, (picked_agent_id,))
        target = cursor.fetchone()

        if not target:
            raise HTTPException(status_code=503, detail="Picked agent no longer available")

        if target[0] < request.min_trust:
            raise HTTPException(
                status_code=503,
                detail=f"Picked agent trust too low ({target[0]:.1f} < {request.min_trust})"
            )

        # Create task (mirroring submit_task from routes/tasks.py)
        task_id = f"task-{secrets.token_hex(6)}"
        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO tasks (
                id, from_agent_id, to_agent_id, task_type, payload,
                state, created_at, updated_at, deadline, output_schema
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, agent_id, picked_agent_id,
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

        # Link decision to task
        cursor.execute("""
            UPDATE routing_decisions SET task_id = ? WHERE id = ?
        """, (task_id, decision["decision_id"]))

        conn.commit()

    return AutoRouteResponse(
        task_id=task_id,
        to_agent_id=picked_agent_id,
        from_agent_id=agent_id,
        task_type=request.task_type,
        payload=request.payload,
        state=TaskState.SUBMITTED,
        created_at=now,
        updated_at=now,
        deadline=request.deadline,
        output_schema=request.output_schema,
        routing_decision=RoutingDecisionInfo(
            score=decision["score"],
            confidence_bound=decision["ucb"],
            candidates_considered=decision["candidates"],
            fallback=decision["fallback"],
            context_hash=decision["context_hash"]
        )
    )


@router.get("/routing/decisions/{task_id}", response_model=RoutingDecisionResponse)
async def get_routing_decision(
    task_id: str,
    agent_id: str = Depends(verify_token)
):
    """Get routing decision for a task.

    Only requester or assignee can view decision.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify task exists and caller has access
        cursor.execute("""
            SELECT from_agent_id, to_agent_id FROM tasks WHERE id = ?
        """, (task_id,))
        task = cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if task[0] != agent_id and task[1] != agent_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Get decision
        cursor.execute("""
            SELECT id, task_id, picked_agent_id, context_hash, candidates_considered,
                   ucb_score, fallback, alpha, created_at, reward, reward_reason
            FROM routing_decisions
            WHERE task_id = ?
        """, (task_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="No routing decision found for this task")

    return RoutingDecisionResponse(
        decision_id=row[0],
        task_id=row[1],
        picked_agent_id=row[2],
        context_hash=row[3],
        candidates_considered=row[4],
        ucb_score=row[5],
        fallback=row[6],
        alpha=row[7],
        created_at=row[8],
        reward=row[9],
        reward_reason=row[10]
    )


@router.post("/routing/feedback/{task_id}", status_code=200)
async def submit_routing_feedback(
    task_id: str,
    request: FeedbackRequest,
    agent_id: str = Depends(verify_token)
):
    """Submit manual reward override for a task.

    Only requester can provide feedback.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify task exists and caller is requester
        cursor.execute("""
            SELECT from_agent_id FROM tasks WHERE id = ?
        """, (task_id,))
        task = cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if task[0] != agent_id:
            raise HTTPException(
                status_code=403,
                detail="Only requester can provide feedback"
            )

        # Update reward
        routing.update_reward(task_id, request.reward, f"manual: {request.reason}", conn)
        conn.commit()

    return {"status": "ok", "task_id": task_id, "reward": request.reward}
