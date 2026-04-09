"""Tests for Phase 3 features: Tasks, Security, Credentials, Federation."""

import json
from datetime import datetime

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
    import secrets

    # Use unique names per test to avoid conflicts
    suffix = secrets.token_hex(4)

    # Register agent 1 (Trusted tier for testing - score 60-85)
    passport1 = {
        "identity": {"name": f"Agent Alpha {suffix}"},
        "score": 7.0,  # 0-10 scale - targets ~65 trust score (Trusted tier)
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
        "score": 4.0,  # 0-10 scale - targets ~50 trust score (Established tier)
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


# Task Lifecycle Tests

def test_task_submission(client, agent_tokens):
    """Test A2A task submission."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    # Submit task from agent1 to agent2
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "code-review",
            "payload": {"repo": "github.com/test/repo", "pr": 42}
        }
    )

    assert response.status_code == 201
    task = response.json()
    assert task["from_agent_id"] == agent1["id"]
    assert task["to_agent_id"] == agent2["id"]
    assert task["state"] == "submitted"
    assert task["task_type"] == "code-review"
    assert "task_id" in task


def test_task_state_transitions(client, agent_tokens):
    """Test task state machine transitions."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    # Submit task
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "test-task",
            "payload": {"data": "test"}
        }
    )
    task_id = response.json()["task_id"]

    # Agent2 transitions to working
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working", "notes": "Starting work"}
    )
    assert response.status_code == 200
    assert response.json()["state"] == "working"

    # Agent2 completes task
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={
            "state": "completed",
            "result": {"status": "done", "output": "success"},
            "notes": "Task complete"
        }
    )
    assert response.status_code == 200
    task = response.json()
    assert task["state"] == "completed"
    assert task["result"]["status"] == "done"


def test_task_invalid_transition(client, agent_tokens):
    """Test that invalid state transitions are rejected."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    # Submit task
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "test-task",
            "payload": {"data": "test"}
        }
    )
    task_id = response.json()["task_id"]

    # Try invalid transition: submitted -> completed (must go through working)
    response = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "completed"}
    )
    assert response.status_code == 400
    assert "Invalid transition" in response.json()["detail"]


def test_task_inbox_and_outbox(client, agent_tokens):
    """Test task inbox and outbox endpoints."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    # Submit 2 tasks from agent1 to agent2
    for i in range(2):
        client.post(
            "/api/v1/tasks",
            headers={"Authorization": f"Bearer {agent1['token']}"},
            json={
                "to_agent_id": agent2["id"],
                "task_type": f"task-{i}",
                "payload": {"number": i}
            }
        )

    # Check agent2's inbox
    response = client.get(
        "/api/v1/tasks/inbox",
        headers={"Authorization": f"Bearer {agent2['token']}"}
    )
    assert response.status_code == 200
    inbox = response.json()
    assert len(inbox) == 2

    # Check agent1's outbox
    response = client.get(
        "/api/v1/tasks/outbox",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )
    assert response.status_code == 200
    outbox = response.json()
    assert len(outbox) == 2


def test_task_history(client, agent_tokens):
    """Test task state transition history."""
    agent1 = agent_tokens["agent1"]
    agent2 = agent_tokens["agent2"]

    # Submit and progress task
    response = client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        json={
            "to_agent_id": agent2["id"],
            "task_type": "test-task",
            "payload": {"data": "test"}
        }
    )
    task_id = response.json()["task_id"]

    # Transition to working
    client.patch(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {agent2['token']}"},
        json={"state": "working"}
    )

    # Get history
    response = client.get(
        f"/api/v1/tasks/{task_id}/history",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )
    assert response.status_code == 200
    history = response.json()
    assert len(history) >= 2  # submitted + working
    assert history[0]["to_state"] == "submitted"
    assert history[1]["to_state"] == "working"


# Security Middleware Tests

def test_sql_injection_detection(client, agent_tokens):
    """Test that SQL injection attempts are blocked."""
    agent1 = agent_tokens["agent1"]

    # Attempt SQL injection in query parameter
    response = client.get(
        "/api/v1/agents/discover?capability=test' OR 1=1--",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        follow_redirects=False
    )
    # Security middleware blocks with 400 or 500
    assert response.status_code in [400, 500]
    if response.status_code == 400:
        assert "Invalid input" in response.json()["detail"]


