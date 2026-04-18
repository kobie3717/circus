"""Unit tests for provenance tracking and confidence decay.

Tests the decay formulas from Circus Memory Commons spec §5.
"""

import math
from datetime import datetime, timedelta

import pytest

from circus.services.provenance import (
    build_provenance,
    decay_confidence,
    verify_provenance_chain,
    HOP_DECAY_RATE,
    HOP_DECAY_FLOOR,
    AGE_HALF_LIFE_DAYS,
    TRUST_TIER_ELDER,
    TRUST_TIER_TRUSTED,
    TRUST_TIER_ESTABLISHED,
    TRUST_BONUS_ELDER,
    TRUST_BONUS_TRUSTED,
    TRUST_BONUS_ESTABLISHED,
    TRUST_BONUS_NEWCOMER,
)


class TestBuildProvenance:
    """Test provenance metadata construction."""

    def test_minimal_provenance(self):
        """Test minimal provenance with only author."""
        prov = build_provenance("claw-abc123")

        assert prov["hop_count"] == 1
        assert prov["original_author"] == "claw-abc123"
        assert "original_timestamp" in prov
        assert "derived_from" not in prov
        assert "citations" not in prov
        assert "reasoning" not in prov

    def test_full_provenance(self):
        """Test provenance with all fields."""
        prov = build_provenance(
            "friday-xyz",
            derived_from=["mem-123", "mem-456"],
            citations=["https://docs.example.com"],
            reasoning="Confirmed via testing"
        )

        assert prov["hop_count"] == 1
        assert prov["original_author"] == "friday-xyz"
        assert prov["derived_from"] == ["mem-123", "mem-456"]
        assert prov["citations"] == ["https://docs.example.com"]
        assert prov["reasoning"] == "Confirmed via testing"

    def test_timestamp_format(self):
        """Test timestamp is valid ISO format."""
        prov = build_provenance("test-agent")
        timestamp = datetime.fromisoformat(prov["original_timestamp"])
        assert isinstance(timestamp, datetime)


