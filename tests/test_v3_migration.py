"""Tests for v3 federation migration (schema hardening)."""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from circus.database import init_database, run_v3_migration


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary database for testing."""
    db_path = tmp_path / "test_circus.db"
    init_database(db_path)
    yield db_path


def test_migration_adds_domain_column(temp_db):
    """Test that migration adds domain column to shared_memories."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    # Check column exists
    cursor.execute("PRAGMA table_info(shared_memories)")
    columns = {row[1] for row in cursor.fetchall()}

    assert 'domain' in columns, "domain column should exist after migration"
    conn.close()


def test_migration_creates_quarantine_table(temp_db):
    """Test that migration creates all federation tables and indexes."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    # Check federation tables exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}

    assert 'federation_seen' in tables
    assert 'federation_quarantine' in tables
    assert 'federation_audit' in tables

    # Check quarantine table structure
    cursor.execute("PRAGMA table_info(federation_quarantine)")
    quarantine_columns = {row[1] for row in cursor.fetchall()}
    expected_columns = {
        'id', 'memory_id', 'source_instance', 'source_passport_hash',
        'reason', 'payload', 'received_at', 'expires_at',
        'reviewed_at', 'reviewed_by_passport', 'review_action', 'review_reason'
    }
    assert expected_columns.issubset(quarantine_columns)

    # Check index exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='federation_quarantine'")
    indexes = {row[0] for row in cursor.fetchall()}
    assert 'idx_federation_quarantine_expires' in indexes

    conn.close()


def test_migration_backfills_existing_rows(temp_db):
    """Test that migration backfills domain from category for existing rows."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    # Pre-seed memories with NULL domain (simulating pre-migration state)
    now = datetime.utcnow().isoformat()
    test_memories = [
        ('mem-1', 'room-test', 'agent-1', 'Test memory 1', 'architecture', '["tag1"]', now),
        ('mem-2', 'room-test', 'agent-2', 'Test memory 2', 'user-preferences', '["tag2"]', now),
    ]

    # Create test room and agent first
    cursor.execute("""
        INSERT INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test', 'Test Room', 'test-room', 'Test', 'agent-1', 1, ?)
    """, (now,))

    cursor.execute("""
        INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, registered_at, last_seen)
        VALUES ('agent-1', 'Test Agent 1', 'bot', '["test"]', 'local', 'hash1', 'token1', ?, ?),
               ('agent-2', 'Test Agent 2', 'bot', '["test"]', 'local', 'hash2', 'token2', ?, ?)
    """, (now, now, now, now))

    # Reset domain to NULL (simulating pre-v3 state)
    for mem_id, room_id, agent_id, content, category, tags, shared_at in test_memories:
        cursor.execute("""
            INSERT INTO shared_memories (id, room_id, from_agent_id, content, category, tags, shared_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (mem_id, room_id, agent_id, content, category, tags, shared_at))
        cursor.execute("UPDATE shared_memories SET domain = NULL WHERE id = ?", (mem_id,))

    conn.commit()

    # Verify domain is NULL before re-running migration
    cursor.execute("SELECT id, category, domain FROM shared_memories WHERE id IN ('mem-1', 'mem-2')")
    before_rows = cursor.fetchall()
    for row_id, category, domain in before_rows:
        assert domain is None, f"domain should be NULL before backfill for {row_id}"

    conn.close()

    # Re-run migration to trigger backfill
    run_v3_migration(temp_db)

    # Verify domain was backfilled
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    cursor.execute("SELECT id, category, domain FROM shared_memories WHERE id = 'mem-1'")
    mem1 = cursor.fetchone()
    assert mem1[2] == 'architecture', "domain should be backfilled from category"

    cursor.execute("SELECT id, category, domain FROM shared_memories WHERE id = 'mem-2'")
    mem2 = cursor.fetchone()
    assert mem2[2] == 'user-preferences', "domain should be backfilled from category"

    conn.close()


def test_migration_skips_malformed_category(temp_db):
    """Test that migration skips rows with empty/NULL category."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    now = datetime.utcnow().isoformat()

    # Create test room and agent
    cursor.execute("""
        INSERT INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test2', 'Test Room 2', 'test-room-2', 'Test', 'agent-3', 1, ?)
    """, (now,))

    cursor.execute("""
        INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, registered_at, last_seen)
        VALUES ('agent-3', 'Test Agent 3', 'bot', '["test"]', 'local', 'hash3', 'token3', ?, ?)
    """, (now, now))

    # Insert memory with empty category
    cursor.execute("""
        INSERT INTO shared_memories (id, room_id, from_agent_id, content, category, tags, shared_at)
        VALUES ('mem-malformed', 'room-test2', 'agent-3', 'Test memory', '', '[]', ?)
    """, (now,))

    # Force domain to NULL
    cursor.execute("UPDATE shared_memories SET domain = NULL WHERE id = 'mem-malformed'")
    conn.commit()
    conn.close()

    # Re-run migration
    run_v3_migration(temp_db)

    # Verify memory still has NULL domain (skipped backfill)
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT domain FROM shared_memories WHERE id = 'mem-malformed'")
    domain = cursor.fetchone()[0]

    # Should be None or remain NULL (migration didn't fail)
    assert domain is None or domain == '', "malformed category should skip backfill"
    conn.close()


def test_migration_logs_backfill_count(temp_db):
    """Test that migration logs backfill count to federation_audit."""
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()

    now = datetime.utcnow().isoformat()

    # Create test data
    cursor.execute("""
        INSERT INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-test3', 'Test Room 3', 'test-room-3', 'Test', 'agent-4', 1, ?)
    """, (now,))

    cursor.execute("""
        INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, registered_at, last_seen)
        VALUES ('agent-4', 'Test Agent 4', 'bot', '["test"]', 'local', 'hash4', 'token4', ?, ?)
    """, (now, now))

    cursor.execute("""
        INSERT INTO shared_memories (id, room_id, from_agent_id, content, category, tags, shared_at)
        VALUES ('mem-audit', 'room-test3', 'agent-4', 'Test', 'test-category', '[]', ?)
    """, (now,))

    # Force domain to NULL
    cursor.execute("UPDATE shared_memories SET domain = NULL WHERE id = 'mem-audit'")
    conn.commit()
    conn.close()

    # Re-run migration
    run_v3_migration(temp_db)

    # Check federation_audit for backfill_run entry
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT action, metadata FROM federation_audit
        WHERE action = 'backfill_run'
    """)
    audit_rows = cursor.fetchall()

    # Should have at least one backfill_run entry
    assert len(audit_rows) > 0, "should log backfill_run to federation_audit"

    # Check metadata contains rows_backfilled
    for action, metadata_json in audit_rows:
        metadata = json.loads(metadata_json)
        assert 'rows_backfilled' in metadata
        assert 'rows_skipped' in metadata
        # At least 1 row should have been backfilled (mem-audit)
        if metadata['rows_backfilled'] > 0:
            break
    else:
        pytest.fail("Expected at least one backfill_run with rows_backfilled > 0")

    conn.close()


def test_migration_idempotent(temp_db):
    """Test that migration can be run multiple times without errors."""
    # Migration already ran once during temp_db fixture
    # Run again
    run_v3_migration(temp_db)

    # Should not raise, tables should still exist
    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='federation_quarantine'")
    assert cursor.fetchone() is not None

    # Check domain column still exists
    cursor.execute("PRAGMA table_info(shared_memories)")
    columns = {row[1] for row in cursor.fetchall()}
    assert 'domain' in columns

    conn.close()
