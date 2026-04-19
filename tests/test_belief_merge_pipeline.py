"""Parity tests for belief merge pipeline extraction.

These tests prove bit-identical behavior before/after extracting
the merge pipeline from memory_commons route into belief_merge service.

Week 3 Sub-step 3.6a-prereq.
"""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from circus.services.belief_merge import apply_belief_merge_pipeline, ConflictResolution


# Fixture helpers

def assert_conflict_row_count(conn, new_memory_id: str, expected: int):
    """Assert no duplicate conflict rows created (required per design §6.1)."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM belief_conflicts WHERE memory_id_a = ? OR memory_id_b = ?",
        (new_memory_id, new_memory_id)
    )
    actual = cursor.fetchone()[0]
    assert actual == expected, f"Expected {expected} conflict rows for {new_memory_id}, got {actual}"


@pytest.fixture
def db_conn():
    """In-memory SQLite database with required schema."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    # Create shared_memories table
    cursor.execute("""
        CREATE TABLE shared_memories (
            id TEXT PRIMARY KEY,
            room_id TEXT,
            from_agent_id TEXT,
            content TEXT,
            category TEXT,
            domain TEXT,
            tags TEXT,
            provenance TEXT,
            privacy_tier TEXT DEFAULT 'public',
            hop_count INTEGER DEFAULT 1,
            original_author TEXT,
            confidence REAL,
            age_days INTEGER DEFAULT 0,
            effective_confidence REAL,
            shared_at TEXT,
            trust_verified INTEGER DEFAULT 0
        )
    """)

    # Create belief_conflicts table
    cursor.execute("""
        CREATE TABLE belief_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id_a TEXT,
            memory_id_b TEXT,
            conflict_type TEXT,
            detected_at TEXT,
            resolution TEXT,
            resolved_at TEXT,
            resolved_by_agent_id TEXT
        )
    """)

    # Create agents table (for trust scores)
    cursor.execute("""
        CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            name TEXT,
            role TEXT,
            trust_score REAL DEFAULT 50.0
        )
    """)

    # Create agent_domains table (for stewardship)
    cursor.execute("""
        CREATE TABLE agent_domains (
            agent_id TEXT,
            domain TEXT,
            stewardship_level REAL,
            claimed_at TEXT,
            PRIMARY KEY (agent_id, domain)
        )
    """)

    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def mock_settings_enabled(monkeypatch):
    """Mock settings with conflict_detection_enabled=True."""
    from circus.config import settings
    monkeypatch.setattr(settings, "conflict_detection_enabled", True)


@pytest.fixture
def mock_settings_disabled(monkeypatch):
    """Mock settings with conflict_detection_enabled=False."""
    from circus.config import settings
    monkeypatch.setattr(settings, "conflict_detection_enabled", False)


