"""
DocVault webhook receiver for document review events.
When a human approves/requests-changes/rejects a document in DocVault,
this endpoint receives the notification and creates a task for the agent.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime
import json
import secrets
import logging

from circus.database import get_db

router = APIRouter()
logger = logging.getLogger(__name__)


class DocVaultNotifyRequest(BaseModel):
    """DocVault webhook payload."""
    agentId: str
    event: Literal["approved", "changes_requested", "rejected"]
    docId: str
    docTitle: str
    docType: str
    docProject: str
    reviewer: str
    comment: Optional[str] = None
    docUrl: str


@router.post("/internal/docvault-notify")
def docvault_notify(req: DocVaultNotifyRequest):
    """
    Receive document review notification from DocVault.
    Creates a Circus task for the agent with the review outcome.
    Internal localhost endpoint (no auth).
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify target agent exists
        cursor.execute(
            "SELECT id FROM agents WHERE id = ? AND is_active = 1",
            (req.agentId,)
        )
        agent = cursor.fetchone()

        if not agent:
            # Don't fail if agent not found — log warning and return 200
            # This is important so DocVault doesn't retry infinitely
            logger.warning(
                f"DocVault notification for unknown agent {req.agentId} (doc: {req.docId})"
            )
            return {"received": True, "task_id": None, "warning": "Agent not found"}

        # Map event to task_type
        task_type_map = {
            "approved": "doc_approved",
            "changes_requested": "doc_changes_requested",
            "rejected": "doc_rejected"
        }
        task_type = task_type_map[req.event]

        # Map event to priority_tier
        priority_map = {
            "approved": "immediate",
            "rejected": "immediate",
            "changes_requested": "urgent"
        }
        priority_tier = priority_map[req.event]

        # Create task
        task_id = f"task-{secrets.token_hex(6)}"
        now = datetime.utcnow().isoformat()

        payload = {
            "event": req.event,
            "docId": req.docId,
            "docTitle": req.docTitle,
            "docType": req.docType,
            "docProject": req.docProject,
            "reviewer": req.reviewer,
            "comment": req.comment,
            "docUrl": req.docUrl
        }

        cursor.execute(
            """INSERT INTO tasks (
                id, from_agent_id, to_agent_id, task_type, payload,
                state, priority_tier, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                "docvault-system",
                req.agentId,
                task_type,
                json.dumps(payload),
                "submitted",
                priority_tier,
                now,
                now
            )
        )

        # Log state transition
        cursor.execute(
            """INSERT INTO task_state_transitions (
                task_id, from_state, to_state, created_at
            ) VALUES (?, ?, ?, ?)""",
            (task_id, None, "submitted", now)
        )

        # Fire webhooks for this agent (event: doc_review_complete)
        # Find all webhook subscriptions for this agent that listen to doc_review_complete
        cursor.execute(
            """SELECT id, agent_id, url, secret FROM webhook_subscriptions
               WHERE agent_id = ? AND is_active = 1 AND events LIKE ?""",
            (req.agentId, '%doc_review_complete%')
        )
        subscriptions = cursor.fetchall()

        webhook_payload = {
            "task_id": task_id,
            "event": req.event,
            "doc_title": req.docTitle,
            "doc_url": req.docUrl,
            "reviewer": req.reviewer
        }

        for sub in subscriptions:
            sub_id = sub[0]
            cursor.execute(
                """INSERT INTO webhook_deliveries
                   (subscription_id, event_type, payload, status, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (sub_id, "doc_review_complete", json.dumps(webhook_payload), "pending", now)
            )

        conn.commit()

        logger.info(
            f"DocVault: {req.event} event for {req.agentId} → task {task_id} "
            f"(doc: {req.docTitle}, reviewer: {req.reviewer})"
        )

        return {
            "received": True,
            "task_id": task_id,
            "event": req.event,
            "webhooks_queued": len(subscriptions)
        }
