"""Tests for AI-IQ and Passport trust bridges."""

import sqlite3
import subprocess
from datetime import datetime
from unittest import mock

import pytest

from circus.services.aiiq_bridge import sync_preference_to_aiiq, clear_preference_in_aiiq
from circus.services.passport_trust import get_passport_multiplier, apply_passport_trust


# AI-IQ Bridge Tests


@mock.patch('subprocess.run')
def test_aiiq_sync_called_on_preference_activation(mock_run):
    """Test that memory-tool add is called with correct parameters when preference is activated."""
    mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")

    result = sync_preference_to_aiiq(
        owner_id="user-123",
        field="user.language_preference",
        value="af",
        confidence=0.85,
        reasoning="detected from conversation"
    )

    assert result is True
    mock_run.assert_called_once()

    # Verify call arguments
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "memory-tool"
    assert call_args[1] == "add"
    assert call_args[2] == "preference"
    assert "user-123 prefers user.language_preference=af (confidence=0.85)" in call_args[3]
    assert "detected from conversation" in call_args[3]
    assert "--tags" in call_args
    assert "circus,preference,user-123" in call_args
    assert "--key" in call_args
    assert "circus-pref-user-123-user-language_preference" in call_args
    assert "--priority" in call_args
    assert "8" in call_args  # int(0.85 * 10) = 8


@mock.patch('subprocess.run')
def test_aiiq_sync_non_fatal(mock_run):
    """Test that AI-IQ sync failures don't raise exceptions (non-fatal)."""
    # Simulate subprocess failure
    mock_run.side_effect = Exception("subprocess error")

    # Should not raise, returns False
    result = sync_preference_to_aiiq(
        owner_id="user-123",
        field="user.theme",
        value="dark",
        confidence=0.7
    )

    assert result is False


@mock.patch('subprocess.run')
def test_aiiq_clear_called_on_preference_delete(mock_run):
    """Test that memory-tool delete is called when preference is cleared."""
    # Mock search result with memory IDs
    search_result = mock.Mock(returncode=0, stdout="#1234 [preference]\n#5678 [preference]\n", stderr="")
    delete_result = mock.Mock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = [search_result, delete_result, delete_result]

    result = clear_preference_in_aiiq("user-123", "user.language_preference")

    assert result is True
    assert mock_run.call_count == 3  # 1 search + 2 deletes

    # Verify search call
    search_call = mock_run.call_args_list[0][0][0]
    assert search_call[0] == "memory-tool"
    assert search_call[1] == "search"
    assert search_call[2] == "user.language_preference"

    # Verify delete calls
    delete_call_1 = mock_run.call_args_list[1][0][0]
    assert delete_call_1[0] == "memory-tool"
    assert delete_call_1[1] == "delete"
    assert delete_call_1[2] == "1234"

    delete_call_2 = mock_run.call_args_list[2][0][0]
    assert delete_call_2[2] == "5678"


# Passport Trust Tests


def test_high_passport_boosts_confidence():
    """Test that high passport score (>=80) applies 1.10 multiplier."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    # Create passports table
    cursor.execute("""
        CREATE TABLE passports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            passport_score REAL DEFAULT 0.0
        )
    """)

    # Insert passport with score 90
    cursor.execute("INSERT INTO passports (agent_id, passport_score) VALUES (?, ?)", ("agent-1", 90.0))
    conn.commit()

    multiplier = get_passport_multiplier(conn, "agent-1")
    assert multiplier == 1.10

    # Test with confidence 0.8
    adjusted = apply_passport_trust(conn, "agent-1", 0.8)
    assert adjusted == pytest.approx(0.88)  # 0.8 * 1.10 = 0.88

    conn.close()


def test_medium_passport_no_change():
    """Test that medium passport score (50-79) applies 1.0 multiplier."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE passports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            passport_score REAL DEFAULT 0.0
        )
    """)

    # Insert passport with score 60
    cursor.execute("INSERT INTO passports (agent_id, passport_score) VALUES (?, ?)", ("agent-2", 60.0))
    conn.commit()

    multiplier = get_passport_multiplier(conn, "agent-2")
    assert multiplier == 1.00

    # Test with confidence 0.7
    adjusted = apply_passport_trust(conn, "agent-2", 0.7)
    assert adjusted == 0.7  # 0.7 * 1.0 = 0.7

    conn.close()


def test_low_passport_penalizes():
    """Test that low passport score (20-49) applies 0.90 multiplier."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE passports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            passport_score REAL DEFAULT 0.0
        )
    """)

    # Insert passport with score 30
    cursor.execute("INSERT INTO passports (agent_id, passport_score) VALUES (?, ?)", ("agent-3", 30.0))
    conn.commit()

    multiplier = get_passport_multiplier(conn, "agent-3")
    assert multiplier == 0.90

    # Test with confidence 0.8
    adjusted = apply_passport_trust(conn, "agent-3", 0.8)
    assert adjusted == pytest.approx(0.72)  # 0.8 * 0.90 = 0.72

    conn.close()


def test_no_passport_penalizes():
    """Test that no passport row applies 0.85 multiplier."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE passports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            passport_score REAL DEFAULT 0.0
        )
    """)
    conn.commit()

    # No passport for agent-4
    multiplier = get_passport_multiplier(conn, "agent-4")
    assert multiplier == 0.85

    # Test with confidence 0.8
    adjusted = apply_passport_trust(conn, "agent-4", 0.8)
    assert adjusted == pytest.approx(0.68)  # 0.8 * 0.85 = 0.68

    conn.close()


def test_passport_trust_clamped():
    """Test that very high confidence * 1.10 is still clamped to 1.0."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE passports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            passport_score REAL DEFAULT 0.0
        )
    """)

    # Insert passport with high score
    cursor.execute("INSERT INTO passports (agent_id, passport_score) VALUES (?, ?)", ("agent-5", 95.0))
    conn.commit()

    multiplier = get_passport_multiplier(conn, "agent-5")
    assert multiplier == 1.10

    # Test with confidence 0.95 (would be 1.045 without clamping)
    adjusted = apply_passport_trust(conn, "agent-5", 0.95)
    assert adjusted == 1.0  # clamped to max

    # Test with confidence 1.0 (edge case)
    adjusted_max = apply_passport_trust(conn, "agent-5", 1.0)
    assert adjusted_max == 1.0

    # Test with very low confidence (should clamp to 0.0)
    adjusted_min = apply_passport_trust(conn, "agent-5", 0.0)
    assert adjusted_min == 0.0

    conn.close()