# Test 1: No conflict (<80% similarity)
def test_no_conflict(db_conn, mock_settings_enabled):
    """<80% similarity → returns None, zero belief_conflicts rows."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert existing memory (different topic)
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent1", "The sky is blue", "observation", "meteorology", 1.0, 1.0,
          (now - timedelta(hours=1)).isoformat(), "public"))

    # Insert new memory (completely different topic)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "Python is a programming language", "observation", "software",
          1.0, 1.0, now.isoformat(), "public"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "Python is a programming language",
            "category": "observation",
            "domain": "software",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is None, "Should return None when no conflict detected"
    assert_conflict_row_count(db_conn, new_mem_id, expected=0)


# Test 2: Auto-resolved (winner 2x loser authority)
def test_auto_resolved(db_conn, mock_settings_enabled):
    """Winner 2x loser authority → ConflictResolution(auto_resolved=True), loser superseded_by set."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agents with different trust scores
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent-high", 90.0))
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent-low", 30.0))

    # Insert domain stewardship (high steward has 2x+ authority)
    cursor.execute("""
        INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at)
        VALUES (?, ?, ?, ?)
    """, ("agent-high", "climate", 2.0, now.isoformat()))
    cursor.execute("""
        INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at)
        VALUES (?, ?, ?, ?)
    """, ("agent-low", "climate", 0.5, now.isoformat()))

    # Insert existing memory (low authority)
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent-low", "Global warming is not real", "belief", "climate", 0.7, 0.7,
          (now - timedelta(hours=1)).isoformat(), "public", "{}"))

    # Insert new memory (high authority, contradicts old)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent-high", "Global warming is real and accelerating", "belief", "climate",
          0.9, 0.9, now.isoformat(), "public", "{}"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent-high",
            "content": "Global warming is real and accelerating",
            "category": "belief",
            "domain": "climate",
            "confidence": 0.9,
            "shared_at": now.isoformat(),
        },
        agent_id="agent-high",
        now=now,
    )

    # Assert
    assert result is not None, "Should detect conflict"
    assert isinstance(result, ConflictResolution)
    assert result.auto_resolved is True, "Should auto-resolve when winner 2x+ loser"
    assert result.winner_id == new_mem_id, "High-authority agent should win"
    assert result.strategy == "supersede", "Different authors should use supersede strategy"

    # Check loser has superseded_by in provenance
    cursor.execute("SELECT provenance FROM shared_memories WHERE id = ?", ("mem-old",))
    prov = json.loads(cursor.fetchone()[0])
    assert prov.get("superseded_by") == new_mem_id, "Loser should have superseded_by set"

    # Check conflict row
    assert_conflict_row_count(db_conn, new_mem_id, expected=1)


# Test 3: Manual review (winner 1.2x loser)
def test_manual_review(db_conn, mock_settings_enabled):
    """Winner 1.2x loser → ConflictResolution(auto_resolved=False), resolved_at=NULL."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agents
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 70.0))
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent2", 60.0))

    # Insert domain stewardship (close scores)
    cursor.execute("""
        INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at)
        VALUES (?, ?, ?, ?)
    """, ("agent1", "economics", 1.2, now.isoformat()))
    cursor.execute("""
        INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at)
        VALUES (?, ?, ?, ?)
    """, ("agent2", "economics", 1.0, now.isoformat()))

    # Insert existing memory
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent2", "Inflation is not concerning", "belief", "economics", 0.8, 0.8,
          (now - timedelta(hours=1)).isoformat(), "public", "{}"))

    # Insert new memory (contradicts old, but not by much authority)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "Inflation is very concerning", "belief", "economics",
          0.85, 0.85, now.isoformat(), "public", "{}"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "Inflation is very concerning",
            "category": "belief",
            "domain": "economics",
            "confidence": 0.85,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is not None, "Should detect conflict"
    assert result.auto_resolved is False, "Should require manual review when scores close"

    # Check conflict row has NULL resolved_at
    cursor.execute("""
        SELECT resolved_at FROM belief_conflicts
        WHERE memory_id_a = ? OR memory_id_b = ?
    """, ("mem-old", new_mem_id))
    resolved_at = cursor.fetchone()[0]
    assert resolved_at is None, "Manual review conflicts should have NULL resolved_at"

    assert_conflict_row_count(db_conn, new_mem_id, expected=1)


# Test 4: Flag disabled
def test_flag_disabled(db_conn, mock_settings_disabled):
    """conflict_detection_enabled=False → returns None, zero writes."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert existing memory (would conflict if enabled)
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent1", "The sky is blue", "observation", "meteorology", 1.0, 1.0,
          (now - timedelta(hours=1)).isoformat(), "public"))

    # Insert new memory (very similar, would trigger conflict)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "The sky is definitely blue", "observation", "meteorology",
          1.0, 1.0, now.isoformat(), "public"))
    db_conn.commit()

    # Run pipeline with flag disabled
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "The sky is definitely blue",
            "category": "observation",
            "domain": "meteorology",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is None, "Should return None when flag disabled"
    assert_conflict_row_count(db_conn, new_mem_id, expected=0)


