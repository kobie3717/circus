"""Tests for routing API endpoints (circus/routes/routing.py)."""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import get_db

client = TestClient(app)


@pytest.fixture
def auth_agents():
    """Create test agents and return auth tokens."""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Create requester (use INSERT OR REPLACE to handle test isolation issues)
        cursor.execute("""
            INSERT OR REPLACE INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("requester", "Requester", "user", json.dumps(["query"]), "local", "hash", "hash", 60.0, 1, now, now))

        # Create worker
        cursor.execute("""
            INSERT OR REPLACE INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("worker", "Worker", "coder", json.dumps(["code", "test"]), "local", "hash", "hash", 75.0, 1, now, now))

        conn.commit()

    # Generate tokens
    from circus.routes.agents import create_access_token
    from datetime import timedelta

    requester_token = create_access_token("requester", timedelta(hours=1))
    worker_token = create_access_token("worker", timedelta(hours=1))

    return {
        "requester": f"Bearer {requester_token}",
        "worker": f"Bearer {worker_token}"
    }


def test_auto_route_creates_task(auth_agents):
    """POST /tasks/auto-route should create task and return routing decision."""
    response = client.post(
        "/api/v1/tasks/auto-route",
        json={
            "task_type": "code",
            "payload": {"description": "implement feature X"},
            "deadline": "2026-05-01T12:00:00Z",
            "min_trust": 50.0
        },
        headers={"Authorization": auth_agents["requester"]}
    )

    assert response.status_code == 201
    data = response.json()

    assert "task_id" in data
    assert data["to_agent_id"] is not None  # Some agent was picked
    assert data["from_agent_id"] == "requester"
    assert data["task_type"] == "code"
    assert data["state"] == "submitted"

    # Check routing_decision metadata
    assert "routing_decision" in data
    routing_info = data["routing_decision"]
    assert "score" in routing_info
    assert "candidates_considered" in routing_info
    assert routing_info["candidates_considered"] >= 1
    assert routing_info["fallback"] in ["bandit", "semantic"]


def test_auto_route_requires_auth(auth_agents):
    """POST /tasks/auto-route should require valid token."""
    response = client.post(
        "/api/v1/tasks/auto-route",
        json={
            "task_type": "code",
            "payload": {"x": 1},
        }
    )

    assert response.status_code == 422  # No auth header


def test_auto_route_no_candidates_returns_503(auth_agents):
    """POST /tasks/auto-route should return 503 if no matching agents."""
    response = client.post(
        "/api/v1/tasks/auto-route",
        json={
            "task_type": "nonexistent-capability",
            "payload": {"x": 1},
        },
        headers={"Authorization": auth_agents["requester"]}
    )

    assert response.status_code == 503
    assert "No agents found" in response.json()["detail"]


def test_auto_route_with_exclude_agents(auth_agents):
    """POST /tasks/auto-route should respect exclude_agents."""
    # Create a task to see which agent gets picked
    response1 = client.post(
        "/api/v1/tasks/auto-route",
        json={
            "task_type": "code",
            "payload": {"x": 1}
        },
        headers={"Authorization": auth_agents["requester"]}
    )
    assert response1.status_code == 201
    picked_agent = response1.json()["to_agent_id"]

    # Now exclude that agent and see if a different one is picked or 503
    response2 = client.post(
        "/api/v1/tasks/auto-route",
        json={
            "task_type": "code",
            "payload": {"x": 1},
            "exclude_agents": [picked_agent]
        },
        headers={"Authorization": auth_agents["requester"]}
    )

    # Either 503 (no other agents) or 201 with different agent
    assert response2.status_code in [201, 503]
    if response2.status_code == 201:
        assert response2.json()["to_agent_id"] != picked_agent


def test_auto_route_with_min_trust(auth_agents):
    """POST /tasks/auto-route should filter by min_trust."""
    # Worker has trust_score=75, so min_trust=80 should exclude it (if it's the only one)
    response = client.post(
        "/api/v1/tasks/auto-route",
        json={
            "task_type": "code",
            "payload": {"x": 1},
            "min_trust": 80.0
        },
        headers={"Authorization": auth_agents["requester"]}
    )

    # Either 503 (no agents with trust>=80) or 201 if there's a high-trust agent
    assert response.status_code in [201, 503]
    if response.status_code == 201:
        # Verify picked agent has high trust
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trust_score FROM agents WHERE id = ?", (response.json()["to_agent_id"],))
            trust = cursor.fetchone()[0]
            assert trust >= 80.0


def test_auto_route_with_output_schema(auth_agents):
    """POST /tasks/auto-route should accept output_schema."""
    response = client.post(
        "/api/v1/tasks/auto-route",
        json={
            "task_type": "code",
            "payload": {"x": 1},
            "output_schema": {
                "type": "object",
                "properties": {"result": {"type": "string"}}
            }
        },
        headers={"Authorization": auth_agents["requester"]}
    )

    assert response.status_code == 201
    data = response.json()
    assert data["output_schema"]["type"] == "object"


