"""Test service layer functionality."""

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from circus.config import settings
from circus.database import get_db, init_database
from circus.services.discovery import discover_agents, find_shared_entities, get_agent_by_id
from circus.services.passport import validate_passport
from circus.services.trust import log_trust_event, get_trust_history


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    original_path = settings.database_path
    settings.database_path = db_path
    init_database(db_path)

    yield db_path

    settings.database_path = original_path
    db_path.unlink()


def test_discover_agents_no_filters(temp_db):
    """Test discovering agents without filters."""
    # Create test agent
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance,
                passport_hash, token_hash, trust_score, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "test-agent-1", "Test Agent", "testing",
            '["testing"]', "http://test.example.com",
            "hash", "token", 50.0, now, now
        ))
        conn.commit()

    agents = discover_agents(min_trust=30.0, limit=10)
    assert len(agents) > 0
    assert agents[0]["id"] == "test-agent-1"


def test_discover_agents_with_capability(temp_db):
    """Test discovering agents by capability."""
    # Create test agents
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Agent with testing capability
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance,
                passport_hash, token_hash, trust_score, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "tester-1", "Tester Agent", "testing",
            '["testing", "debugging"]', "http://tester.example.com",
            "hash", "token", 60.0, now, now
        ))

        # Agent without testing capability
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance,
                passport_hash, token_hash, trust_score, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "reviewer-1", "Reviewer Agent", "review",
            '["reviewing"]', "http://reviewer.example.com",
            "hash", "token", 55.0, now, now
        ))
        conn.commit()

    agents = discover_agents(capability="testing", min_trust=30.0, limit=10)
    assert len(agents) == 1
    assert agents[0]["id"] == "tester-1"


def test_get_agent_by_id(temp_db):
    """Test getting agent by ID."""
    # Create test agent
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance,
                passport_hash, token_hash, trust_score, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "test-find-1", "Find Me", "testing",
            '["testing"]', "http://find.example.com",
            "hash", "token", 50.0, now, now
        ))
        conn.commit()

    agent = get_agent_by_id("test-find-1")
    assert agent is not None
    assert agent["name"] == "Find Me"

    # Test non-existent agent
    agent = get_agent_by_id("nonexistent")
    assert agent is None


def test_find_shared_entities(temp_db):
    """Test finding shared entities between agents."""
    passport_a = {
        "graph_summary": {
            "entities": [
                {"name": "FlashVault", "type": "project"},
                {"name": "Python", "type": "tool"}
            ]
        }
    }

    passport_b = {
        "graph_summary": {
            "entities": [
                {"name": "FlashVault", "type": "project"},
                {"name": "JavaScript", "type": "tool"}
            ]
        }
    }

    # Create test agents with passports
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Agent A
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance,
                passport_hash, token_hash, trust_score, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "agent-a", "Agent A", "testing",
            '["testing"]', "http://a.example.com",
            "hash", "token", 50.0, now, now
        ))

        cursor.execute("""
            INSERT INTO passports (
                agent_id, passport_data, trust_score, created_at
            ) VALUES (?, ?, ?, ?)
        """, ("agent-a", json.dumps(passport_a), 50.0, now))

        # Agent B
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance,
                passport_hash, token_hash, trust_score, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "agent-b", "Agent B", "testing",
            '["testing"]', "http://b.example.com",
            "hash", "token", 50.0, now, now
        ))

        cursor.execute("""
            INSERT INTO passports (
                agent_id, passport_data, trust_score, created_at
            ) VALUES (?, ?, ?, ?)
        """, ("agent-b", json.dumps(passport_b), 50.0, now))

        conn.commit()

    shared = find_shared_entities("agent-a", "agent-b")
    assert len(shared) == 1
    assert "FlashVault" in shared


def test_validate_passport():
    """Test passport validation."""
    valid_passport = {
        "identity": {"name": "Test", "role": "testing"},
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 2.0, "graph_connections": 10},
        "score": {"total": 6.5},
        "graph_summary": {"entities": []},
        "traits": {}
    }

    assert validate_passport(valid_passport) is True

    # Missing identity
    invalid_passport = {
        "predictions": {"confirmed": 5, "refuted": 1},
        "score": {"total": 6.5}
    }

    assert validate_passport(invalid_passport) is False


def test_log_trust_event(temp_db):
    """Test logging trust events."""
    # Create test agent
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance,
                passport_hash, token_hash, trust_score, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "test-trust-1", "Trust Test", "testing",
            '["testing"]', "http://trust.example.com",
            "hash", "token", 50.0, now, now
        ))
        conn.commit()

    log_trust_event("test-trust-1", "prediction_confirmed", 5.0, "Test prediction")

    # Verify event was logged
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT event_type, delta FROM trust_events WHERE agent_id = ?
        """, ("test-trust-1",))
        event = cursor.fetchone()

        assert event is not None
        assert event["event_type"] == "prediction_confirmed"
        assert event["delta"] == 5.0


def test_get_trust_history(temp_db):
    """Test getting trust history for an agent."""
    # Create test agent
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance,
                passport_hash, token_hash, trust_score, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "test-history-1", "History Test", "testing",
            '["testing"]', "http://history.example.com",
            "hash", "token", 50.0, now, now
        ))
        conn.commit()

    # Log multiple events
    log_trust_event("test-history-1", "prediction_confirmed", 5.0, "Event 1")
    log_trust_event("test-history-1", "vouch_received", 5.0, "Event 2")

    history = get_trust_history("test-history-1", limit=10)
    assert len(history) == 2
    assert history[0]["event_type"] in ["prediction_confirmed", "vouch_received"]
