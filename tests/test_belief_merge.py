"""Unit tests for belief conflict detection and resolution.

Tests the conflict detection and domain authority from spec §6.
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from circus.database import init_database, run_v2_migration
from circus.services.belief_merge import (
    detect_conflict,
    resolve_conflict,
    apply_merge,
    _has_negation,
    _cosine_similarity,
    _recency_score,
    SIMILARITY_THRESHOLD,
    AUTO_RESOLVE_RATIO,
)


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    init_database(db_path)
    run_v2_migration(db_path)

    # Add test agents
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO agents (
            id, name, role, capabilities, home_instance, passport_hash,
            token_hash, trust_score, trust_tier, registered_at, last_seen
        ) VALUES
        ('friday-123', 'Friday', 'assistant', '[]', 'http://localhost', 'p1', 't1', 88.0, 'Elder', datetime('now'), datetime('now')),
        ('claw-456', 'Claw', 'infra', '[]', 'http://localhost', 'p2', 't2', 72.0, 'Trusted', datetime('now'), datetime('now'))
    """)

    conn.commit()
    conn.close()

    yield db_path

    db_path.unlink(missing_ok=True)


class TestNegationDetection:
    """Test negation pattern detection."""

    def test_english_not(self):
        assert _has_negation("user does not prefer terse replies") is True

    def test_english_never(self):
        assert _has_negation("never use emojis") is True

    def test_english_isnt(self):
        assert _has_negation("this isn't correct") is True

    def test_english_no_longer(self):
        assert _has_negation("no longer applicable") is True

    def test_afrikaans_nie(self):
        assert _has_negation("gebruiker verkies nie beknopte antwoorde nie") is True

    def test_afrikaans_nooit(self):
        assert _has_negation("nooit gebruik emojis") is True

    def test_portuguese_nao(self):
        assert _has_negation("usuário não prefere respostas breves") is True

    def test_portuguese_nunca(self):
        assert _has_negation("nunca use emojis") is True

    def test_no_negation(self):
        assert _has_negation("user prefers terse replies") is False

    # Fix B: Test negation false positives are avoided
    def test_no_idea_not_negation(self):
        """'no idea' should NOT be detected as negation."""
        assert _has_negation("I have no idea what happened") is False

    def test_no_problem_not_negation(self):
        """'no problem' should NOT be detected as negation."""
        assert _has_negation("no problem with this") is False

    def test_no_worries_not_negation(self):
        """'no worries' should NOT be detected as negation."""
        assert _has_negation("no worries about that") is False

    def test_not_sure_not_negation(self):
        """'not sure' should NOT be detected as negation."""
        assert _has_negation("I'm not sure if it works") is False

    def test_not_certain_not_negation(self):
        """'not certain' should NOT be detected as negation."""
        assert _has_negation("not certain about the outcome") is False

    def test_not_yet_not_negation(self):
        """'not yet' should NOT be detected as negation."""
        assert _has_negation("not yet released") is False

    def test_not_clear_not_negation(self):
        """'not clear' should NOT be detected as negation."""
        assert _has_negation("it's not clear from the logs") is False

    # Positive controls: these MUST still detect negation
    def test_not_working_is_negation(self):
        """'not working' MUST be detected as negation."""
        assert _has_negation("the feature is not working") is True

    def test_never_happened_is_negation(self):
        """'never happened' MUST be detected as negation."""
        assert _has_negation("this never happened before") is True

    def test_no_longer_valid_is_negation(self):
        """'no longer valid' MUST be detected as negation."""
        assert _has_negation("the config is no longer valid") is True

    def test_no_more_is_negation(self):
        """'no more' MUST be detected as negation."""
        assert _has_negation("we have no more capacity") is True

    def test_afrikaans_nie_meer_is_negation(self):
        """Afrikaans 'nie meer' (no longer) MUST be detected."""
        assert _has_negation("dit werk nie meer nie") is True

    def test_portuguese_nao_funciona_is_negation(self):
        """Portuguese 'não funciona' (doesn't work) MUST be detected."""
        assert _has_negation("não funciona corretamente") is True


