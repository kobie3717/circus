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

    # Run v2 migration after base schema
    is_new_db = not db_path.exists() or db_path.stat().st_size == 0

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
            public_key BLOB,
            signed_card TEXT,
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

    # Tasks table (A2A task lifecycle)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            from_agent_id TEXT NOT NULL,
            to_agent_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            payload TEXT NOT NULL,       -- JSON blob
            state TEXT DEFAULT 'submitted',
            result TEXT,                 -- JSON blob (when completed)
            error TEXT,                  -- Error message (when failed)
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deadline TEXT,
            FOREIGN KEY (from_agent_id) REFERENCES agents(id),
            FOREIGN KEY (to_agent_id) REFERENCES agents(id),
            CHECK (state IN ('submitted', 'working', 'input-required', 'completed', 'failed', 'canceled'))
        )
    """)

    # Task state transitions table (for audit log)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_state_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)

    # Audit log table (OWASP security)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            action TEXT NOT NULL,
            resource_type TEXT,
            resource_id TEXT,
            trust_tier TEXT,
            allowed INTEGER NOT NULL,
            reason TEXT,
            ip_address TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # Federation peers table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS federation_peers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT UNIQUE NOT NULL,
            public_key BLOB NOT NULL,
            trust_score REAL DEFAULT 50.0,
            last_sync TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    # Federation sync log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS federation_sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id TEXT NOT NULL,
            direction TEXT NOT NULL,  -- 'pull' or 'push'
            agents_synced INTEGER DEFAULT 0,
            status TEXT NOT NULL,     -- 'success' or 'failed'
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (peer_id) REFERENCES federation_peers(id)
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_from_agent ON tasks(from_agent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_to_agent ON tasks(to_agent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_agent ON audit_log(agent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at)")

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

    # Agent embeddings table (for semantic search)
    # Store both blob (for sqlite-vec) and JSON (for fallback)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_embeddings (
            agent_id TEXT PRIMARY KEY,
            embedding BLOB,
            embedding_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        )
    """)

    # Agent competence table (per-domain scoring)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_competence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            score REAL DEFAULT 0.5,
            observations INTEGER DEFAULT 0,
            last_updated TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
            UNIQUE(agent_id, domain)
        )
    """)

    # Create indexes for competence table
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_competence_agent_id ON agent_competence(agent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_competence_domain ON agent_competence(domain)")

    # Try to enable sqlite-vec if available
    try:
        conn.enable_load_extension(True)
        vec_loaded = False
        for ext_path in ["vec0", "/usr/local/lib/vec0.so", "/usr/lib/vec0.so"]:
            try:
                conn.load_extension(ext_path)
                vec_loaded = True
                break
            except sqlite3.OperationalError:
                continue
        conn.enable_load_extension(False)

        if vec_loaded:
            # Create optimized vector index if sqlite-vec is available
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_embeddings_vec
                ON agent_embeddings(embedding)
            """)
    except Exception:
        # sqlite-vec not available, will use fallback search
        pass

    conn.commit()
    conn.close()

    # Run v2 migration for Memory Commons
    run_v2_migration(db_path)


def run_v2_migration(db_path: Optional[Path] = None) -> None:
    """Run Memory Commons v2 migration."""
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v2_memory_commons.sql"

    if not migration_file.exists():
        return  # Migration file not found, skip

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Read and execute migration SQL using executescript (handles multi-statement SQL)
    with open(migration_file, 'r') as f:
        migration_sql = f.read()

    cursor.executescript(migration_sql)

    # Add columns to shared_memories if they don't exist
    # Check which columns exist
    cursor.execute("PRAGMA table_info(shared_memories)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    columns_to_add = {
        'privacy_tier': "ALTER TABLE shared_memories ADD COLUMN privacy_tier TEXT DEFAULT 'team' CHECK(privacy_tier IN ('private', 'team', 'public'))",
        'hop_count': "ALTER TABLE shared_memories ADD COLUMN hop_count INTEGER DEFAULT 1",
        'original_author': "ALTER TABLE shared_memories ADD COLUMN original_author TEXT",
        'confidence': "ALTER TABLE shared_memories ADD COLUMN confidence REAL DEFAULT 1.0",
        'age_days': "ALTER TABLE shared_memories ADD COLUMN age_days INTEGER DEFAULT 0",
        'derived_from': "ALTER TABLE shared_memories ADD COLUMN derived_from TEXT",
        'effective_confidence': "ALTER TABLE shared_memories ADD COLUMN effective_confidence REAL"
    }

    for col_name, alter_sql in columns_to_add.items():
        if col_name not in existing_columns:
            cursor.execute(alter_sql)

    # Create index on privacy_tier if it doesn't exist
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_privacy_tier ON shared_memories(privacy_tier)")

    # Add columns to federation_peers if they don't exist
    cursor.execute("PRAGMA table_info(federation_peers)")
    existing_peer_columns = {row[1] for row in cursor.fetchall()}

    peer_columns_to_add = {
        'memory_sync_enabled': "ALTER TABLE federation_peers ADD COLUMN memory_sync_enabled INTEGER DEFAULT 1",
        'last_memory_sync': "ALTER TABLE federation_peers ADD COLUMN last_memory_sync TEXT",
        'min_trust_for_sync': "ALTER TABLE federation_peers ADD COLUMN min_trust_for_sync REAL DEFAULT 30.0"
    }

    for col_name, alter_sql in peer_columns_to_add.items():
        if col_name not in existing_peer_columns:
            cursor.execute(alter_sql)

    # Ensure room-memory-commons exists (required for Memory Commons feature)
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT OR IGNORE INTO rooms (id, name, slug, description, created_by, is_public, created_at)
        VALUES ('room-memory-commons', '#Memory Commons',
                'memory-commons', 'Goal-driven memory sharing and semantic routing',
                'circus-system', 1, ?)
    """, (now,))

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

        # Create memory-commons special room (for goal-routed memories)
        cursor.execute("""
            INSERT OR IGNORE INTO rooms (
                id, name, slug, description, created_by, is_public, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "room-memory-commons",
            "#Memory Commons",
            "memory-commons",
            "Goal-driven memory sharing and semantic routing",
            system_agent_id,
            1,
            now
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
