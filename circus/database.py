"""Database schema and operations for The Circus."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from circus.config import settings


def seed_owner_key_from_env(conn: sqlite3.Connection) -> None:
    """Auto-seed owner public key on startup if owner_keys table is empty for this owner."""
    import os
    import base64

    owner_id = settings.owner_id
    key_path = settings.owner_private_key_path

    if not owner_id or not key_path:
        return
    if not os.path.exists(key_path):
        return

    # Check if already seeded
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT COUNT(*) FROM owner_keys WHERE owner_id=?", (owner_id,)
    ).fetchone()
    if row[0] > 0:
        return

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        priv_bytes = base64.b64decode(open(key_path).read().strip())
        pk = Ed25519PrivateKey.from_private_bytes(priv_bytes)
        pub = pk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        pub_b64 = base64.b64encode(pub).decode()
        cursor.execute(
            "INSERT OR IGNORE INTO owner_keys (owner_id, public_key, created_at) VALUES (?,?,?)",
            (owner_id, pub_b64, datetime.utcnow().isoformat())
        )
        conn.commit()
        print(f"[DB] Auto-seeded owner_key for {owner_id}")
    except Exception as e:
        print(f"[DB] Could not auto-seed owner key: {e}")


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
    # Run v3 migration for Federation
    run_v3_migration(db_path)
    # Run v4 migration for Federation dedup
    run_v4_migration(db_path)
    # Run v5 migration for Instance identity
    run_v5_migration(db_path)
    # Run v6 migration for Federation rate limits
    run_v6_migration(db_path)
    # Run v7 migration for Active preferences
    run_v7_migration(db_path)
    # Run v8 migration for Owner keys
    run_v8_migration(db_path)
    # Run v9 migration for Conflict count
    run_v9_migration(db_path)
    # Run v10 migration for Key lifecycle
    run_v10_migration(db_path)
    # Run v11 migration for Federation outbox
    run_v11_migration(db_path)
    # Run v12 migration for Quarantine and governance audit
    run_v12_migration(db_path)

    # Auto-seed owner key if configured
    conn = sqlite3.connect(str(db_path))
    seed_owner_key_from_env(conn)
    conn.close()


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
        'effective_confidence': "ALTER TABLE shared_memories ADD COLUMN effective_confidence REAL",
        'status': "ALTER TABLE shared_memories ADD COLUMN status TEXT DEFAULT 'active'"
    }

    for col_name, alter_sql in columns_to_add.items():
        if col_name not in existing_columns:
            cursor.execute(alter_sql)

    # Create index on privacy_tier if it doesn't exist
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_privacy_tier ON shared_memories(privacy_tier)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shared_memories_status ON shared_memories(status)")

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


def run_v3_migration(db_path: Optional[Path] = None) -> None:
    """Run Memory Commons v3 migration (Federation schema hardening)."""
    import json
    import logging
    import uuid

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v3_federation.sql"

    if not migration_file.exists():
        return  # Migration file not found, skip

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        # Check if migration already applied (domain column exists)
        cursor.execute("PRAGMA table_info(shared_memories)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        domain_already_exists = 'domain' in existing_columns

        # Add domain column if it doesn't exist
        if not domain_already_exists:
            cursor.execute("ALTER TABLE shared_memories ADD COLUMN domain TEXT")

        # Execute federation tables SQL first (idempotent with IF NOT EXISTS)
        with open(migration_file, 'r') as f:
            migration_sql = f.read()
        cursor.executescript(migration_sql)

        # Backfill domain from category (idempotent - only NULL domains)
        # Only backfill if category is valid (non-empty, printable ASCII)
        cursor.execute("""
            SELECT id, category FROM shared_memories
            WHERE domain IS NULL AND category IS NOT NULL AND category != ''
        """)
        rows_to_backfill = cursor.fetchall()

        backfilled_count = 0
        skipped_count = 0

        for row_id, category in rows_to_backfill:
            # Validate category is domain-name-looking (non-empty, printable ASCII)
            if category and category.isprintable() and len(category) > 0:
                cursor.execute("UPDATE shared_memories SET domain = ? WHERE id = ?", (category, row_id))
                backfilled_count += 1
            else:
                # Skip malformed category, log warning
                skipped_count += 1
                logger.warning("v3 migration: skipped backfill for memory %s with malformed category: %r",
                             row_id, category)

        # Create domain index
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_commons_domain ON shared_memories(domain)")

        # Log backfill to federation_audit (only if we actually backfilled something)
        if backfilled_count > 0 or skipped_count > 0:
            audit_id = f"audit-{uuid.uuid4().hex[:16]}"
            now = datetime.utcnow().isoformat()
            metadata = json.dumps({
                "rows_backfilled": backfilled_count,
                "rows_skipped": skipped_count
            })
            cursor.execute("""
                INSERT INTO federation_audit (id, action, actor_passport, target_id, reason, metadata, created_at)
                VALUES (?, 'backfill_run', NULL, NULL, 'v3 migration domain backfill', ?, ?)
            """, (audit_id, metadata, now))

            logger.info("v3 federation migration: backfilled %d rows (skipped %d malformed)",
                       backfilled_count, skipped_count)

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("v3 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v4_migration(db_path: Optional[Path] = None) -> None:
    """Run Memory Commons v4 migration (federation_bundles_seen for transport dedup)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v4_bundles_seen.sql"

    if not migration_file.exists():
        return  # Migration file not found, skip

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        # Execute migration SQL (idempotent via IF NOT EXISTS)
        with open(migration_file, 'r') as f:
            migration_sql = f.read()
        cursor.executescript(migration_sql)

        conn.commit()
        logger.info("v4 federation migration: federation_bundles_seen table created")
    except Exception as e:
        conn.rollback()
        logger.error("v4 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v5_migration(db_path: Optional[Path] = None) -> None:
    """Run v5 migration: instance_config table + keypair bootstrap.

    Idempotent: runs the SQL (IF NOT EXISTS) then calls ensure_instance_keypair
    which is itself idempotent (loads existing keys, only generates if missing).
    """
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v5_instance_config.sql"

    if not migration_file.exists():
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.cursor()
        with open(migration_file, 'r') as f:
            cursor.executescript(f.read())

        # Seed identity (idempotent — no-op if already present)
        from circus.services.instance_identity import ensure_instance_keypair
        ensure_instance_keypair(conn)

        conn.commit()
        logger.info("v5 federation migration: instance_config table created and identity seeded")
    except Exception as e:
        conn.rollback()
        logger.error("v5 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v6_migration(db_path: Optional[Path] = None) -> None:
    """Run v6 migration: federation_rate_limits table."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v6_federation_rate_limits.sql"

    if not migration_file.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        with open(migration_file) as f:
            conn.executescript(f.read())
        conn.commit()
        logger.info("v6 migration: rate limits table created")
    except Exception as e:
        conn.rollback()
        logger.error("v6 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v7_migration(db_path: Optional[Path] = None) -> None:
    """Run v7 migration: active_preferences table."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v7_active_preferences.sql"

    if not migration_file.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        with open(migration_file) as f:
            conn.executescript(f.read())
        conn.commit()
        logger.info("v7 migration: active_preferences table created")
    except Exception as e:
        conn.rollback()
        logger.error("v7 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v8_migration(db_path: Optional[Path] = None) -> None:
    """Run v8 migration: owner_keys table + clear active_preferences."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v8_owner_keys.sql"

    if not migration_file.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        # Count rows in active_preferences before clearing
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM active_preferences")
        rows_before = cursor.fetchone()[0]

        # Run migration
        with open(migration_file) as f:
            conn.executescript(f.read())
        conn.commit()

        logger.info(
            "v8 migration: owner_keys table created, cleared %d rows from active_preferences",
            rows_before
        )
    except Exception as e:
        conn.rollback()
        logger.error("v8 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v9_migration(db_path: Optional[Path] = None) -> None:
    """Run v9 migration: Add conflict_count column to active_preferences (W7)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if conflict_count column already exists
        cursor.execute("PRAGMA table_info(active_preferences)")
        columns = {row[1] for row in cursor.fetchall()}

        if 'conflict_count' not in columns:
            # Add column (default 0)
            cursor.execute("ALTER TABLE active_preferences ADD COLUMN conflict_count INTEGER DEFAULT 0")
            conn.commit()
            logger.info("v9 migration: added conflict_count column to active_preferences")
        else:
            logger.debug("v9 migration: conflict_count column already exists, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v9 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v10_migration(db_path: Optional[Path] = None) -> None:
    """Run v10 migration: Key lifecycle (W9) — rotation, revocation, TOFU, discovery."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v10_key_lifecycle.sql"

    if not migration_file.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if migration already applied (is_active column exists)
        cursor.execute("PRAGMA table_info(owner_keys)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'is_active' not in existing_columns:
            # Run migration SQL
            with open(migration_file, 'r') as f:
                migration_sql = f.read()
            cursor.executescript(migration_sql)
            conn.commit()
            logger.info("v10 migration: key lifecycle schema applied (owner_keys + key_events)")
        else:
            logger.debug("v10 migration: key lifecycle columns already exist, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v10 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v11_migration(db_path: Optional[Path] = None) -> None:
    """Run v11 migration: Federation outbox + peer health (W10)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v11_federation_outbox.sql"

    if not migration_file.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if migration already applied (federation_outbox table exists)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='federation_outbox'")
        outbox_exists = cursor.fetchone() is not None

        if not outbox_exists:
            # Run migration SQL (creates table + indexes)
            with open(migration_file, 'r') as f:
                migration_sql = f.read()
            cursor.executescript(migration_sql)

            # Add health tracking columns to federation_peers
            cursor.execute("PRAGMA table_info(federation_peers)")
            existing_columns = {row[1] for row in cursor.fetchall()}

            if 'last_seen_at' not in existing_columns:
                cursor.execute("ALTER TABLE federation_peers ADD COLUMN last_seen_at TEXT")
            if 'last_failed_at' not in existing_columns:
                cursor.execute("ALTER TABLE federation_peers ADD COLUMN last_failed_at TEXT")
            if 'consecutive_failures' not in existing_columns:
                cursor.execute("ALTER TABLE federation_peers ADD COLUMN consecutive_failures INTEGER DEFAULT 0")
            if 'is_healthy' not in existing_columns:
                cursor.execute("ALTER TABLE federation_peers ADD COLUMN is_healthy INTEGER DEFAULT 1")
            if 'registered_at' not in existing_columns:
                cursor.execute("ALTER TABLE federation_peers ADD COLUMN registered_at TEXT")

            # Fix public_key constraint: SQLite doesn't support DROP NOT NULL,
            # so we'll set a dummy key for existing rows without one (should be none)
            # New peers added via outbox don't need public_key (they're just URLs)

            conn.commit()
            logger.info("v11 migration: federation_outbox table created, peer health columns added")
        else:
            logger.debug("v11 migration: federation_outbox already exists, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v11 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v12_migration(db_path: Optional[Path] = None) -> None:
    """Run v12 migration: Quarantine system + governance audit (W11)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v12_quarantine.sql"

    if not migration_file.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if migration already applied (quarantine table exists)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quarantine'")
        quarantine_exists = cursor.fetchone() is not None

        if not quarantine_exists:
            # Run migration SQL
            with open(migration_file, 'r') as f:
                migration_sql = f.read()
            cursor.executescript(migration_sql)

            conn.commit()
            logger.info("v12 migration: quarantine + governance_audit tables created")
        else:
            logger.debug("v12 migration: quarantine table already exists, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v12 migration failed: %s", e)
        raise
    finally:
        conn.close()


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Get database connection context manager.

    IMPORTANT — COMMIT DISCIPLINE:
    This context manager does NOT auto-commit on exit. Writes require an
    explicit `conn.commit()` before the context block ends, or they will
    be silently dropped when the connection closes.

    On exception inside the block, SQLite rolls back the open transaction
    automatically when the connection closes — no explicit rollback needed,
    but nothing will have persisted either.

    Pattern for writes:

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT ...", (...))
            conn.commit()   # REQUIRED — do not omit

    Pattern for reads: no commit needed.

    Violating this is the single most common "why didn't it persist?"
    failure mode in this codebase. If you add a new module that writes
    through get_db(), also add at least one test that reads the row back
    to prove the commit landed.
    """
    conn = sqlite3.connect(str(settings.database_path))
    conn.row_factory = sqlite3.Row
    # Enforce referential integrity — 20+ FK constraints are defined in schema
    # but SQLite ships with foreign_keys=OFF by default. Without this, deleting
    # an agent leaves orphaned passports/trust_events/vouches/etc.
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL mode allows concurrent reads during writes — required for federation
    # PUSH throughput and SSE polling while memory commons writes land.
    # Set once on first connection; subsequent calls are no-ops but cheap.
    conn.execute("PRAGMA journal_mode=WAL")
    # Busy timeout prevents "database is locked" errors under concurrent writes
    conn.execute("PRAGMA busy_timeout=5000")
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
