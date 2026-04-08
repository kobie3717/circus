"""Test trust score calculation and management."""

import pytest

from circus.services.trust import (
    apply_trust_decay,
    calculate_trust_score,
    can_create_room,
    can_moderate,
    can_vouch,
    get_trust_tier,
    get_vouch_cost,
)


def test_calculate_trust_score():
    """Test trust score calculation."""
    # Perfect agent
    score = calculate_trust_score(
        prediction_accuracy=1.0,
        belief_stability=1.0,
        memory_quality=1.0,
        passport_score=10.0,
        days_active=365
    )

    assert score == 100.0

    # New agent with decent stats
    score = calculate_trust_score(
        prediction_accuracy=0.8,
        belief_stability=0.9,
        memory_quality=0.6,
        passport_score=7.0,
        days_active=0
    )

    assert 50 <= score <= 80

    # Poor agent
    score = calculate_trust_score(
        prediction_accuracy=0.3,
        belief_stability=0.5,
        memory_quality=0.2,
        passport_score=3.0,
        days_active=0
    )

    assert score < 40


def test_get_trust_tier():
    """Test trust tier assignment."""
    assert get_trust_tier(25) == "Newcomer"
    assert get_trust_tier(45) == "Established"
    assert get_trust_tier(70) == "Trusted"
    assert get_trust_tier(90) == "Elder"


def test_apply_trust_decay():
    """Test trust decay application."""
    # No decay for active agent
    new_trust, events = apply_trust_decay(
        current_trust=80.0,
        days_since_activity=10,
        failed_predictions=0,
        contradictions=0,
        passport_age_days=15
    )

    assert new_trust == 80.0
    assert len(events) == 0

    # Decay for 30-day inactivity
    new_trust, events = apply_trust_decay(
        current_trust=80.0,
        days_since_activity=45,
        failed_predictions=0,
        contradictions=0,
        passport_age_days=15
    )

    assert new_trust < 80.0
    assert len(events) > 0
    assert any(e["event_type"] == "inactivity_decay_30d" for e in events)

    # Decay for failed predictions
    new_trust, events = apply_trust_decay(
        current_trust=80.0,
        days_since_activity=10,
        failed_predictions=3,
        contradictions=0,
        passport_age_days=15
    )

    assert new_trust == 65.0  # 80 - (3 * 5)
    assert any(e["event_type"] == "failed_predictions" for e in events)

    # Decay for stale passport
    new_trust, events = apply_trust_decay(
        current_trust=80.0,
        days_since_activity=10,
        failed_predictions=0,
        contradictions=0,
        passport_age_days=45
    )

    assert new_trust == 70.0  # 80 - 10
    assert any(e["event_type"] == "stale_passport" for e in events)


def test_permissions():
    """Test permission checks."""
    # Newcomer (0-30)
    assert not can_create_room(25)
    assert not can_vouch(25)
    assert not can_moderate(25)

    # Established (30-60)
    assert not can_create_room(45)
    assert not can_vouch(45)
    assert not can_moderate(45)

    # Trusted (60-85)
    assert can_create_room(70)
    assert can_vouch(70)
    assert not can_moderate(70)

    # Elder (85-100)
    assert can_create_room(90)
    assert can_vouch(90)
    assert can_moderate(90)


def test_vouch_cost():
    """Test vouch cost calculation."""
    # Newcomer/Established pay 2 points
    assert get_vouch_cost(25) == 2.0
    assert get_vouch_cost(45) == 2.0

    # Trusted pay 2 points
    assert get_vouch_cost(70) == 2.0

    # Elders vouch for free
    assert get_vouch_cost(90) == 0.0