def test_get_routing_decision_returns_details(auth_agents):
    """GET /routing/decisions/:task_id should return decision details."""
    # First, create a routed task
    create_resp = client.post(
        "/api/v1/tasks/auto-route",
        json={
            "task_type": "code",
            "payload": {"x": 1}
        },
        headers={"Authorization": auth_agents["requester"]}
    )
    assert create_resp.status_code == 201
    task_id = create_resp.json()["task_id"]

    # Now fetch decision
    response = client.get(
        f"/api/v1/routing/decisions/{task_id}",
        headers={"Authorization": auth_agents["requester"]}
    )

    assert response.status_code == 200
    data = response.json()

    assert data["task_id"] == task_id
    assert data["picked_agent_id"] is not None
    assert "decision_id" in data
    assert "context_hash" in data
    assert data["fallback"] in ["bandit", "semantic"]
    assert data["reward"] is None  # Not completed yet


def test_get_routing_decision_requires_access(auth_agents):
    """GET /routing/decisions/:task_id should only allow requester/assignee."""
    # Create third agent
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("outsider", "Outsider", "spy", json.dumps(["snoop"]), "local", "hash", "hash", 30.0, 1, now, now))
        conn.commit()

    from circus.routes.agents import create_access_token
    from datetime import timedelta
    outsider_token = create_access_token("outsider", timedelta(hours=1))

    # Create task
    create_resp = client.post(
        "/api/v1/tasks/auto-route",
        json={"task_type": "code", "payload": {"x": 1}},
        headers={"Authorization": auth_agents["requester"]}
    )
    task_id = create_resp.json()["task_id"]

    # Outsider tries to view decision
    response = client.get(
        f"/api/v1/routing/decisions/{task_id}",
        headers={"Authorization": f"Bearer {outsider_token}"}
    )

    assert response.status_code == 403


def test_get_routing_decision_worker_can_view(auth_agents):
    """GET /routing/decisions/:task_id should allow assignee to view."""
    # Create task
    create_resp = client.post(
        "/api/v1/tasks/auto-route",
        json={"task_type": "code", "payload": {"x": 1}},
        headers={"Authorization": auth_agents["requester"]}
    )
    task_id = create_resp.json()["task_id"]
    assigned_to = create_resp.json()["to_agent_id"]

    # If assigned to "worker", worker should be able to view
    if assigned_to == "worker":
        response = client.get(
            f"/api/v1/routing/decisions/{task_id}",
            headers={"Authorization": auth_agents["worker"]}
        )
        assert response.status_code == 200
    else:
        # If assigned to someone else, requester can still view
        response = client.get(
            f"/api/v1/routing/decisions/{task_id}",
            headers={"Authorization": auth_agents["requester"]}
        )
        assert response.status_code == 200


def test_submit_feedback_updates_reward(auth_agents):
    """POST /routing/feedback/:task_id should update reward."""
    # Create task
    create_resp = client.post(
        "/api/v1/tasks/auto-route",
        json={"task_type": "code", "payload": {"x": 1}},
        headers={"Authorization": auth_agents["requester"]}
    )
    task_id = create_resp.json()["task_id"]

    # Submit feedback
    response = client.post(
        f"/api/v1/routing/feedback/{task_id}",
        json={
            "reward": 0.95,
            "reason": "excellent work"
        },
        headers={"Authorization": auth_agents["requester"]}
    )

    assert response.status_code == 200
    assert response.json()["reward"] == 0.95

    # Verify reward was recorded
    decision_resp = client.get(
        f"/api/v1/routing/decisions/{task_id}",
        headers={"Authorization": auth_agents["requester"]}
    )
    decision = decision_resp.json()
    assert decision["reward"] == 0.95
    assert "excellent work" in decision["reward_reason"]


def test_submit_feedback_only_requester(auth_agents):
    """POST /routing/feedback/:task_id should only allow requester."""
    # Create task
    create_resp = client.post(
        "/api/v1/tasks/auto-route",
        json={"task_type": "code", "payload": {"x": 1}},
        headers={"Authorization": auth_agents["requester"]}
    )
    task_id = create_resp.json()["task_id"]

    # Worker tries to submit feedback (should be denied)
    response = client.post(
        f"/api/v1/routing/feedback/{task_id}",
        json={"reward": 0.5, "reason": "meh"},
        headers={"Authorization": auth_agents["worker"]}
    )

    assert response.status_code == 403


def test_submit_feedback_validates_reward_range(auth_agents):
    """POST /routing/feedback/:task_id should validate reward in [0,1]."""
    # Create task
    create_resp = client.post(
        "/api/v1/tasks/auto-route",
        json={"task_type": "code", "payload": {"x": 1}},
        headers={"Authorization": auth_agents["requester"]}
    )
    task_id = create_resp.json()["task_id"]

    # Try invalid reward
    response = client.post(
        f"/api/v1/routing/feedback/{task_id}",
        json={"reward": 1.5, "reason": "too high"},
        headers={"Authorization": auth_agents["requester"]}
    )

    assert response.status_code == 422


def test_routing_decision_not_found_for_manual_task():
    """GET /routing/decisions/:task_id should 404 for non-routed tasks."""
    # Create manual task
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("manual-user", "Manual", "user", json.dumps(["work"]), "local", "hash", "hash", 50.0, 1, now, now))

        cursor.execute("""
            INSERT INTO tasks (id, from_agent_id, to_agent_id, task_type, payload, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("manual-task", "manual-user", "manual-user", "work", json.dumps({}), "submitted", now, now))

        conn.commit()

    from circus.routes.agents import create_access_token
    from datetime import timedelta
    token = create_access_token("manual-user", timedelta(hours=1))

    response = client.get(
        "/api/v1/routing/decisions/manual-task",
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 404
    assert "No routing decision" in response.json()["detail"]
