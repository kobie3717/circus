"""Tests for routing service module (circus/services/routing.py)."""

import json
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest

from circus.database import get_db
from circus.services import routing
from circus.services.bandit import ArmState


def test_migration_creates_tables(isolate_database):
    """v14 migration should create routing tables."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'routing%'")
        tables = {row[0] for row in cursor.fetchall()}

    assert "routing_arms" in tables
    assert "routing_decisions" in tables
    assert "routing_feature_stats" in tables


def test_migration_idempotent(isolate_database):
    """Running migration twice should not error."""
    from circus.database import run_v14_migration
    run_v14_migration(isolate_database)
    run_v14_migration(isolate_database)  # Should not raise


def test_build_context_returns_32dim():
    """build_context should return 32-dim float vector."""
    with get_db() as conn:
        # Create a test agent
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("test-agent", "Test", "tester", json.dumps(["summarize"]), "local", "hash", "hash", 50.0, 1,
              datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
        conn.commit()

        deadline = (datetime.utcnow() + timedelta(hours=12)).isoformat()
        context = routing.build_context(
            task_type="summarize",
            payload={"text": "hello world"},
            requester_agent_id="test-agent",
            deadline=deadline,
            db_conn=conn
        )

    assert context.shape == (routing.FEATURE_DIM,)
    assert context.dtype == np.float64
    # Bias term should be 1.0
    assert context[28] == 1.0


def test_standardize_context_updates_stats():
    """standardize_context should update running stats."""
    with get_db() as conn:
        cursor = conn.cursor()

        x = np.random.randn(routing.FEATURE_DIM)

        # First call
        x_norm1 = routing.standardize_context(x, conn)
        conn.commit()

        # Check stats were created
        cursor.execute("SELECT COUNT(*) FROM routing_feature_stats")
        assert cursor.fetchone()[0] == routing.FEATURE_DIM

        # Second call with same vector
        x_norm2 = routing.standardize_context(x, conn)
        conn.commit()

        # Stats should have updated
        cursor.execute("SELECT n_samples FROM routing_feature_stats WHERE feature_idx = 0")
        assert cursor.fetchone()[0] == 2


def test_standardize_shifts_toward_zero_mean():
    """standardize_context should z-score normalize over many samples."""
    with get_db() as conn:
        # Generate many samples with mean=5, std=2
        for _ in range(100):
            x = np.random.randn(routing.FEATURE_DIM) * 2 + 5
            routing.standardize_context(x, conn)
            conn.commit()

        # New sample should be z-scored around zero
        x_test = np.ones(routing.FEATURE_DIM) * 5  # exactly at mean
        x_norm = routing.standardize_context(x_test, conn)

        # Should be close to zero (within tolerance due to sample variance)
        assert np.abs(x_norm.mean()) < 1.0


def test_get_candidate_agents_filters_by_capability():
    """get_candidate_agents should filter by task_type in capabilities."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Create agents with different capabilities
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("agent-a", "A", "coder", json.dumps(["summarize", "code"]), "local", "hash", "hash", 60.0, 1, now, now))

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("agent-b", "B", "writer", json.dumps(["write", "edit"]), "local", "hash", "hash", 70.0, 1, now, now))

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("agent-c", "C", "summarizer", json.dumps(["summarize"]), "local", "hash", "hash", 80.0, 1, now, now))

        conn.commit()

        candidates = routing.get_candidate_agents(
            task_type="summarize",
            min_trust=50.0,
            exclude_agents=[],
            db_conn=conn
        )

    agent_ids = [a[0] for a in candidates]
    assert "agent-a" in agent_ids
    assert "agent-c" in agent_ids
    assert "agent-b" not in agent_ids  # doesn't have "summarize" capability


def test_get_candidate_agents_filters_by_trust():
    """get_candidate_agents should filter by min_trust."""
    with get_db() as conn:
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("agent-low", "Low", "coder", json.dumps(["code"]), "local", "hash", "hash", 20.0, 1, now, now))

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("agent-high", "High", "coder", json.dumps(["code"]), "local", "hash", "hash", 80.0, 1, now, now))

        conn.commit()

        candidates = routing.get_candidate_agents(
            task_type="code",
            min_trust=50.0,
            exclude_agents=[],
            db_conn=conn
        )

    agent_ids = [a[0] for a in candidates]
    assert "agent-high" in agent_ids
    assert "agent-low" not in agent_ids


