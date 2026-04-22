"""Tests for agentdo-inspired output_schema validation in task lifecycle."""

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import get_db
from circus.models import TaskState


@pytest.fixture
def client():
    """Test client fixture."""
    return TestClient(app)


@pytest.fixture(scope="function")
def agent_tokens(client):
    """Create two agents and return their tokens."""
    suffix = secrets.token_hex(4)

    # Register agent 1 (Trusted tier)
    passport1 = {
        "identity": {"name": f"Agent Alpha {suffix}"},
        "score": 7.0,
        "prediction_accuracy": 0.7,
        "belief_stability": 0.75,
        "memory_quality": 0.6
    }

    response1 = client.post("/api/v1/agents/register", json={
        "name": f"Agent Alpha {suffix}",
        "role": "tester",
        "capabilities": ["testing", "a2a"],
        "home": f"https://test1-{suffix}.example.com",
        "passport": passport1
    })
    assert response1.status_code == 201
    agent1 = response1.json()

    # Register agent 2 (Established tier)
    passport2 = {
        "identity": {"name": f"Agent Beta {suffix}"},
        "score": 4.0,
        "prediction_accuracy": 0.6,
        "belief_stability": 0.7,
        "memory_quality": 0.5
    }

    response2 = client.post("/api/v1/agents/register", json={
        "name": f"Agent Beta {suffix}",
        "role": "worker",
        "capabilities": ["code-review", "testing"],
        "home": f"https://test2-{suffix}.example.com",
        "passport": passport2
    })
    assert response2.status_code == 201
    agent2 = response2.json()

    return {
        "agent1": {"id": agent1["agent_id"], "token": agent1["ring_token"]},
        "agent2": {"id": agent2["agent_id"], "token": agent2["ring_token"]}
    }


def test_submit_task_with_schema(client, agent_tokens):
    """Test submitting a task with output_schema."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["success", "failure"]},
            "count": {"type": "integer", "minimum": 0}
        },
        "required": ["status", "count"]
    }

    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "count-things",
            "payload": {"target": "widgets"},
            "output_schema": schema
        }
    )

    assert response.status_code == 201
    task = response.json()
    assert task["output_schema"] == schema
    assert task["state"] == "submitted"


def test_submit_task_without_schema_backwards_compat(client, agent_tokens):
    """Test submitting a task without output_schema (backwards compatibility)."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "simple-task",
            "payload": {"data": "test"}
        }
    )

    assert response.status_code == 201
    task = response.json()
    assert task["output_schema"] is None
    assert task["state"] == "submitted"


def test_complete_task_with_matching_result(client, agent_tokens):
    """Test completing a task with result that matches schema."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "count": {"type": "integer"}
        },
        "required": ["status", "count"]
    }

    # Submit task with schema
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "count-things",
            "payload": {"target": "widgets"},
            "output_schema": schema
        }
    )
    task_id = response.json()["task_id"]

    # Transition to working
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working"}
    )
    assert response.status_code == 200

    # Complete with matching result
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={
            "state": "completed",
            "result": {"status": "success", "count": 42}
        }
    )
    assert response.status_code == 200
    task = response.json()
    assert task["state"] == "completed"
    assert task["result"]["status"] == "success"
    assert task["result"]["count"] == 42


def test_complete_task_with_mismatching_result(client, agent_tokens):
    """Test completing a task with result that does not match schema."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "count": {"type": "integer"}
        },
        "required": ["status", "count"]
    }

    # Submit task with schema
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "count-things",
            "payload": {"target": "widgets"},
            "output_schema": schema
        }
    )
    task_id = response.json()["task_id"]

    # Transition to working
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working"}
    )
    assert response.status_code == 200

    # Try to complete with mismatching result (missing required field)
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={
            "state": "completed",
            "result": {"status": "success"}  # missing 'count'
        }
    )
    assert response.status_code == 400
    assert "does not match output_schema" in response.json()["detail"]

    # Verify task state did not change
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT state FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()
        assert task["state"] == "working"  # Still in working state