class TestDecayConfidence:
    """Test confidence decay formula."""

    def test_hop_decay_first_hop(self):
        """Test first hop has no decay."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=0.0,
            author_trust_score=50.0
        )
        # First hop: hop_penalty = 1.0, age_penalty = 1.0, trust_bonus = 1.0
        assert effective == pytest.approx(0.9, abs=0.01)

    def test_hop_decay_second_hop(self):
        """Test second hop has 5% decay."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=2,
            age_seconds=0.0,
            author_trust_score=50.0  # Established
        )
        # hop_penalty = 0.95, age_penalty = 1.0, trust_bonus = 1.0
        expected = 0.9 * 0.95
        assert effective == pytest.approx(expected, abs=0.01)

    def test_hop_decay_third_hop(self):
        """Test third hop has 10% decay."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=3,
            age_seconds=0.0,
            author_trust_score=50.0
        )
        # hop_penalty = 0.90
        expected = 0.9 * 0.90
        assert effective == pytest.approx(expected, abs=0.01)

    def test_hop_decay_floor(self):
        """Test hop decay floors at 0.5."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=20,  # Would be negative without floor
            age_seconds=0.0,
            author_trust_score=50.0
        )
        # hop_penalty should floor at 0.5
        expected = 0.9 * 0.5
        assert effective == pytest.approx(expected, abs=0.01)

    def test_age_decay_fresh(self):
        """Test fresh memory (0 age) has no decay."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=0.0,
            author_trust_score=50.0
        )
        assert effective == pytest.approx(0.9, abs=0.01)

    def test_age_decay_2_days(self):
        """Test age decay after 2 days."""
        age_seconds = 2 * 86400.0
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=age_seconds,
            author_trust_score=50.0
        )
        # age_penalty = exp(-2/90 * ln(2))
        age_penalty = math.exp(-2 / AGE_HALF_LIFE_DAYS * math.log(2))
        expected = 0.9 * age_penalty
        assert effective == pytest.approx(expected, abs=0.01)

    def test_age_decay_90_days(self):
        """Test age decay at half-life (90 days)."""
        age_seconds = 90 * 86400.0
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=age_seconds,
            author_trust_score=50.0
        )
        # age_penalty = exp(-90/90 * ln(2)) = exp(-ln(2)) = 0.5
        expected = 0.9 * 0.5
        assert effective == pytest.approx(expected, abs=0.01)

    def test_age_decay_180_days(self):
        """Test age decay after 180 days (2 half-lives)."""
        age_seconds = 180 * 86400.0
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=age_seconds,
            author_trust_score=50.0
        )
        # age_penalty = exp(-180/90 * ln(2)) = exp(-2*ln(2)) = 0.25
        expected = 0.9 * 0.25
        assert effective == pytest.approx(expected, abs=0.01)

    def test_trust_bonus_elder(self):
        """Test Elder tier gets +20% bonus."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=0.0,
            author_trust_score=TRUST_TIER_ELDER
        )
        # 0.9 * 1.2 = 1.08, but clamped to 1.0 ceiling
        assert effective == pytest.approx(1.0, abs=0.01)

    def test_trust_bonus_trusted(self):
        """Test Trusted tier gets +10% bonus."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=0.0,
            author_trust_score=TRUST_TIER_TRUSTED
        )
        expected = 0.9 * TRUST_BONUS_TRUSTED
        assert effective == pytest.approx(expected, abs=0.01)

    def test_trust_bonus_established(self):
        """Test Established tier gets no bonus."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=0.0,
            author_trust_score=TRUST_TIER_ESTABLISHED
        )
        expected = 0.9 * TRUST_BONUS_ESTABLISHED
        assert effective == pytest.approx(expected, abs=0.01)

    def test_trust_bonus_newcomer(self):
        """Test Newcomer tier gets -10% penalty."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=0.0,
            author_trust_score=20.0  # Below Established
        )
        expected = 0.9 * TRUST_BONUS_NEWCOMER
        assert effective == pytest.approx(expected, abs=0.01)

    def test_combined_decay(self):
        """Test all decay factors combined."""
        # Scenario: hop=2, 10 days old, Trusted author (score 72)
        age_seconds = 10 * 86400.0
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=2,
            age_seconds=age_seconds,
            author_trust_score=72.0
        )

        hop_penalty = 0.95  # Second hop
        age_penalty = math.exp(-10 / AGE_HALF_LIFE_DAYS * math.log(2))
        trust_bonus = TRUST_BONUS_TRUSTED

        expected = 0.9 * hop_penalty * age_penalty * trust_bonus
        assert effective == pytest.approx(expected, abs=0.01)

    def test_confidence_floor(self):
        """Test confidence floors at 0.1."""
        effective = decay_confidence(
            base_confidence=0.1,
            hop_count=20,  # Max hop penalty
            age_seconds=180 * 86400.0,  # Old
            author_trust_score=10.0  # Newcomer penalty
        )
        # Should clamp to 0.1 minimum
        assert effective >= 0.1

    def test_confidence_ceiling(self):
        """Test confidence caps at 1.0."""
        effective = decay_confidence(
            base_confidence=0.9,
            hop_count=1,
            age_seconds=0.0,
            author_trust_score=90.0  # Elder with +20%
        )
        # 0.9 * 1.2 = 1.08, should clamp to 1.0
        assert effective == pytest.approx(1.0, abs=0.01)


class TestVerifyProvenanceChain:
    """Test provenance verification."""

    def test_valid_minimal_provenance(self):
        """Test valid minimal provenance passes."""
        prov = {
            "hop_count": 1,
            "original_author": "test-agent",
            "original_timestamp": datetime.utcnow().isoformat()
        }
        assert verify_provenance_chain(prov) is True

    def test_valid_full_provenance(self):
        """Test valid full provenance passes."""
        prov = {
            "hop_count": 2,
            "original_author": "test-agent",
            "original_timestamp": datetime.utcnow().isoformat(),
            "derived_from": ["mem-1", "mem-2"],
            "citations": ["https://example.com"],
            "reasoning": "Test reasoning"
        }
        assert verify_provenance_chain(prov) is True

    def test_missing_hop_count(self):
        """Test missing hop_count fails."""
        prov = {
            "original_author": "test-agent",
            "original_timestamp": datetime.utcnow().isoformat()
        }
        assert verify_provenance_chain(prov) is False

    def test_missing_original_author(self):
        """Test missing original_author fails."""
        prov = {
            "hop_count": 1,
            "original_timestamp": datetime.utcnow().isoformat()
        }
        assert verify_provenance_chain(prov) is False

    def test_missing_timestamp(self):
        """Test missing timestamp fails."""
        prov = {
            "hop_count": 1,
            "original_author": "test-agent"
        }
        assert verify_provenance_chain(prov) is False

    def test_invalid_hop_count_zero(self):
        """Test hop_count = 0 fails."""
        prov = {
            "hop_count": 0,
            "original_author": "test-agent",
            "original_timestamp": datetime.utcnow().isoformat()
        }
        assert verify_provenance_chain(prov) is False

    def test_invalid_hop_count_negative(self):
        """Test negative hop_count fails."""
        prov = {
            "hop_count": -1,
            "original_author": "test-agent",
            "original_timestamp": datetime.utcnow().isoformat()
        }
        assert verify_provenance_chain(prov) is False

    def test_invalid_timestamp_format(self):
        """Test invalid timestamp format fails."""
        prov = {
            "hop_count": 1,
            "original_author": "test-agent",
            "original_timestamp": "not-a-timestamp"
        }
        assert verify_provenance_chain(prov) is False

    def test_empty_original_author(self):
        """Test empty original_author fails."""
        prov = {
            "hop_count": 1,
            "original_author": "",
            "original_timestamp": datetime.utcnow().isoformat()
        }
        assert verify_provenance_chain(prov) is False

    def test_invalid_derived_from_type(self):
        """Test non-list derived_from fails."""
        prov = {
            "hop_count": 1,
            "original_author": "test-agent",
            "original_timestamp": datetime.utcnow().isoformat(),
            "derived_from": "not-a-list"
        }
        assert verify_provenance_chain(prov) is False

    def test_invalid_citations_type(self):
        """Test non-list citations fails."""
        prov = {
            "hop_count": 1,
            "original_author": "test-agent",
            "original_timestamp": datetime.utcnow().isoformat(),
            "citations": "not-a-list"
        }
        assert verify_provenance_chain(prov) is False

    def test_invalid_reasoning_type(self):
        """Test non-string reasoning fails."""
        prov = {
            "hop_count": 1,
            "original_author": "test-agent",
            "original_timestamp": datetime.utcnow().isoformat(),
            "reasoning": 123
        }
        assert verify_provenance_chain(prov) is False