def test_get_candidate_agents_loads_arm_state():
    """get_candidate_agents should load arm state from DB or create empty."""
    with get_db() as conn:
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("agent-x", "X", "coder", json.dumps(["code"]), "local", "hash", "hash", 60.0, 1, now, now))

        conn.commit()

        # First call: should create empty arm
        candidates1 = routing.get_candidate_agents("code", 50.0, [], conn)
        # Filter to only agent-x
        candidates1 = [(a, s) for a, s in candidates1 if a == "agent-x"]
        assert len(candidates1) == 1
        assert candidates1[0][1].n_samples == 0

        # Save a trained arm
        arm = ArmState.empty(routing.FEATURE_DIM)
        x = np.random.randn(routing.FEATURE_DIM)
        arm.update(x, 0.8)
        A_blob, b_blob = arm.serialize()

        cursor.execute("""
            INSERT INTO routing_arms (agent_id, task_type, A_blob, b_blob, n_samples, cumulative_reward, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("agent-x", "code", A_blob, b_blob, arm.n_samples, arm.cumulative_reward, now))
        conn.commit()

        # Second call: should load trained arm
        candidates2 = routing.get_candidate_agents("code", 50.0, [], conn)
        candidates2 = [(a, s) for a, s in candidates2 if a == "agent-x"]
        assert len(candidates2) == 1
        assert candidates2[0][1].n_samples == 1


def test_route_task_picks_agent():
    """route_task should pick an agent and persist decision."""
    with get_db() as conn:
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("req-route-1", "R", "user", json.dumps(["query"]), "local", "hash", "hash", 50.0, 1, now, now))

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("work-route-1", "W", "coder", json.dumps(["code"]), "local", "hash", "hash", 70.0, 1, now, now))

        conn.commit()

        decision = routing.route_task(
            task_type="code",
            payload={"description": "fix bug"},
            requester="req-route-1",
            deadline=(datetime.utcnow() + timedelta(hours=6)).isoformat(),
            min_trust=50.0,
            exclude_agents=[],
            alpha_override=1.0,
            db_conn=conn
        )

        conn.commit()

    # Agent should be picked (might be work-route-1 or another agent with "code" capability)
    assert decision["agent_id"] is not None
    assert "decision_id" in decision
    assert decision["candidates"] >= 1

    # Verify decision was persisted
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM routing_decisions WHERE id = ?", (decision["decision_id"],))
        row = cursor.fetchone()
        assert row is not None
        assert row["picked_agent_id"] == decision["agent_id"]


def test_update_reward_updates_arm():
    """update_reward should update arm state when task completes."""
    with get_db() as conn:
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()

        # Create agents and task
        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("req-reward-1", "R", "user", json.dumps(["query"]), "local", "hash", "hash", 50.0, 1, now, now))

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("work-reward-1", "W", "coder", json.dumps(["code"]), "local", "hash", "hash", 70.0, 1, now, now))

        cursor.execute("""
            INSERT INTO tasks (id, from_agent_id, to_agent_id, task_type, payload, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("task-reward-1", "req-reward-1", "work-reward-1", "code", json.dumps({"x": 1}), "submitted", now, now))

        # Create decision
        x = np.random.randn(routing.FEATURE_DIM)
        cursor.execute("""
            INSERT INTO routing_decisions (
                id, task_id, picked_agent_id, context_hash, context_blob,
                candidates_considered, ucb_score, fallback, alpha, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("decision-reward-1", "task-reward-1", "work-reward-1", "hash123", x.astype(np.float32).tobytes(),
              1, 0.5, "bandit", 1.0, now))

        conn.commit()

        # Update reward
        routing.update_reward("task-reward-1", 0.9, "completed", conn)
        conn.commit()

        # Verify arm was updated
        cursor.execute("""
            SELECT n_samples, cumulative_reward FROM routing_arms
            WHERE agent_id = ? AND task_type = ?
        """, ("work-reward-1", "code"))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 1  # n_samples
        assert abs(row[1] - 0.9) < 0.01  # cumulative_reward


def test_update_reward_skips_self_routing():
    """update_reward should skip sybil (self-routed) tasks."""
    with get_db() as conn:
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("self", "S", "bot", json.dumps(["work"]), "local", "hash", "hash", 50.0, 1, now, now))

        # Self-routed task
        cursor.execute("""
            INSERT INTO tasks (id, from_agent_id, to_agent_id, task_type, payload, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("task-self", "self", "self", "work", json.dumps({}), "completed", now, now))

        x = np.random.randn(routing.FEATURE_DIM)
        cursor.execute("""
            INSERT INTO routing_decisions (
                id, task_id, picked_agent_id, context_hash, context_blob,
                candidates_considered, ucb_score, fallback, alpha, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("decision-self", "task-self", "self", "hash", x.astype(np.float32).tobytes(),
              1, 0.5, "bandit", 1.0, now))

        conn.commit()

        # Update reward
        routing.update_reward("task-self", 1.0, "test", conn)
        conn.commit()

        # Arm should NOT have been created
        cursor.execute("SELECT COUNT(*) FROM routing_arms WHERE agent_id = ?", ("self",))
        assert cursor.fetchone()[0] == 0


def test_update_reward_idempotent():
    """update_reward should not double-update if called twice."""
    with get_db() as conn:
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("req-idem-1", "R", "user", json.dumps(["query"]), "local", "hash", "hash", 50.0, 1, now, now))

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("work-idem-1", "W", "coder", json.dumps(["code"]), "local", "hash", "hash", 70.0, 1, now, now))

        cursor.execute("""
            INSERT INTO tasks (id, from_agent_id, to_agent_id, task_type, payload, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("task-idem-1", "req-idem-1", "work-idem-1", "code", json.dumps({}), "completed", now, now))

        x = np.random.randn(routing.FEATURE_DIM)
        cursor.execute("""
            INSERT INTO routing_decisions (
                id, task_id, picked_agent_id, context_hash, context_blob,
                candidates_considered, ucb_score, fallback, alpha, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("decision-idem-1", "task-idem-1", "work-idem-1", "hash", x.astype(np.float32).tobytes(),
              1, 0.5, "bandit", 1.0, now))

        conn.commit()

        # First update
        routing.update_reward("task-idem-1", 0.8, "completed", conn)
        conn.commit()

        # Second update (should be no-op)
        routing.update_reward("task-idem-1", 0.2, "override", conn)
        conn.commit()

        # Arm should still have reward=0.8
        cursor.execute("""
            SELECT cumulative_reward FROM routing_arms WHERE agent_id = ? AND task_type = ?
        """, ("work-idem-1", "code"))
        row = cursor.fetchone()
        assert abs(row[0] - 0.8) < 0.01


def test_compute_default_reward():
    """compute_default_reward should follow spec reward table."""
    # Completed + schema valid
    reward, reason = routing.compute_default_reward("completed", True, None, None)
    assert reward == 1.0
    assert "schema_valid" in reason

    # Completed + no schema
    reward, reason = routing.compute_default_reward("completed", None, None, None)
    assert reward == 0.8
    assert "no_schema" in reason

    # Completed + schema invalid
    reward, reason = routing.compute_default_reward("completed", False, None, None)
    assert reward == 0.4
    assert "schema_invalid" in reason

    # Failed
    reward, reason = routing.compute_default_reward("failed", None, None, None)
    assert reward == 0.0
    assert reason == "failed"

    # Canceled
    reward, reason = routing.compute_default_reward("canceled", None, None, None)
    assert reward == 0.5


def test_is_terminal_state():
    """is_terminal_state should identify terminal states."""
    assert routing.is_terminal_state("completed")
    assert routing.is_terminal_state("failed")
    assert routing.is_terminal_state("canceled")
    assert not routing.is_terminal_state("submitted")
    assert not routing.is_terminal_state("working")


def test_cold_start_fallback():
    """route_task should use semantic fallback when all arms cold."""
    with get_db() as conn:
        cursor = conn.cursor()

        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("req-cold-1", "R", "user", json.dumps(["query"]), "local", "hash", "hash", 50.0, 1, now, now))

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("work-cold-a", "A", "coder", json.dumps(["code"]), "local", "hash", "hash", 70.0, 1, now, now))

        cursor.execute("""
            INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, trust_score, is_active, registered_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("work-cold-b", "B", "coder", json.dumps(["code"]), "local", "hash", "hash", 80.0, 1, now, now))

        conn.commit()

        decision = routing.route_task(
            task_type="code",
            payload={"desc": "test"},
            requester="req-cold-1",
            deadline=None,
            min_trust=50.0,
            exclude_agents=[],
            alpha_override=1.0,
            db_conn=conn
        )

        conn.commit()

    # Should use semantic fallback (all arms have n_samples < 5)
    assert decision["fallback"] == "semantic"
    # Should pick one of the agents with "code" capability
    assert decision["agent_id"] is not None
