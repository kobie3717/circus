"""Test database schema and operations."""

import tempfile
from pathlib import Path

import pytest

from circus.database import get_db, init_database, seed_default_rooms


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    init_database(db_path)
    yield db_path

    # Cleanup
    db_path.unlink()


def test_database_initialization(temp_db):
    """Test database schema creation."""
    from circus.database import get_db as _get_db
    from circus.config import settings

    # Override database path for test
    original_path = settings.database_path
    settings.database_path = temp_db

    try:
        with _get_db() as conn:
            cursor = conn.cursor()

            # Check tables exist
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table'
                ORDER BY name
            """)

            tables = {row[0] for row in cursor.fetchall()}

            expected_tables = {
                "agents", "passports", "rooms", "room_members",
                "shared_memories", "trust_events", "vouches", "handshakes",
                "agents_fts", "rooms_fts"
            }

            assert expected_tables.issubset(tables)
    finally:
        settings.database_path = original_path


def test_seed_default_rooms(temp_db):
    """Test seeding default rooms."""
    from circus.config import settings

    original_path = settings.database_path
    settings.database_path = temp_db

    try:
        seed_default_rooms()

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM rooms")
            count = cursor.fetchone()[0]

            assert count == len(settings.default_rooms)

            # Check room slugs
            cursor.execute("SELECT slug FROM rooms")
            slugs = {row[0] for row in cursor.fetchall()}

            assert slugs == set(settings.default_rooms)
    finally:
        settings.database_path = original_path


def test_fts_search(temp_db):
    """Test FTS5 full-text search."""
    from circus.config import settings
    from datetime import datetime

    original_path = settings.database_path
    settings.database_path = temp_db

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Insert test agent
            now = datetime.utcnow().isoformat()
            cursor.execute("""
                INSERT INTO agents (
                    id, name, role, capabilities, home_instance,
                    passport_hash, token_hash, registered_at, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "test-001", "Test Agent", "testing",
                '["testing", "debugging"]', "http://test.example.com",
                "hash123", "tokenhash", now, now
            ))

            conn.commit()

            # Search using FTS
            cursor.execute("""
                SELECT agent_id, name FROM agents_fts WHERE agents_fts MATCH ?
            """, ("testing",))

            results = cursor.fetchall()
            assert len(results) == 1
            assert results[0][0] == "test-001"
    finally:
        settings.database_path = original_path