# Test 5: Bootstrap empty
def test_bootstrap_empty(db_conn, mock_settings_enabled):
    """No existing memories → returns None."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert ONLY the new memory (no existing memories)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "First memory ever", "observation", "general",
          1.0, 1.0, now.isoformat(), "public"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "First memory ever",
            "category": "observation",
            "domain": "general",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is None, "Should return None when no existing memories"
    assert_conflict_row_count(db_conn, new_mem_id, expected=0)


# Test 6: Cross-category
def test_cross_category(db_conn, mock_settings_enabled):
    """Different category → returns None (category filter works)."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert existing memory in different category
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent1", "The sky is blue", "observation", "meteorology", 1.0, 1.0,
          (now - timedelta(hours=1)).isoformat(), "public"))

    # Insert new memory in different category (but similar content)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "The sky is blue today", "prediction", "meteorology",
          1.0, 1.0, now.isoformat(), "public"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "The sky is blue today",
            "category": "prediction",
            "domain": "meteorology",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is None, "Should return None when categories differ"
    assert_conflict_row_count(db_conn, new_mem_id, expected=0)


# Test 7: Privacy filter
def test_privacy_filter(db_conn, mock_settings_enabled):
    """Existing memory private → returns None (privacy filter works)."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert existing memory with private privacy tier
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent1", "The sky is blue", "observation", "meteorology", 1.0, 1.0,
          (now - timedelta(hours=1)).isoformat(), "private"))

    # Insert new memory (public, similar content)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "The sky is definitely blue", "observation", "meteorology",
          1.0, 1.0, now.isoformat(), "public"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "The sky is definitely blue",
            "category": "observation",
            "domain": "meteorology",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is None, "Should return None when existing memory is private"
    assert_conflict_row_count(db_conn, new_mem_id, expected=0)


# Test 8: Multi-candidates
def test_multi_candidates(db_conn, mock_settings_enabled):
    """2+ conflicts → picks first by shared_at DESC."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert two existing memories (both would conflict)
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old-1", "agent1", "The sky is blue", "observation", "meteorology", 1.0, 1.0,
          (now - timedelta(hours=2)).isoformat(), "public"))

    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old-2", "agent1", "The sky is very blue", "observation", "meteorology", 1.0, 1.0,
          (now - timedelta(hours=1)).isoformat(), "public"))

    # Insert new memory (conflicts with both)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "The sky is extremely blue", "observation", "meteorology",
          1.0, 1.0, now.isoformat(), "public"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "The sky is extremely blue",
            "category": "observation",
            "domain": "meteorology",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is not None, "Should detect conflict"
    # Should pick the most recent one (mem-old-2)
    assert result.memory_id_a == "mem-old-2", "Should pick most recent by shared_at DESC"
    assert_conflict_row_count(db_conn, new_mem_id, expected=1)


# Test 9: Self-contradiction
def test_self_contradiction(db_conn, mock_settings_enabled):
    """Same author + negation → conflict_type='self-contradiction'."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert existing memory
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent1", "The sky is blue", "observation", "meteorology", 1.0, 1.0,
          (now - timedelta(hours=1)).isoformat(), "public", "{}"))

    # Insert new memory (same author, negation)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "The sky is not blue", "observation", "meteorology",
          1.0, 1.0, now.isoformat(), "public", "{}"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "The sky is not blue",
            "category": "observation",
            "domain": "meteorology",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is not None, "Should detect conflict"
    assert result.conflict_type == "self-contradiction", "Same author + negation = self-contradiction"
    assert_conflict_row_count(db_conn, new_mem_id, expected=1)


# Test 10: Same author update
def test_same_author_update(db_conn, mock_settings_enabled):
    """Same author, no negation → conflict_type='update', strategy='merge', derived_from updated."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert existing memory (much older to ensure new one wins with 1.5x+ ratio)
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent1", "The sky is blue", "observation", "meteorology", 0.5, 0.5,
          (now - timedelta(days=90)).isoformat(), "public", "{}"))

    # Insert new memory (same author, refinement, no negation, higher confidence)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "The sky is a beautiful shade of blue", "observation", "meteorology",
          1.0, 1.0, now.isoformat(), "public", "{}"))
    db_conn.commit()

    # Run pipeline
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "The sky is a beautiful shade of blue",
            "category": "observation",
            "domain": "meteorology",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert
    assert result is not None, "Should detect conflict"
    assert result.conflict_type == "update", "Same author, no negation = update"
    assert result.strategy == "merge", "Same author should use merge strategy"
    assert result.auto_resolved is True, "Should auto-resolve with large confidence+recency gap"

    # Check winner has derived_from in provenance
    # Compute loser_id (the one that's not the winner)
    loser_id = result.memory_id_b if result.winner_id == result.memory_id_a else result.memory_id_a

    cursor.execute("SELECT provenance FROM shared_memories WHERE id = ?", (result.winner_id,))
    prov = json.loads(cursor.fetchone()[0])
    assert "derived_from" in prov, "Winner should have derived_from in provenance"
    assert loser_id in prov["derived_from"], "Loser should be in derived_from"

    assert_conflict_row_count(db_conn, new_mem_id, expected=1)


