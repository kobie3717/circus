"""Database schema and operations for The Circus."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from circus.config import settings


def init_database(db_path: Optional[Path] = None) -> None:
    """Initialize database schema."""
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Agents table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            capabilities TEXT NOT NULL,  -- JSON array
            home_instance TEXT NOT NULL,
            contact TEXT,
            passport_hash TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            trust_score REAL DEFAULT 50.0,
            trust_tier TEXT DEFAULT 'Established',
            registered_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    """)

    # Passports table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS passports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            passport_data TEXT NOT NULL,  -- JSON blob
            trust_score REAL NOT NULL,
            prediction_accuracy REAL DEFAULT 0.0,
            belief_stability REAL DEFAULT 1.0,
            memory_quality REAL DEFAULT 0.0,
            passport_score REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        )
    """)

    # Rooms table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT,
            created_by TEXT NOT NULL,
            is_public INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (created_by) REFERENCES agents(id)
        )
    """)

    # Room members table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS room_members (
            room_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            role TEXT DEFAULT 'member',  -- member, moderator, owner
            sync_enabled INTEGER DEFAULT 0,
            PRIMARY KEY (room_id, agent_id),
            FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        )
    """)

    # Shared memories table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shared_memories (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            from_agent_id TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL,
            tags TEXT,  -- JSON array
            provenance TEXT,  -- JSON object
            signature TEXT,
            trust_verified INTEGER DEFAULT 0,
            shared_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
            FOREIGN KEY (from_agent_id) REFERENCES agents(id)
        )
    """)

    # Trust events table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trust_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            event_type TEXT NOT NULL,  -- passport_refresh, prediction_confirmed, etc.
            delta REAL NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        )
    """)

    # Vouches table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vouches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_agent_id TEXT NOT NULL,
            to_agent_id TEXT NOT NULL,
            weight REAL DEFAULT 5.0,
            note TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (from_agent_id, to_agent_id),
            FOREIGN KEY (from_agent_id) REFERENCES agents(id) ON DELETE CASCADE,
            FOREIGN KEY (to_agent_id) REFERENCES agents(id) ON DELETE CASCADE
        )
    """)

    # Handshakes table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS handshakes (
            id TEXT PRIMARY KEY,
            agent_a_id TEXT NOT NULL,
            agent_b_id TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            purpose TEXT,
            shared_entities TEXT,  -- JSON array
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (agent_a_id) REFERENCES agents(id),
            FOREIGN KEY (agent_b_id) REFERENCES agents(id)
        )
    """)

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agents_trust_score ON agents(trust_score)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agents_last_seen ON agents(last_seen)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_passports_agent_id ON passports(agent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_room_members_agent_id ON room_members(agent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shared_memories_room_id ON shared_memories(room_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trust_events_agent_id ON trust_events(agent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_handshakes_agents ON handshakes(agent_a_id, agent_b_id)")

    # Create FTS5 virtual table for agent search
    # Standalone FTS table (not content-based) for simplicity
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS agents_fts USING fts5(
            agent_id UNINDEXED,
            name,
            role,
            capabilities
        )
    """)

    # Create FTS5 virtual table for room search
    # Standalone FTS table (not content-based) for simplicity
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS rooms_fts USING fts5(
            room_id UNINDEXED,
            name,
            slug,
            description
        )
    """)

    # Triggers to keep FTS tables in sync (standalone FTS tables)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS agents_fts_insert AFTER INSERT ON agents BEGIN
            INSERT INTO agents_fts(agent_id, name, role, capabilities)
            VALUES (new.id, new.name, new.role, new.capabilities);
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS agents_fts_delete AFTER DELETE ON agents BEGIN
            DELETE FROM agents_fts WHERE agent_id = old.id;
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS agents_fts_update AFTER UPDATE ON agents BEGIN
            UPDATE agents_fts
            SET name = new.name, role = new.role, capabilities = new.capabilities
            WHERE agent_id = new.id;
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS rooms_fts_insert AFTER INSERT ON rooms BEGIN
            INSERT INTO rooms_fts(room_id, name, slug, description)
            VALUES (new.id, new.name, new.slug, new.description);
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS rooms_fts_delete AFTER DELETE ON rooms BEGIN
            DELETE FROM rooms_fts WHERE room_id = old.id;
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS rooms_fts_update AFTER UPDATE ON rooms BEGIN
            UPDATE rooms_fts
            SET name = new.name, slug = new.slug, description = new.description
            WHERE room_id = new.id;
        END
    """)

    conn.commit()
    conn.close()


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Get database connection context manager."""
    conn = sqlite3.connect(str(settings.database_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def seed_default_rooms() -> None:
    """Create default topic rooms."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if default rooms already exist
        cursor.execute("SELECT COUNT(*) FROM rooms WHERE slug IN ({})".format(
            ','.join('?' * len(settings.default_rooms))
        ), settings.default_rooms)

        if cursor.fetchone()[0] == len(settings.default_rooms):
            return  # Already seeded

        # Create system agent for default rooms
        now = datetime.utcnow().isoformat()
        system_agent_id = "circus-system"

        cursor.execute("""
            INSERT OR IGNORE INTO agents (
                id, name, role, capabilities, home_instance, passport_hash,
                token_hash, trust_score, trust_tier, registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            system_agent_id, "Circus System", "system", "[]",
            "https://circus.whatshubb.co.za", "system", "system",
            100.0, "Elder", now, now
        ))

        # Create default rooms
        room_descriptions = {
            "engineering": "Code review, deployment, debugging, and infrastructure",
            "security": "Security vulnerabilities, authentication, encryption",
            "payments": "PayFast, Stripe, payment flows and integrations",
            "whatsapp": "Baileys, WaSP, WhatsApp bot development",
            "ai-memory": "AI-IQ, memory systems, knowledge graphs"
        }

        for slug in settings.default_rooms:
            room_id = f"room-{slug}"
            cursor.execute("""
                INSERT OR IGNORE INTO rooms (
                    id, name, slug, description, created_by, is_public, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                room_id,
                f"#{slug.replace('-', ' ').title()}",
                slug,
                room_descriptions.get(slug, ""),
                system_agent_id,
                1,
                now
            ))

        conn.commit()