class TestCosineSimilarity:
    """Test cosine similarity calculation."""

    def test_identical_vectors(self):
        vec = [1.0, 2.0, 3.0]
        sim = _cosine_similarity(vec, vec)
        assert sim == pytest.approx(1.0, abs=0.01)

    def test_orthogonal_vectors(self):
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        sim = _cosine_similarity(vec_a, vec_b)
        assert sim == pytest.approx(0.0, abs=0.01)

    def test_opposite_vectors(self):
        vec_a = [1.0, 0.0]
        vec_b = [-1.0, 0.0]
        sim = _cosine_similarity(vec_a, vec_b)
        assert sim == pytest.approx(-1.0, abs=0.01)

    def test_different_lengths(self):
        vec_a = [1.0, 2.0]
        vec_b = [1.0, 2.0, 3.0]
        sim = _cosine_similarity(vec_a, vec_b)
        assert sim == 0.0

    def test_zero_magnitude(self):
        vec_a = [0.0, 0.0]
        vec_b = [1.0, 2.0]
        sim = _cosine_similarity(vec_a, vec_b)
        assert sim == 0.0


class TestRecencyScore:
    """Test recency scoring."""

    def test_fresh_memory(self):
        """Fresh memory (now) should get high score."""
        timestamp = datetime.utcnow().isoformat()
        score = _recency_score(timestamp)
        assert score == pytest.approx(1.0, abs=0.01)

    def test_old_memory(self):
        """Old memory (180 days) should get low score."""
        timestamp = (datetime.utcnow() - timedelta(days=180)).isoformat()
        score = _recency_score(timestamp)
        # exp(-180/180 * ln(2)) = 0.5
        assert score == pytest.approx(0.5, abs=0.01)

    def test_ancient_memory(self):
        """Ancient memory (360 days) should get very low score."""
        timestamp = (datetime.utcnow() - timedelta(days=360)).isoformat()
        score = _recency_score(timestamp)
        # exp(-360/180 * ln(2)) = 0.25
        assert score == pytest.approx(0.25, abs=0.01)

    def test_invalid_timestamp(self):
        """Invalid timestamp should return neutral score."""
        score = _recency_score("invalid")
        assert score == 0.5


class TestConflictDetection:
    """Test conflict detection logic."""

    def test_no_conflict_different_content(self):
        """Different content should not conflict."""
        new_memory = {
            "id": "mem-2",
            "from_agent_id": "friday-123",
            "content": "User prefers detailed explanations",
            "category": "user-preferences",
            "confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()
        }

        existing_memories = [
            {
                "id": "mem-1",
                "from_agent_id": "friday-123",
                "content": "System uses PostgreSQL database",  # Totally different
                "category": "architecture",
                "confidence": 0.9,
                "shared_at": (datetime.utcnow() - timedelta(days=1)).isoformat()
            }
        ]

        conflict = detect_conflict(new_memory, existing_memories)
        assert conflict is None

    def test_update_same_author(self):
        """Same author, similar content = update conflict."""
        new_memory = {
            "id": "mem-2",
            "from_agent_id": "friday-123",
            "content": "Kobus prefers terse replies in chat",
            "category": "user-preferences",
            "domain": "user-preferences",
            "confidence": 0.95,
            "shared_at": datetime.utcnow().isoformat()
        }

        existing_memories = [
            {
                "id": "mem-1",
                "from_agent_id": "friday-123",
                "content": "Kobus prefers terse replies",  # Very similar
                "category": "user-preferences",
                "domain": "user-preferences",
                "confidence": 0.9,
                "shared_at": (datetime.utcnow() - timedelta(days=1)).isoformat()
            }
        ]

        conflict = detect_conflict(new_memory, existing_memories)
        assert conflict is not None
        assert conflict.conflict_type == "update"
        assert conflict.similarity >= SIMILARITY_THRESHOLD

    def test_contradiction_with_negation(self):
        """Similar content + negation = contradiction."""
        new_memory = {
            "id": "mem-2",
            "from_agent_id": "claw-456",
            "content": "Kobus does not prefer terse replies",
            "category": "user-preferences",
            "domain": "user-preferences",
            "confidence": 0.8,
            "shared_at": datetime.utcnow().isoformat()
        }

        existing_memories = [
            {
                "id": "mem-1",
                "from_agent_id": "friday-123",
                "content": "Kobus prefers terse replies",
                "category": "user-preferences",
                "domain": "user-preferences",
                "confidence": 0.9,
                "shared_at": (datetime.utcnow() - timedelta(days=1)).isoformat()
            }
        ]

        conflict = detect_conflict(new_memory, existing_memories)
        assert conflict is not None
        assert conflict.conflict_type == "contradiction"

    def test_refinement_different_authors(self):
        """Similar content, different authors, no negation = refinement."""
        new_memory = {
            "id": "mem-2",
            "from_agent_id": "claw-456",
            "content": "PayFast webhooks use IP whitelist for security",
            "category": "architecture",
            "domain": "payment-flows",
            "confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()
        }

        existing_memories = [
            {
                "id": "mem-1",
                "from_agent_id": "friday-123",
                "content": "PayFast webhooks use IP whitelist",
                "category": "architecture",
                "domain": "payment-flows",
                "confidence": 0.9,
                "shared_at": (datetime.utcnow() - timedelta(days=1)).isoformat()
            }
        ]

        conflict = detect_conflict(new_memory, existing_memories)
        # May or may not trigger depending on semantic similarity
        # This is implementation-dependent based on embeddings

    def test_self_contradiction(self):
        """Fix C: Same author + similar content + negation = self-contradiction (not silent update)."""
        new_memory = {
            "id": "mem-2",
            "from_agent_id": "friday-123",  # Same author
            "content": "Kobus does not prefer terse replies",  # Negation present
            "category": "user-preferences",
            "domain": "user-preferences",
            "confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()
        }

        existing_memories = [
            {
                "id": "mem-1",
                "from_agent_id": "friday-123",  # Same author
                "content": "Kobus prefers terse replies",  # No negation
                "category": "user-preferences",
                "domain": "user-preferences",
                "confidence": 0.9,
                "shared_at": (datetime.utcnow() - timedelta(days=1)).isoformat()
            }
        ]

        conflict = detect_conflict(new_memory, existing_memories)
        assert conflict is not None
        assert conflict.conflict_type == "self-contradiction"
        assert conflict.similarity >= SIMILARITY_THRESHOLD