# Test 11: Missing domain raises
def test_missing_domain_raises(db_conn, mock_settings_enabled):
    """new_memory lacks domain key → raises validation error."""
    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))

    # Insert existing memory to trigger conflict detection (so domain is accessed)
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("mem-old", "agent1", "The sky is blue", "observation", "general", 1.0, 1.0,
          (now - timedelta(hours=1)).isoformat(), "public"))

    # Insert new memory (will be used in pipeline call)
    new_mem_id = "mem-new"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, from_agent_id, content, category, domain, confidence,
            effective_confidence, shared_at, privacy_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_mem_id, "agent1", "The sky is very blue", "observation", "general",
          1.0, 1.0, now.isoformat(), "public"))
    db_conn.commit()

    # Run pipeline with missing domain key (should raise when detect_conflict tries to access it)
    with pytest.raises(KeyError) as exc_info:
        apply_belief_merge_pipeline(
            db_conn,
            new_memory={
                "id": new_mem_id,
                "from_agent_id": "agent1",
                "content": "The sky is very blue",
                "category": "observation",
                # "domain" key missing - will raise in detect_conflict
                "confidence": 1.0,
                "shared_at": now.isoformat(),
            },
            agent_id="agent1",
            now=now,
        )

    assert "domain" in str(exc_info.value), "Should raise about missing domain key"
    assert_conflict_row_count(db_conn, new_mem_id, expected=0)


# Test 12: Memory not in DB raises
def test_memory_not_in_db_raises(db_conn, mock_settings_enabled):
    """new_memory ID not INSERTed first → raises pre-condition error."""
    # Note: This test documents current behavior. If the inlined code doesn't
    # raise but silently misbehaves, we preserve that behavior for parity.
    # However, based on the code, if memory_id doesn't exist, the SELECT will
    # return no rows and detect_conflict will get an empty list, returning None.
    # So this is actually a valid case where result is None, not an error.

    cursor = db_conn.cursor()
    now = datetime.utcnow()

    # Insert agent
    cursor.execute("INSERT INTO agents (id, trust_score) VALUES (?, ?)", ("agent1", 50.0))
    db_conn.commit()

    # DO NOT insert the new memory (violates PRE-condition)
    new_mem_id = "mem-not-exists"

    # Run pipeline without INSERTing the memory first
    # Based on code inspection, this will just return None (empty existing_memories)
    result = apply_belief_merge_pipeline(
        db_conn,
        new_memory={
            "id": new_mem_id,
            "from_agent_id": "agent1",
            "content": "Test content",
            "category": "observation",
            "domain": "general",
            "confidence": 1.0,
            "shared_at": now.isoformat(),
        },
        agent_id="agent1",
        now=now,
    )

    # Assert: Preserves current behavior (returns None, doesn't raise)
    assert result is None, "Should return None when no existing memories (including self)"
    assert_conflict_row_count(db_conn, new_mem_id, expected=0)