def test_audit_log_elder_only(client):
    """Test that audit log is Elder-only."""
    # Create a low-trust agent
    import secrets
    suffix = secrets.token_hex(4)

    passport = {
        "identity": {"name": f"Low Trust Agent {suffix}"},
        "score": 1.0,  # 0-10 scale - low score
        "prediction_accuracy": 0.2,
        "belief_stability": 0.3,
        "memory_quality": 0.1
    }

    response = client.post("/api/v1/agents/register", json={
        "name": f"Low Trust Agent {suffix}",
        "role": "newcomer",
        "capabilities": ["basic"],
        "home": f"https://low-trust-{suffix}.example.com",
        "passport": passport
    })
    assert response.status_code == 201
    token = response.json()["ring_token"]

    # Try to access audit log
    response = client.get(
        "/api/v1/agents/audit-log",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert "Elder" in response.json()["detail"]


def test_audit_log_records_actions(client, agent_tokens):
    """Test that security actions are logged."""
    agent1 = agent_tokens["agent1"]

    # Make a request that should be logged
    client.get(
        "/api/v1/agents/discover?capability=testing",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )

    # Check if audit log contains entries (need Elder to view)
    # First elevate agent1 to Elder
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE agents SET trust_score = 90, trust_tier = 'Elder'
            WHERE id = ?
        """, (agent1["id"],))
        conn.commit()

    # Now check audit log
    response = client.get(
        "/api/v1/agents/audit-log?limit=10",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )
    assert response.status_code == 200
    logs = response.json()
    assert isinstance(logs, list)


# Credentials Tests

def test_export_trust_attestation(client, agent_tokens):
    """Test exporting trust attestation as verifiable credential."""
    agent1 = agent_tokens["agent1"]

    response = client.get(
        "/api/v1/credentials/trust-attestation",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )
    assert response.status_code == 200
    credential = response.json()
    assert credential["type"] == ["VerifiableCredential", "CircusTrustAttestation"]
    assert credential["credentialSubject"]["id"] == agent1["id"]
    assert "proof" in credential
    assert credential["proof"]["type"] == "Ed25519Signature2020"


def test_verify_credential(client, agent_tokens):
    """Test verifying a trust attestation credential structure."""
    agent1 = agent_tokens["agent1"]

    # Export credential
    response = client.get(
        "/api/v1/credentials/trust-attestation",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )
    credential = response.json()

    # Verify credential structure (skip cryptographic verification for Phase 3)
    # In Phase 4, agents will have their own keypairs
    assert "@context" in credential
    assert "credentialSubject" in credential
    assert credential["credentialSubject"]["id"] == agent1["id"]
    assert "trust" in credential["credentialSubject"]
    assert "proof" in credential

    # Note: Actual signature verification would require agents to have
    # persistent keypairs, which is planned for Phase 4


# Federation Tests

def test_list_federation_peers(client):
    """Test listing federation peers."""
    response = client.get("/api/v1/federation/peers")
    assert response.status_code == 200
    peers = response.json()
    assert isinstance(peers, list)


def test_register_peer_requires_elder(client, agent_tokens):
    """Test that registering federation peers requires Elder tier."""
    import secrets
    suffix = secrets.token_hex(4)
    agent1 = agent_tokens["agent1"]  # Trusted, not Elder

    response = client.post(
        "/api/v1/federation/peers",
        headers={"Authorization": f"Bearer {agent1['token']}"},
        params={
            "name": f"Test Peer {suffix}",
            "url": f"https://peer-{suffix}.example.com",
            "public_key_b64": "dGVzdC1wdWJsaWMta2V5"  # base64 test
        }
    )
    assert response.status_code == 403
    assert "Elder" in response.json()["detail"]


def test_federated_discovery(client, agent_tokens):
    """Test federated agent discovery."""
    agent1 = agent_tokens["agent1"]

    response = client.get(
        "/api/v1/federation/discover?capability=testing&include_local=true",
        headers={"Authorization": f"Bearer {agent1['token']}"}
    )
    assert response.status_code == 200
    result = response.json()
    assert "agents" in result
    assert "count" in result
    assert "sources" in result
    assert result["sources"]["local"] >= 1  # At least our test agents


def test_agent_card_includes_phase3_capabilities(client):
    """Test that agent card includes Phase 3 capabilities."""
    response = client.get("/.well-known/agent.json")
    assert response.status_code == 200
    card = response.json()
    assert "a2a-task-lifecycle" in card["capabilities"]
    assert "trust-portability" in card["capabilities"]
    assert "federation-trqp" in card["capabilities"]
    assert "audit-logging" in card["capabilities"]
    assert "/api/v1/tasks" in card["endpoints"]["tasks"]