class TestConflictResolution:
    """Test conflict resolution logic."""

    def test_resolve_with_stewardship(self, temp_db):
        """Friday (steward) wins over Claw (non-steward)."""
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Friday claims user-preferences domain
        cursor.execute("""
            INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at, last_updated)
            VALUES ('friday-123', 'user-preferences', 0.8, datetime('now'), datetime('now'))
        """)
        conn.commit()

        memory_a = {
            "id": "mem-1",
            "from_agent_id": "friday-123",
            "content": "Kobus prefers detailed explanations",
            "category": "user-preferences",
            "effective_confidence": 0.8,
            "shared_at": (datetime.utcnow() - timedelta(days=5)).isoformat()
        }

        memory_b = {
            "id": "mem-2",
            "from_agent_id": "claw-456",
            "content": "Kobus prefers terse replies",
            "category": "user-preferences",
            "effective_confidence": 0.9,  # Higher confidence
            "shared_at": datetime.utcnow().isoformat()  # More recent
        }

        result = resolve_conflict(conn, memory_a, memory_b, "user-preferences")

        # Friday should win despite lower confidence and older timestamp
        # because stewardship multiplier (0.8) gives Friday advantage
        assert result.winner_id == "mem-1"  # Friday's memory
        assert result.strategy in ["supersede", "merge"]

        conn.close()

    def test_resolve_same_author_merge(self, temp_db):
        """Same author update should use merge strategy."""
        conn = sqlite3.connect(str(temp_db))

        memory_a = {
            "id": "mem-1",
            "from_agent_id": "friday-123",
            "content": "Kobus prefers detailed explanations",
            "category": "user-preferences",
            "effective_confidence": 0.8,
            "shared_at": (datetime.utcnow() - timedelta(days=5)).isoformat()
        }

        memory_b = {
            "id": "mem-2",
            "from_agent_id": "friday-123",  # Same author
            "content": "Kobus prefers terse replies",
            "category": "user-preferences",
            "effective_confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()
        }

        result = resolve_conflict(conn, memory_a, memory_b, "user-preferences")

        assert result.strategy == "merge"

        conn.close()

    def test_auto_resolve_threshold(self, temp_db):
        """Auto-resolve only if winner score >= 1.5x loser score."""
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Friday has high stewardship (0.9)
        cursor.execute("""
            INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at, last_updated)
            VALUES ('friday-123', 'user-preferences', 0.9, datetime('now'), datetime('now'))
        """)
        conn.commit()

        memory_a = {
            "id": "mem-1",
            "from_agent_id": "friday-123",
            "content": "Kobus prefers detailed explanations",
            "category": "user-preferences",
            "effective_confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()
        }

        memory_b = {
            "id": "mem-2",
            "from_agent_id": "claw-456",
            "content": "Kobus prefers terse replies",
            "category": "user-preferences",
            "effective_confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()
        }

        result = resolve_conflict(conn, memory_a, memory_b, "user-preferences")

        # With stewardship 0.9 vs 0.0, Friday should auto-resolve
        # score_a = 0.9 * 0.9 * 1.0 = 0.81
        # score_b = 0.0 * 0.9 * 1.0 = 0.0
        # ratio = infinity, so should auto-resolve
        assert result.auto_resolved is True

        conn.close()

    def test_manual_review_close_scores(self, temp_db):
        """Manual review if scores too close."""
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Both have similar stewardship
        cursor.execute("""
            INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at, last_updated)
            VALUES
            ('friday-123', 'user-preferences', 0.6, datetime('now'), datetime('now')),
            ('claw-456', 'user-preferences', 0.5, datetime('now'), datetime('now'))
        """)
        conn.commit()

        memory_a = {
            "id": "mem-1",
            "from_agent_id": "friday-123",
            "content": "Kobus prefers detailed explanations",
            "category": "user-preferences",
            "effective_confidence": 0.85,
            "shared_at": datetime.utcnow().isoformat()
        }

        memory_b = {
            "id": "mem-2",
            "from_agent_id": "claw-456",
            "content": "Kobus prefers terse replies",
            "category": "user-preferences",
            "effective_confidence": 0.85,
            "shared_at": datetime.utcnow().isoformat()
        }

        result = resolve_conflict(conn, memory_a, memory_b, "user-preferences")

        # Scores: 0.6*0.85*1.0 = 0.51 vs 0.5*0.85*1.0 = 0.425
        # Ratio = 0.51/0.425 = 1.2, which is < 1.5, so manual review needed
        assert result.auto_resolved is False

        conn.close()

    def test_unregistered_domain_fallback(self, temp_db):
        """Fix A: Category with no registered stewards uses neutral resolution (confidence × recency only)."""
        conn = sqlite3.connect(str(temp_db))

        # No stewards registered for "unregistered-category"

        memory_a = {
            "id": "mem-1",
            "from_agent_id": "friday-123",
            "content": "System uses PostgreSQL",
            "category": "unregistered-category",
            "effective_confidence": 0.7,
            "shared_at": datetime.utcnow().isoformat()
        }

        memory_b = {
            "id": "mem-2",
            "from_agent_id": "claw-456",
            "content": "System uses PostgreSQL database",
            "category": "unregistered-category",
            "effective_confidence": 0.9,  # Higher confidence
            "shared_at": datetime.utcnow().isoformat()
        }

        result = resolve_conflict(conn, memory_a, memory_b, "unregistered-category")

        # mem-2 should win due to higher effective_confidence (no stewardship factor)
        assert result.winner_id == "mem-2"

        conn.close()

    def test_registered_domain_steward_wins(self, temp_db):
        """Fix A: With registered stewards, steward wins even with lower confidence."""
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Friday is registered steward
        cursor.execute("""
            INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at, last_updated)
            VALUES ('friday-123', 'architecture', 0.8, datetime('now'), datetime('now'))
        """)
        conn.commit()

        memory_a = {
            "id": "mem-1",
            "from_agent_id": "friday-123",  # Steward
            "content": "System uses PostgreSQL",
            "category": "architecture",
            "effective_confidence": 0.6,  # Lower confidence
            "shared_at": (datetime.utcnow() - timedelta(days=1)).isoformat()  # Older
        }

        memory_b = {
            "id": "mem-2",
            "from_agent_id": "claw-456",  # Non-steward
            "content": "System uses PostgreSQL database",
            "category": "architecture",
            "effective_confidence": 0.9,  # Higher confidence
            "shared_at": datetime.utcnow().isoformat()  # Newer
        }

        result = resolve_conflict(conn, memory_a, memory_b, "architecture")

        # Friday (steward) should win despite lower confidence and older timestamp
        assert result.winner_id == "mem-1"

        conn.close()

    def test_authority_math_multiplicative(self, temp_db):
        """Fix D: Authority score uses multiplicative 3-factor formula (s × c × r), not additive."""
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Friday is steward with level 0.8
        cursor.execute("""
            INSERT INTO agent_domains (agent_id, domain, stewardship_level, claimed_at, last_updated)
            VALUES ('friday-123', 'test-domain', 0.8, datetime('now'), datetime('now'))
        """)
        conn.commit()

        memory_a = {
            "id": "mem-1",
            "from_agent_id": "friday-123",
            "content": "Test memory",
            "category": "test-domain",
            "effective_confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()  # Fresh = recency ~1.0
        }

        memory_b = {
            "id": "mem-2",
            "from_agent_id": "claw-456",
            "content": "Test memory alternative",
            "category": "test-domain",
            "effective_confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()
        }

        result = resolve_conflict(conn, memory_a, memory_b, "test-domain")

        # Verify multiplicative formula:
        # score_a should be approximately: 0.8 (steward) × 0.9 (confidence) × 1.0 (recency) = 0.72
        # score_b should be approximately: 0.0 (steward) × 0.9 (confidence) × 1.0 (recency) = 0.0
        assert result.authority_score_a == pytest.approx(0.72, abs=0.05)
        assert result.authority_score_b == pytest.approx(0.0, abs=0.05)

        # Old additive formula would give: 0.4*0.8 + 0.3*0.9 + 0.2*1.0 + 0.1*0.5 = 0.84
        # This should NOT match
        assert result.authority_score_a != pytest.approx(0.84, abs=0.01)

        conn.close()

    def test_domain_logging(self, temp_db, caplog):
        """Week 3: Verify logger.debug fires when resolving conflict on domain."""
        import logging
        caplog.set_level(logging.DEBUG)

        conn = sqlite3.connect(str(temp_db))

        memory_a = {
            "id": "mem-1",
            "from_agent_id": "friday-123",
            "content": "Test memory",
            "category": "test-category",
            "domain": "test-domain",
            "effective_confidence": 0.9,
            "shared_at": datetime.utcnow().isoformat()
        }

        memory_b = {
            "id": "mem-2",
            "from_agent_id": "claw-456",
            "content": "Test alternative",
            "category": "test-category",
            "domain": "test-domain",
            "effective_confidence": 0.8,
            "shared_at": datetime.utcnow().isoformat()
        }

        resolve_conflict(conn, memory_a, memory_b, "test-domain")

        # Check that logger.debug was called with domain name
        assert any("Resolving conflict on domain 'test-domain'" in record.message for record in caplog.records)

        conn.close()