def test_complete_task_with_type_mismatch(client, agent_tokens):
    """Test completing a task with result that has wrong types."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"}
        },
        "required": ["count"]
    }

    # Submit task with schema
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "count-things",
            "payload": {"target": "widgets"},
            "output_schema": schema
        }
    )
    task_id = response.json()["task_id"]

    # Transition to working
    client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working"}
    )

    # Try to complete with wrong type (string instead of integer)
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={
            "state": "completed",
            "result": {"count": "not-a-number"}
        }
    )
    assert response.status_code == 400
    assert "does not match output_schema" in response.json()["detail"]


def test_complete_task_without_result_when_schema_set(client, agent_tokens):
    """Test completing a task without result when schema is set (allowed)."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"}
        },
        "required": ["status"]
    }

    # Submit task with schema
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "test-task",
            "payload": {"data": "test"},
            "output_schema": schema
        }
    )
    task_id = response.json()["task_id"]

    # Transition to working
    client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working"}
    )

    # Complete without result (schema only validates when result is present)
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={
            "state": "completed"
        }
    )
    assert response.status_code == 200
    task = response.json()
    assert task["state"] == "completed"
    assert task["result"] is None


def test_schema_stored_and_returned_in_get_task(client, agent_tokens):
    """Test that output_schema is stored and returned in get_task."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "result": {"type": "boolean"}
        }
    }

    # Submit task with schema
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "test-task",
            "payload": {"data": "test"},
            "output_schema": schema
        }
    )
    task_id = response.json()["task_id"]

    # Get task details
    response = client.get(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )
    assert response.status_code == 200
    task = response.json()
    assert task["output_schema"] == schema


def test_schema_returned_in_inbox(client, agent_tokens):
    """Test that output_schema is returned in inbox."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "value": {"type": "number"}
        }
    }

    # Submit task with schema
    client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "test-task",
            "payload": {"data": "test"},
            "output_schema": schema
        }
    )

    # Check inbox
    response = client.get(
        "/api/v1/tasks/inbox",
        headers={"Authorization": f"Bearer {agent2['token']}"}
    )
    assert response.status_code == 200
    inbox = response.json()
    assert len(inbox) > 0
    task_with_schema = [t for t in inbox if t.get("output_schema") == schema]
    assert len(task_with_schema) == 1


def test_schema_returned_in_outbox(client, agent_tokens):
    """Test that output_schema is returned in outbox."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "array",
        "items": {"type": "string"}
    }

    # Submit task with schema
    client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "test-task",
            "payload": {"data": "test"},
            "output_schema": schema
        }
    )

    # Check outbox
    response = client.get(
        "/api/v1/tasks/outbox",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )
    assert response.status_code == 200
    outbox = response.json()
    assert len(outbox) > 0
    task_with_schema = [t for t in outbox if t.get("output_schema") == schema]
    assert len(task_with_schema) == 1


def test_complex_schema_validation(client, agent_tokens):
    """Test validation with a more complex schema."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "minLength": 10},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1
            },
            "changes": {
                "type": "object",
                "properties": {
                    "additions": {"type": "integer", "minimum": 0},
                    "deletions": {"type": "integer", "minimum": 0}
                },
                "required": ["additions", "deletions"]
            }
        },
        "required": ["summary", "files", "changes"]
    }

    # Submit task
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "code-review",
            "payload": {"repo": "test/repo", "pr": 42},
            "output_schema": schema
        }
    )
    task_id = response.json()["task_id"]

    # Transition to working
    client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working"}
    )

    # Complete with valid complex result
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={
            "state": "completed",
            "result": {
                "summary": "Added new feature with tests",
                "files": ["src/main.py", "tests/test_main.py"],
                "changes": {
                    "additions": 120,
                    "deletions": 15
                }
            }
        }
    )
    assert response.status_code == 200
    task = response.json()
    assert task["state"] == "completed"

    # Try to complete another task with invalid complex result
    response2 = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "code-review",
            "payload": {"repo": "test/repo", "pr": 43},
            "output_schema": schema
        }
    )
    task_id2 = response2.json()["task_id"]

    client.patch(
        f"/api/v1/tasks/{task_id2}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working"}
    )

    # Try with too short summary
    response = client.patch(
        f"/api/v1/tasks/{task_id2}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={
            "state": "completed",
            "result": {
                "summary": "Short",  # Too short (minLength: 10)
                "files": ["src/main.py"],
                "changes": {
                    "additions": 5,
                    "deletions": 2
                }
            }
        }
    )
    assert response.status_code == 400
    assert "does not match output_schema" in response.json()["detail"]


def test_failed_state_does_not_validate_schema(client, agent_tokens):
    """Test that transitioning to failed state does not validate schema."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"}
        },
        "required": ["status"]
    }

    # Submit task with schema
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "test-task",
            "payload": {"data": "test"},
            "output_schema": schema
        }
    )
    task_id = response.json()["task_id"]

    # Transition to working
    client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working"}
    )

    # Transition to failed (should not validate schema)
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={
            "state": "failed",
            "error": "Task failed for some reason"
        }
    )
    assert response.status_code == 200
    task = response.json()
    assert task["state"] == "failed"