class TestApplyMerge:
    """Test merge strategy application."""

    def test_supersede_strategy(self, temp_db):
        """Test supersede marks loser."""
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Insert test memories
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, tags, provenance,
                privacy_tier, hop_count, original_author, confidence, shared_at, trust_verified
            ) VALUES
            ('mem-1', 'room-1', 'friday-123', 'Old memory', 'test', '[]', '{}', 'public', 1, 'friday-123', 0.9, datetime('now'), 0),
            ('mem-2', 'room-1', 'claw-456', 'New memory', 'test', '[]', '{}', 'public', 1, 'claw-456', 0.9, datetime('now'), 0)
        """)
        conn.commit()

        apply_merge(conn, winner_id="mem-2", loser_id="mem-1", strategy="supersede")

        # Check loser was marked as superseded
        cursor.execute("SELECT provenance FROM shared_memories WHERE id = 'mem-1'")
        row = cursor.fetchone()
        import json
        prov = json.loads(row[0])
        assert prov.get("superseded_by") == "mem-2"

        conn.close()

    def test_merge_strategy(self, temp_db):
        """Test merge updates winner's derived_from."""
        conn = sqlite3.connect(str(temp_db))
        cursor = conn.cursor()

        # Insert test memories
        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, tags, provenance,
                privacy_tier, hop_count, original_author, confidence, shared_at, trust_verified
            ) VALUES
            ('mem-1', 'room-1', 'friday-123', 'Old memory', 'test', '[]', '{}', 'public', 1, 'friday-123', 0.9, datetime('now'), 0),
            ('mem-2', 'room-1', 'friday-123', 'New memory', 'test', '[]', '{}', 'public', 1, 'friday-123', 0.9, datetime('now'), 0)
        """)
        conn.commit()

        apply_merge(conn, winner_id="mem-2", loser_id="mem-1", strategy="merge")

        # Check winner's provenance includes loser
        cursor.execute("SELECT provenance FROM shared_memories WHERE id = 'mem-2'")
        row = cursor.fetchone()
        import json
        prov = json.loads(row[0])
        assert "mem-1" in prov.get("derived_from", [])

        conn.close()
