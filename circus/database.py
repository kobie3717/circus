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

    # FTS5 virtual table for full-text search on shared memories
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_shared_memories
        USING fts5(content, content='shared_memories', content_rowid='rowid')
    """)

    # Populate FTS from existing rows (idempotent - fts5 deduplicates on rebuild)
    cursor.execute("""
        INSERT OR IGNORE INTO fts_shared_memories(rowid, content)
        SELECT rowid, content FROM shared_memories
    """)

    # Triggers to keep FTS in sync
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS shared_memories_ai
        AFTER INSERT ON shared_memories BEGIN
            INSERT INTO fts_shared_memories(rowid, content) VALUES (new.rowid, new.content);
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS shared_memories_ad
        AFTER DELETE ON shared_memories BEGIN
            INSERT INTO fts_shared_memories(fts_shared_memories, rowid, content)
            VALUES ('delete', old.rowid, old.content);
        END
    """)

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS shared_memories_au
        AFTER UPDATE ON shared_memories BEGIN
            INSERT INTO fts_shared_memories(fts_shared_memories, rowid, content)
            VALUES ('delete', old.rowid, old.content);
            INSERT INTO fts_shared_memories(rowid, content) VALUES (new.rowid, new.content);
        END
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

    # Token revocations table (JWT revocation)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_revocations (
            jti TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            revoked_at TEXT NOT NULL,
            reason TEXT DEFAULT 'manual'
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_token_revocations_agent ON token_revocations(agent_id)")

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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trust_events_agent_created ON trust_events(agent_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created_desc ON audit_log(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shared_memories_from_agent ON shared_memories(from_agent_id, shared_at DESC)")

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

    # Token pool tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_pool (
            id INTEGER PRIMARY KEY DEFAULT 1,
            daily_budget INTEGER DEFAULT 5000000,
            daily_used INTEGER DEFAULT 0,
            current_date TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_pool_bots (
            bot_id TEXT PRIMARY KEY,
            daily_used INTEGER DEFAULT 0,
            conversation_used INTEGER DEFAULT 0,
            current_session TEXT DEFAULT '',
            tier TEXT DEFAULT 'green',
            updated_at TEXT DEFAULT ''
        )
    """)

    # Insert default token pool row
    cursor.execute("INSERT OR IGNORE INTO token_pool (id) VALUES (1)")

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
    # Run v13 migration for task output schemas
    run_v13_migration(db_path)
    # Run v14 migration for bandit routing
    run_v14_migration(db_path)
    # Run v15 migration for graph orchestration
    run_v15_migration(db_path)
    # Run v16 migration for graph audit log
    run_v16_migration(db_path)
    # Run v17 migration for troupe isolation
    run_v17_migration(db_path)
    # Run v18 migration for TTL + domain shift signals
    run_v18_migration(db_path)
    # Run v19 migration for Merkle chain validation
    run_v19_migration(db_path)
    # Run v20 migration for provenance chain-of-custody
    run_v20_migration(db_path)
    # Run v21 migration for niche tier classification
    run_v21_migration(db_path)
    # Run v22 migration for risk-weighted knowledge frontiers
    run_v22_migration(db_path)
    # Run v23 migration for backpressure synthesis
    run_v23_migration(db_path)
    # Run v24 migration for trust-decay escrow with cross-backing
    run_v24_migration(db_path)
    # Run v25 migration for stake-driven task compression
    run_v25_migration(db_path)
    # Run v26 migration for trajectory-weighted mutual aid pools
    run_v26_migration(db_path)
    # Run v27 migration for agent_vouches table
    run_v27_migration(db_path)
    # Run v28 migration for vouch chain columns
    run_v28_migration(db_path)
    # Run v29 migration for capability_proofs table
    run_v29_migration(db_path)
    # Run v30 migration for reversibility_class + required_capabilities on tasks
    run_v30_migration(db_path)
    # Run v31 migration for memory_claims table (Phase 3: atomic claim store)
    run_v31_migration(db_path)
    # Run v32 migration for memory_contradictions table (Phase 3: contradiction tracking)
    run_v32_migration(db_path)
    # Run v33 migration for task_escrow table (Phase 4: attack-resistant escrow)
    run_v33_migration(db_path)
    # Run v34 migration for fraud_reports table (Phase 4: fraud tracking)
    run_v34_migration(db_path)
    # Run v35 migration for task_events + doom loop detection (Phase 5: trust scaffolding)
    run_v35_migration(db_path)
    # Run v36 migration for webhooks + observer_mode + platform economics (Phase 6: The Standard)
    run_v36_migration(db_path)
    # Run v37 migration for memory claim embeddings (semantic search)
    run_v37_migration(db_path)
    # Run v38 migration for docvault-system agent registration
    run_v38_migration(db_path)
    # Run v39 migration for dispute_votes table (Elder multi-sig)
    run_v39_migration(db_path)

    # Run v40 migration for auto_discovered column (peer autodiscovery)
    run_v40_migration(db_path)

    # Run v41 migration for P2P task delegation chain tracking
    run_v41_migration(db_path)

    # Run v42 migration for agent_experiences table (narrative memory)
    run_v42_migration(db_path)

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


def run_v13_migration(db_path: Optional[Path] = None) -> None:
    """Run v13 migration: Add output_schema column for agentdo-style task schema validation."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if output_schema column already exists
        cursor.execute("PRAGMA table_info(tasks)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'output_schema' not in existing_columns:
            # Add column (nullable, JSON string)
            cursor.execute("ALTER TABLE tasks ADD COLUMN output_schema TEXT")
            conn.commit()
            logger.info("v13 migration: added output_schema column to tasks table")
        else:
            logger.debug("v13 migration: output_schema column already exists, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v13 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v14_migration(db_path: Optional[Path] = None) -> None:
    """Run v14 migration: LinUCB bandit routing tables."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v14_bandit_routing.sql"

    if not migration_file.exists():
        raise FileNotFoundError(f"Migration file not found: {migration_file}")

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if routing_arms table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='routing_arms'")
        if cursor.fetchone():
            logger.debug("v14 migration: routing tables already exist, skipping")
            return

        # Read and execute migration SQL
        with open(migration_file, "r") as f:
            sql_script = f.read()

        # Execute all statements
        cursor.executescript(sql_script)
        conn.commit()
        logger.info("v14 migration: created routing tables (routing_arms, routing_decisions, routing_feature_stats)")

    except Exception as e:
        conn.rollback()
        logger.error("v14 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v15_migration(db_path: Optional[Path] = None) -> None:
    """Run v15 migration: Graph orchestration tables."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if graph_definitions table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='graph_definitions'")
        if cursor.fetchone():
            logger.debug("v15 migration: graph tables already exist, skipping")
            return

        # Execute migration SQL
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS graph_definitions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                definition TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (name, version)
            );

            CREATE TABLE IF NOT EXISTS graph_executions (
                id TEXT PRIMARY KEY,
                graph_id TEXT NOT NULL,
                graph_version INTEGER NOT NULL,
                started_by TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'running',
                input_data TEXT NOT NULL,
                output_data TEXT,
                current_node TEXT,
                execution_path TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                CHECK (state IN ('running', 'paused', 'completed', 'failed', 'canceled'))
            );

            CREATE TABLE IF NOT EXISTS node_executions (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                node_type TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'pending',
                input_data TEXT NOT NULL,
                output_data TEXT,
                task_id TEXT,
                worker_result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                CHECK (state IN ('pending', 'running', 'completed', 'failed', 'skipped')),
                CHECK (node_type IN ('task', 'worker', 'parallel', 'human', 'merge', 'conditional', 'passthrough'))
            );

            CREATE TABLE IF NOT EXISTS graph_checkpoints (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                node_execution_id TEXT NOT NULL,
                checkpoint_index INTEGER NOT NULL,
                state_snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (execution_id, checkpoint_index)
            );

            CREATE TABLE IF NOT EXISTS graph_human_approvals (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                node_execution_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                options TEXT,
                response TEXT,
                responded_by TEXT,
                created_at TEXT NOT NULL,
                responded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS graph_parallel_branches (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                parent_node_execution_id TEXT NOT NULL,
                branch_index INTEGER NOT NULL,
                branch_node_id TEXT NOT NULL,
                node_execution_id TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (state IN ('pending', 'running', 'completed', 'failed'))
            );

            -- Indexes
            CREATE INDEX IF NOT EXISTS idx_graph_executions_state ON graph_executions(state);
            CREATE INDEX IF NOT EXISTS idx_graph_executions_started_by ON graph_executions(started_by);
            CREATE INDEX IF NOT EXISTS idx_node_executions_execution_id ON node_executions(execution_id);
            CREATE INDEX IF NOT EXISTS idx_graph_checkpoints_execution_id ON graph_checkpoints(execution_id);
            CREATE INDEX IF NOT EXISTS idx_graph_human_approvals_execution_id ON graph_human_approvals(execution_id);
            CREATE INDEX IF NOT EXISTS idx_graph_parallel_branches_execution_id ON graph_parallel_branches(execution_id);
        """)

        conn.commit()
        logger.info("v15 migration: created graph orchestration tables")

    except Exception as e:
        conn.rollback()
        logger.error("v15 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v16_migration(db_path: Optional[Path] = None) -> None:
    """Run v16 migration: Graph audit log table."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if graph_audit_log table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='graph_audit_log'")
        if cursor.fetchone():
            logger.debug("v16 migration: graph_audit_log table already exists, skipping")
            return

        # Execute migration SQL
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS graph_audit_log (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                node_id TEXT,
                event_type TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL,
                CHECK (event_type IN (
                    'graph_started', 'graph_completed', 'graph_failed', 'graph_canceled',
                    'node_started', 'node_completed', 'node_failed', 'node_retried',
                    'node_timed_out', 'human_paused', 'human_resumed', 'checkpoint_created',
                    'parallel_started', 'parallel_completed'
                ))
            );
            CREATE INDEX IF NOT EXISTS idx_graph_audit_execution ON graph_audit_log(execution_id, created_at);
        """)

        conn.commit()
        logger.info("v16 migration: created graph_audit_log table")

    except Exception as e:
        conn.rollback()
        logger.error("v16 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v17_migration(db_path: Optional[Path] = None) -> None:
    """Run v17 migration: Troupe-scoped memory isolation."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v17_troupe_isolation.sql"

    if not migration_file.exists():
        raise FileNotFoundError(f"Migration file not found: {migration_file}")

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if troupe_id column already exists in shared_memories
        cursor.execute("PRAGMA table_info(shared_memories)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'troupe_id' in columns:
            logger.debug("v17 migration: troupe_id column already exists, skipping")
            return

        # Read and execute migration SQL
        with open(migration_file, "r") as f:
            sql_script = f.read()

        # Execute all statements
        cursor.executescript(sql_script)
        conn.commit()
        logger.info("v17 migration: added troupe isolation (troupe_id column, troupe_members table)")

    except Exception as e:
        conn.rollback()
        logger.error("v17 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v18_migration(db_path: Optional[Path] = None) -> None:
    """Run v18 migration: TTL + domain shift signals (Gap 1)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    migration_file = Path(__file__).parent / "database_migrations" / "v18_ttl_domain_shift.sql"

    if not migration_file.exists():
        raise FileNotFoundError(f"Migration file not found: {migration_file}")

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if expires_at column already exists in shared_memories
        cursor.execute("PRAGMA table_info(shared_memories)")
        columns = {row[1] for row in cursor.fetchall()}

        columns_added = []

        # Add expires_at column if it doesn't exist
        if 'expires_at' not in columns:
            cursor.execute("ALTER TABLE shared_memories ADD COLUMN expires_at TEXT")
            columns_added.append('expires_at')

        # Add contradiction_count column if it doesn't exist
        if 'contradiction_count' not in columns:
            cursor.execute("ALTER TABLE shared_memories ADD COLUMN contradiction_count INTEGER DEFAULT 0")
            columns_added.append('contradiction_count')

        # Execute migration SQL (creates domain_shift_signals table)
        with open(migration_file, "r") as f:
            sql_script = f.read()
        cursor.executescript(sql_script)

        conn.commit()
        if columns_added:
            logger.info(f"v18 migration: added columns {columns_added}, created domain_shift_signals table")
        else:
            logger.debug("v18 migration: columns already exist, created domain_shift_signals table if needed")

    except Exception as e:
        conn.rollback()
        logger.error("v18 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v19_migration(db_path: Optional[Path] = None) -> None:
    """Run v19 migration: Merkle chain validation for multi-bot task chains (Gap 4)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if task_chain_nodes table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_chain_nodes'")
        if cursor.fetchone():
            logger.debug("v19 migration: task_chain_nodes table already exists, skipping")
            return

        # Create task_chain_nodes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_chain_nodes (
                id TEXT PRIMARY KEY,
                root_task_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                parent_task_id TEXT,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'sub-agent',
                input_hash TEXT,
                output_hash TEXT,
                verdict TEXT,
                output_summary TEXT,
                contradiction_with TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)

        # Create indexes for task_chain_nodes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tcn_root ON task_chain_nodes(root_task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tcn_parent ON task_chain_nodes(parent_task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tcn_agent ON task_chain_nodes(agent_id)")

        # Check if synthesis_score and validation_score columns exist in agent_competence
        cursor.execute("PRAGMA table_info(agent_competence)")
        competence_columns = {row[1] for row in cursor.fetchall()}

        # Add synthesis_score column if it doesn't exist
        if 'synthesis_score' not in competence_columns:
            cursor.execute("ALTER TABLE agent_competence ADD COLUMN synthesis_score REAL DEFAULT 0.5")

        # Add validation_score column if it doesn't exist
        if 'validation_score' not in competence_columns:
            cursor.execute("ALTER TABLE agent_competence ADD COLUMN validation_score REAL DEFAULT 0.5")

        conn.commit()
        logger.info("v19 migration: created task_chain_nodes table + synthesis_score/validation_score columns")

    except Exception as e:
        conn.rollback()
        logger.error("v19 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v20_migration(db_path: Optional[Path] = None) -> None:
    """Run v20 migration: Provenance chain-of-custody on shared memories (Round 2 Gap 1)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if custody_chain column already exists
        cursor.execute("PRAGMA table_info(shared_memories)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        columns_added = []

        # Add custody_chain column if it doesn't exist
        if 'custody_chain' not in existing_columns:
            cursor.execute("ALTER TABLE shared_memories ADD COLUMN custody_chain TEXT")
            columns_added.append('custody_chain')

        # Add stake_escrowed column if it doesn't exist
        if 'stake_escrowed' not in existing_columns:
            cursor.execute("ALTER TABLE shared_memories ADD COLUMN stake_escrowed REAL DEFAULT 0.0")
            columns_added.append('stake_escrowed')

        # Create index on from_agent_id
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sm_from_agent ON shared_memories(from_agent_id)")

        conn.commit()
        if columns_added:
            logger.info(f"v20 migration: added columns {columns_added}, created idx_sm_from_agent index")
        else:
            logger.debug("v20 migration: columns already exist, created index if needed")

    except Exception as e:
        conn.rollback()
        logger.error("v20 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v21_migration(db_path: Optional[Path] = None) -> None:
    """Run v21 migration: Niche tier classification for task types (Round 2 Gap 3)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if task_niche_registry table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_niche_registry'")
        if cursor.fetchone():
            logger.debug("v21 migration: task_niche_registry table already exists, skipping")
            return

        # Create task_niche_registry table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_niche_registry (
                task_type TEXT PRIMARY KEY,
                tier TEXT NOT NULL DEFAULT 'SANDBOX',
                min_trust REAL NOT NULL DEFAULT 0.0,
                description TEXT,
                requires_human_approval INTEGER DEFAULT 0,
                completion_count INTEGER DEFAULT 0,
                created_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (tier IN ('SANDBOX', 'PRODUCTION', 'SAFETY_CRITICAL'))
            )
        """)

        # Create index on tier
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tnr_tier ON task_niche_registry(tier)")

        # Seed default task types
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT OR IGNORE INTO task_niche_registry (task_type, tier, min_trust, description, created_by, created_at, updated_at)
            VALUES
                ('build', 'PRODUCTION', 40.0, 'Code build tasks', 'circus-system', ?, ?),
                ('code-review', 'PRODUCTION', 40.0, 'Code review tasks', 'circus-system', ?, ?),
                ('research', 'SANDBOX', 0.0, 'Research and lookup tasks', 'circus-system', ?, ?),
                ('notify', 'SANDBOX', 0.0, 'Notification tasks', 'circus-system', ?, ?),
                ('test-task', 'SANDBOX', 0.0, 'Test tasks', 'circus-system', ?, ?)
        """, (now, now, now, now, now, now, now, now, now, now))

        # Add niche_tier column to tasks table if it doesn't exist
        cursor.execute("PRAGMA table_info(tasks)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'niche_tier' not in existing_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN niche_tier TEXT DEFAULT 'SANDBOX'")
            logger.info("v21 migration: added niche_tier column to tasks table")

        conn.commit()
        logger.info("v21 migration: created task_niche_registry table with seed data")

    except Exception as e:
        conn.rollback()
        logger.error("v21 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v22_migration(db_path: Optional[Path] = None) -> None:
    """Run v22 migration: Risk-weighted knowledge frontiers (Round 2 Gap 4)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if niche_difficulty_scores table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='niche_difficulty_scores'")
        if cursor.fetchone():
            logger.debug("v22 migration: niche_difficulty_scores table already exists, skipping")
            return

        # Create niche_difficulty_scores table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS niche_difficulty_scores (
                domain_tag TEXT PRIMARY KEY,
                difficulty_score REAL NOT NULL DEFAULT 0.1,
                base_escrow_rate REAL NOT NULL DEFAULT 0.05,
                lock_days INTEGER NOT NULL DEFAULT 90,
                creator_lock_days INTEGER NOT NULL DEFAULT 30,
                observation_count INTEGER DEFAULT 0,
                contradiction_rate REAL DEFAULT 0.0,
                last_calibrated TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Create index on difficulty_score
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_nds_score ON niche_difficulty_scores(difficulty_score DESC)")

        # Seed default difficulty scores for known domains
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT OR IGNORE INTO niche_difficulty_scores
                (domain_tag, difficulty_score, base_escrow_rate, lock_days, creator_lock_days, created_at, updated_at)
            VALUES
                ('medical', 0.9, 0.20, 90, 30, ?, ?),
                ('legal', 0.85, 0.18, 90, 30, ?, ?),
                ('financial', 0.80, 0.16, 90, 30, ?, ?),
                ('security', 0.75, 0.15, 90, 30, ?, ?),
                ('engineering', 0.55, 0.10, 90, 30, ?, ?),
                ('research', 0.40, 0.07, 90, 30, ?, ?),
                ('general', 0.20, 0.04, 90, 30, ?, ?),
                ('trivia', 0.05, 0.01, 90, 30, ?, ?)
        """, (now, now, now, now, now, now, now, now, now, now, now, now, now, now, now, now))

        # Create escrow_locks table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS escrow_locks (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'publisher',
                escrow_amount REAL NOT NULL DEFAULT 0.0,
                locked_at TEXT NOT NULL,
                unlocks_at TEXT NOT NULL,
                released_at TEXT,
                release_reason TEXT,
                FOREIGN KEY (memory_id) REFERENCES shared_memories(id)
            )
        """)

        # Create indexes for escrow_locks
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_el_agent ON escrow_locks(agent_id, released_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_el_memory ON escrow_locks(memory_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_el_unlocks ON escrow_locks(unlocks_at)")

        conn.commit()
        logger.info("v22 migration: created niche_difficulty_scores and escrow_locks tables with seed data")

    except Exception as e:
        conn.rollback()
        logger.error("v22 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v23_migration(db_path: Optional[Path] = None) -> None:
    """Run v23 migration: Backpressure-triggered memory synthesis (Round 3 Gap 2)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if priority_tier column already exists in tasks
        cursor.execute("PRAGMA table_info(tasks)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        columns_added = []

        # Add priority_tier column if it doesn't exist
        if 'priority_tier' not in existing_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN priority_tier TEXT DEFAULT 'deferrable'")
            columns_added.append('priority_tier')

        # Create index on priority_tier
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tier ON tasks(priority_tier, state)")

        # Check if task_synthesis_log table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_synthesis_log'")
        if not cursor.fetchone():
            # Create task_synthesis_log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS task_synthesis_log (
                    id TEXT PRIMARY KEY,
                    triggered_at TEXT NOT NULL,
                    queue_depth_before INTEGER NOT NULL,
                    tasks_consumed INTEGER NOT NULL DEFAULT 0,
                    tasks_created INTEGER NOT NULL DEFAULT 0,
                    compression_ratio REAL DEFAULT 1.0,
                    synthesis_groups TEXT,
                    completed_at TEXT
                )
            """)
            logger.info("v23 migration: created task_synthesis_log table")

        # Seed: mark known realtime task types
        cursor.execute("""
            UPDATE tasks SET priority_tier = 'realtime'
            WHERE task_type IN ('notify', 'auction', 'bid', 'alert') AND priority_tier = 'deferrable'
        """)

        conn.commit()
        if columns_added:
            logger.info(f"v23 migration: added columns {columns_added}, created synthesis tracking")
        else:
            logger.debug("v23 migration: priority_tier column already exists, ensured synthesis table")

    except Exception as e:
        conn.rollback()
        logger.error("v23 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v24_migration(db_path: Optional[Path] = None) -> None:
    """Run v24 migration: Trust-decay escrow with cross-backing (Round 3 Gap 1)."""
    import logging

    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if task_backing_stakes table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_backing_stakes'")
        if cursor.fetchone():
            logger.debug("v24 migration: task_backing_stakes table already exists, skipping")
            return

        # Create task_backing_stakes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_backing_stakes (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                backer_agent_id TEXT NOT NULL,
                performer_agent_id TEXT NOT NULL,
                stake_amount REAL NOT NULL DEFAULT 0.0,
                backer_trust_at_stake REAL NOT NULL,
                performer_trust_at_stake REAL NOT NULL,
                trust_delta REAL NOT NULL,
                max_yield_rate REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'staked',
                yield_earned REAL DEFAULT 0.0,
                staked_at TEXT NOT NULL,
                resolved_at TEXT,
                CHECK (state IN ('staked', 'yielded', 'forfeited', 'returned')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tbs_task ON task_backing_stakes(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tbs_backer ON task_backing_stakes(backer_agent_id, state)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tbs_performer ON task_backing_stakes(performer_agent_id, state)")

        # Create backing concentration view (anti-cartel)
        cursor.execute("""
            CREATE VIEW IF NOT EXISTS backing_concentration AS
                SELECT performer_agent_id, backer_agent_id,
                       COUNT(*) as stake_count,
                       SUM(stake_amount) as total_staked,
                       SUM(stake_amount) / NULLIF((SELECT SUM(s2.stake_amount) FROM task_backing_stakes s2 WHERE s2.performer_agent_id = s.performer_agent_id AND s2.state = 'staked'), 0) as concentration_ratio
                FROM task_backing_stakes s
                WHERE state = 'staked'
                GROUP BY performer_agent_id, backer_agent_id
        """)

        conn.commit()
        logger.info("v24 migration: created task_backing_stakes table and backing_concentration view")

    except Exception as e:
        conn.rollback()
        logger.error("v24 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v25_migration(db_path: Optional[Path] = None) -> None:
    """Run v25 migration: Stake-driven task compression (Round 3 Gap 4)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(task_backing_stakes)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if 'compression_id' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE task_backing_stakes ADD COLUMN compression_id TEXT")
            except Exception as e:
                logger.warning(f"v25: compression_id column: {e}")
        if 'compression_discount' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE task_backing_stakes ADD COLUMN compression_discount REAL DEFAULT 1.0")
            except Exception as e:
                logger.warning(f"v25: compression_discount column: {e}")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tbs_compression ON task_backing_stakes(compression_id)")
        conn.commit()
        logger.info("v25 migration complete: stake compression columns added")
    except Exception as e:
        conn.rollback()
        logger.error("v25 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v26_migration(db_path: Optional[Path] = None) -> None:
    """Run v26 migration: Trajectory-weighted mutual aid pools (Round 3 Gap 3)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if mutual_aid_pools table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mutual_aid_pools'")
        if cursor.fetchone():
            logger.debug("v26 migration: mutual_aid_pools table already exists, skipping")
            return

        # Create mutual_aid_pools table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mutual_aid_pools (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                total_contributed REAL DEFAULT 0.0,
                total_disbursed REAL DEFAULT 0.0,
                balance REAL DEFAULT 0.0,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Create pool_contributions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pool_contributions (
                id TEXT PRIMARY KEY,
                pool_id TEXT NOT NULL,
                contributor_agent_id TEXT NOT NULL,
                amount REAL NOT NULL,
                contributed_at TEXT NOT NULL,
                FOREIGN KEY (pool_id) REFERENCES mutual_aid_pools(id)
            )
        """)

        # Create agent_trust_snapshots table for trajectory tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_trust_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                trust_score REAL NOT NULL,
                snapped_at TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ats_agent ON agent_trust_snapshots(agent_id, snapped_at)")

        # Create pool_payouts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pool_payouts (
                id TEXT PRIMARY KEY,
                pool_id TEXT NOT NULL,
                recipient_agent_id TEXT NOT NULL,
                amount REAL NOT NULL,
                trust_slope REAL NOT NULL,
                niche_diversity REAL NOT NULL,
                paid_at TEXT NOT NULL,
                FOREIGN KEY (pool_id) REFERENCES mutual_aid_pools(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pp_recipient ON pool_payouts(recipient_agent_id, paid_at)")

        # Seed default pool
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT OR IGNORE INTO mutual_aid_pools (id, name, description, total_contributed, total_disbursed, balance, created_by, created_at, updated_at)
            VALUES ('default-pool', 'Circus Bootstrap Pool', 'Auto-funded pool for high-velocity new agents', 0.0, 0.0, 0.0, 'circus-system', ?, ?)
        """, (now, now))

        conn.commit()
        logger.info("v26 migration: created mutual_aid_pools, pool_contributions, agent_trust_snapshots, pool_payouts tables + seed data")

    except Exception as e:
        conn.rollback()
        logger.error("v26 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v27_migration(db_path: Optional[Path] = None) -> None:
    """Run v27 migration: agent_vouches table for ai-mesh trust scaffolding."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if agent_vouches table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_vouches'")
        if cursor.fetchone():
            logger.debug("v27 migration: agent_vouches table already exists, skipping")
            return

        # Create agent_vouches table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_vouches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id TEXT NOT NULL,
                vouchee_id TEXT NOT NULL,
                vouch_date TEXT NOT NULL,
                liability_pct REAL DEFAULT 100.0,
                status TEXT DEFAULT 'active',
                note TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(voucher_id, vouchee_id),
                FOREIGN KEY (voucher_id) REFERENCES agents(id) ON DELETE CASCADE,
                FOREIGN KEY (vouchee_id) REFERENCES agents(id) ON DELETE CASCADE
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_vouches_voucher ON agent_vouches(voucher_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_vouches_vouchee ON agent_vouches(vouchee_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_vouches_status ON agent_vouches(status)")

        conn.commit()
        logger.info("v27 migration: created agent_vouches table")

    except Exception as e:
        conn.rollback()
        logger.error("v27 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v28_migration(db_path: Optional[Path] = None) -> None:
    """Run v28 migration: Add vouch chain columns to agents table."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if columns already exist
        cursor.execute("PRAGMA table_info(agents)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        columns_added = []

        # Add sponsor_id column if it doesn't exist
        if 'sponsor_id' not in existing_columns:
            cursor.execute("ALTER TABLE agents ADD COLUMN sponsor_id TEXT REFERENCES agents(id)")
            columns_added.append('sponsor_id')

        # Add vouch_depth column if it doesn't exist
        if 'vouch_depth' not in existing_columns:
            cursor.execute("ALTER TABLE agents ADD COLUMN vouch_depth INTEGER DEFAULT 0")
            columns_added.append('vouch_depth')

        # Add ancestry_chain column if it doesn't exist
        if 'ancestry_chain' not in existing_columns:
            cursor.execute("ALTER TABLE agents ADD COLUMN ancestry_chain TEXT")
            columns_added.append('ancestry_chain')

        # Create index on sponsor_id
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agents_sponsor ON agents(sponsor_id)")

        conn.commit()
        if columns_added:
            logger.info(f"v28 migration: added columns {columns_added} to agents table")
        else:
            logger.debug("v28 migration: vouch chain columns already exist, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v28 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v29_migration(db_path: Optional[Path] = None) -> None:
    """Run v29 migration: capability_proofs table for ai-mesh trust scaffolding Phase 2."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if capability_proofs table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='capability_proofs'")
        if cursor.fetchone():
            logger.debug("v29 migration: capability_proofs table already exists, skipping")
            return

        # Create capability_proofs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS capability_proofs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                capability_tag TEXT NOT NULL,
                proof_type TEXT DEFAULT 'eval',
                eval_task_id TEXT,
                score REAL NOT NULL,
                verified_at TEXT NOT NULL,
                expires_at TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
            )
        """)

        # Create index
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_capability_proofs_agent ON capability_proofs(agent_id, status)")

        conn.commit()
        logger.info("v29 migration: created capability_proofs table")

    except Exception as e:
        conn.rollback()
        logger.error("v29 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v30_migration(db_path: Optional[Path] = None) -> None:
    """Run v30 migration: reversibility_class + required_capabilities on tasks table."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if columns already exist
        cursor.execute("PRAGMA table_info(tasks)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        columns_added = []

        # Add reversibility_class column if it doesn't exist
        if 'reversibility_class' not in existing_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN reversibility_class TEXT DEFAULT 'REVERSIBLE'")
            columns_added.append('reversibility_class')

        # Add required_capabilities column if it doesn't exist
        if 'required_capabilities' not in existing_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN required_capabilities TEXT")
            columns_added.append('required_capabilities')

        conn.commit()
        if columns_added:
            logger.info(f"v30 migration: added columns {columns_added} to tasks table")
        else:
            logger.debug("v30 migration: reversibility columns already exist, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v30 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v31_migration(db_path: Optional[Path] = None) -> None:
    """Run v31 migration: memory_claims table for atomic claim store (Phase 3)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if memory_claims table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_claims'")
        if cursor.fetchone():
            logger.debug("v31 migration: memory_claims table already exists, skipping")
            return

        # Create memory_claims table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_claims (
                id TEXT PRIMARY KEY,
                namespace_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                claim_text TEXT NOT NULL,
                subject TEXT,
                claim_type TEXT DEFAULT 'semantic',
                importance REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.6,
                status TEXT DEFAULT 'candidate',
                source TEXT,
                episode_id TEXT,
                created_at TEXT NOT NULL,
                last_accessed TEXT,
                access_count INTEGER DEFAULT 0,
                superseded_by TEXT,
                decay_rate REAL,
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
                CHECK (claim_type IN ('episodic', 'semantic', 'procedural', 'identity')),
                CHECK (status IN ('candidate', 'active', 'superseded', 'archived'))
            )
        """)

        # Create FTS5 virtual table for claim search
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_memory_claims
            USING fts5(claim_text, subject, content='memory_claims', content_rowid='rowid')
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_claims_namespace ON memory_claims(namespace_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_claims_agent ON memory_claims(agent_id, claim_type, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_claims_subject ON memory_claims(subject, status)")

        # Create FTS sync triggers
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_claims_ai
            AFTER INSERT ON memory_claims BEGIN
                INSERT INTO fts_memory_claims(rowid, claim_text, subject) VALUES (new.rowid, new.claim_text, new.subject);
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_claims_ad
            AFTER DELETE ON memory_claims BEGIN
                INSERT INTO fts_memory_claims(fts_memory_claims, rowid, claim_text, subject)
                VALUES ('delete', old.rowid, old.claim_text, old.subject);
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS memory_claims_au
            AFTER UPDATE ON memory_claims BEGIN
                INSERT INTO fts_memory_claims(fts_memory_claims, rowid, claim_text, subject)
                VALUES ('delete', old.rowid, old.claim_text, old.subject);
                INSERT INTO fts_memory_claims(rowid, claim_text, subject) VALUES (new.rowid, new.claim_text, new.subject);
            END
        """)

        conn.commit()
        logger.info("v31 migration: created memory_claims table with FTS5 indexing")

    except Exception as e:
        conn.rollback()
        logger.error("v31 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v32_migration(db_path: Optional[Path] = None) -> None:
    """Run v32 migration: memory_contradictions table (Phase 3)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if memory_contradictions table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_contradictions'")
        if cursor.fetchone():
            logger.debug("v32 migration: memory_contradictions table already exists, skipping")
            return

        # Create memory_contradictions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_contradictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id TEXT NOT NULL,
                contradicting_claim_id TEXT NOT NULL,
                contradicting_agent_id TEXT NOT NULL,
                evidence TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY (claim_id) REFERENCES memory_claims(id),
                FOREIGN KEY (contradicting_agent_id) REFERENCES agents(id),
                CHECK (status IN ('pending', 'confirmed', 'dismissed'))
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mc_claim ON memory_contradictions(claim_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mc_agent ON memory_contradictions(contradicting_agent_id)")

        conn.commit()
        logger.info("v32 migration: created memory_contradictions table")

    except Exception as e:
        conn.rollback()
        logger.error("v32 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v33_migration(db_path: Optional[Path] = None) -> None:
    """Run v33 migration: task_escrow table (Phase 4: attack-resistant escrow)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if task_escrow table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_escrow'")
        if cursor.fetchone():
            logger.debug("v33 migration: task_escrow table already exists, skipping")
            return

        # Create task_escrow table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_escrow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                amount_staked REAL NOT NULL,
                sponsor_1_id TEXT,
                sponsor_1_stake REAL DEFAULT 0.0,
                sponsor_2_id TEXT,
                sponsor_2_stake REAL DEFAULT 0.0,
                payout_amount REAL NOT NULL,
                status TEXT DEFAULT 'locked',
                locked_at TEXT NOT NULL,
                release_date TEXT NOT NULL,
                released_at TEXT,
                dispute_id INTEGER,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id),
                FOREIGN KEY (sponsor_1_id) REFERENCES agents(id),
                FOREIGN KEY (sponsor_2_id) REFERENCES agents(id),
                CHECK (status IN ('locked', 'released', 'forfeited', 'disputed'))
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_escrow_task ON task_escrow(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_escrow_agent ON task_escrow(agent_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_escrow_release ON task_escrow(release_date, status)")

        conn.commit()
        logger.info("v33 migration: created task_escrow table")

    except Exception as e:
        conn.rollback()
        logger.error("v33 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v34_migration(db_path: Optional[Path] = None) -> None:
    """Run v34 migration: fraud_reports table (Phase 4: fraud tracking)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if fraud_reports table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fraud_reports'")
        if cursor.fetchone():
            logger.debug("v34 migration: fraud_reports table already exists, skipping")
            return

        # Create fraud_reports table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fraud_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                escrow_id INTEGER NOT NULL,
                reporter_id TEXT NOT NULL,
                evidence TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                penalty_applied INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT,
                resolution_note TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                FOREIGN KEY (escrow_id) REFERENCES task_escrow(id),
                FOREIGN KEY (reporter_id) REFERENCES agents(id),
                CHECK (status IN ('open', 'confirmed', 'dismissed'))
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fraud_reports_task ON fraud_reports(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fraud_reports_status ON fraud_reports(status)")

        conn.commit()
        logger.info("v34 migration: created fraud_reports table")

    except Exception as e:
        conn.rollback()
        logger.error("v34 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v35_migration(db_path: Optional[Path] = None) -> None:
    """Run v35 migration: task_events + task_banned_strategies + checkpoint columns (Phase 5: doom-loop detection)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if task_events table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_events'")
        if cursor.fetchone():
            logger.debug("v35 migration: task_events table already exists, skipping")
            return

        # Create task_events table (append-only audit log)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT,
                is_ok INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            )
        """)

        # Create indexes for task_events
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_events_agent ON task_events(agent_id, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_events_type ON task_events(event_type, created_at)")

        # Create task_banned_strategies table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_banned_strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                strategy_signature TEXT NOT NULL,
                ban_reason TEXT,
                banned_at TEXT NOT NULL,
                UNIQUE(task_id, agent_id, strategy_signature)
            )
        """)

        # Add checkpoint columns to tasks table (using try/except for idempotency)
        cursor.execute("PRAGMA table_info(tasks)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'checkpoint_state' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE tasks ADD COLUMN checkpoint_state TEXT")
            except Exception as e:
                logger.warning(f"v35: checkpoint_state column add failed (may already exist): {e}")

        if 'checkpoint_step' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE tasks ADD COLUMN checkpoint_step INTEGER DEFAULT 0")
            except Exception as e:
                logger.warning(f"v35: checkpoint_step column add failed (may already exist): {e}")

        if 'doom_loop_count' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE tasks ADD COLUMN doom_loop_count INTEGER DEFAULT 0")
            except Exception as e:
                logger.warning(f"v35: doom_loop_count column add failed (may already exist): {e}")

        conn.commit()
        logger.info("v35 migration: created task_events, task_banned_strategies tables + checkpoint columns")

    except Exception as e:
        conn.rollback()
        logger.error("v35 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v36_migration(db_path: Optional[Path] = None) -> None:
    """Run v36 migration: webhooks + observer_mode + platform economics (Phase 6: The Standard)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if webhook_subscriptions table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_subscriptions'")
        if cursor.fetchone():
            logger.debug("v36 migration: webhook tables already exist, skipping")
            return

        # Create webhook_subscriptions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS webhook_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                url TEXT NOT NULL,
                events TEXT NOT NULL,
                secret TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                last_triggered_at TEXT,
                failure_count INTEGER DEFAULT 0,
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_webhooks_agent ON webhook_subscriptions(agent_id, is_active)")

        # Create webhook_deliveries table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                response_status INTEGER,
                attempt_count INTEGER DEFAULT 0,
                last_error TEXT,
                attempted_at TEXT,
                delivered_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id)
            )
        """)

        # Add observer_mode column to agents table
        cursor.execute("PRAGMA table_info(agents)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'observer_mode' not in existing_columns:
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN observer_mode INTEGER DEFAULT 0")
            except Exception as e:
                logger.warning(f"v36: observer_mode column add failed (may already exist): {e}")

        # Create platform_fees table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS platform_fees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                payout_amount REAL NOT NULL,
                fee_amount REAL NOT NULL,
                fee_pct REAL DEFAULT 0.02,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                collected_at TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_platform_fees_status ON platform_fees(status, created_at)")

        # Create flywheel_snapshots table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS flywheel_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL,
                total_agents INTEGER DEFAULT 0,
                active_agents INTEGER DEFAULT 0,
                tasks_completed INTEGER DEFAULT 0,
                tasks_failed INTEGER DEFAULT 0,
                doom_loops_detected INTEGER DEFAULT 0,
                memory_claims_published INTEGER DEFAULT 0,
                escrow_volume REAL DEFAULT 0.0,
                fees_collected REAL DEFAULT 0.0,
                top_failure_signatures TEXT,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()
        logger.info("v36 migration: created webhook_subscriptions, webhook_deliveries, platform_fees, flywheel_snapshots + observer_mode column")

    except Exception as e:
        conn.rollback()
        logger.error("v36 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v37_migration(db_path: Optional[Path] = None) -> None:
    """Run v37 migration: Add embedding column to memory_claims for semantic search."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if embedding column already exists
        cursor.execute("PRAGMA table_info(memory_claims)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'embedding' not in existing_columns:
            # Add embedding column (stores JSON array as TEXT)
            cursor.execute("ALTER TABLE memory_claims ADD COLUMN embedding TEXT")
            # Create index for future optimization
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_claims_embedding ON memory_claims(embedding) WHERE embedding IS NOT NULL")
            conn.commit()
            logger.info("v37 migration: added embedding column to memory_claims table")
        else:
            logger.debug("v37 migration: embedding column already exists, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v37 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v38_migration(db_path: Optional[Path] = None) -> None:
    """Run v38 migration: Register docvault-system agent for DocVault webhook notifications."""
    import logging
    import json
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if docvault-system agent already exists
        cursor.execute("SELECT id FROM agents WHERE id = 'docvault-system'")
        if cursor.fetchone():
            logger.debug("v38 migration: docvault-system agent already exists, skipping")
            return

        # Insert docvault-system agent
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO agents (
                id, name, role, capabilities, home_instance, passport_hash,
                token_hash, trust_score, trust_tier, registered_at, last_seen, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "docvault-system",
            "DocVault",
            "system",
            json.dumps(["doc-review"]),
            "http://127.0.0.1:6300",
            "docvault-system",
            "docvault-system",
            80.0,
            "Trusted",
            now,
            now,
            1
        ))

        conn.commit()
        logger.info("v38 migration: registered docvault-system agent")

    except Exception as e:
        conn.rollback()
        logger.error("v38 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v39_migration(db_path: Optional[Path] = None) -> None:
    """Run v39 migration: dispute_votes table (Elder multi-sig dispute resolution)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if dispute_votes table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dispute_votes'")
        if cursor.fetchone():
            logger.debug("v39 migration: dispute_votes table already exists, skipping")
            return

        # Create dispute_votes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dispute_votes (
                id TEXT PRIMARY KEY,
                dispute_id INTEGER NOT NULL,
                voter_id TEXT NOT NULL,
                vote TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(dispute_id, voter_id),
                FOREIGN KEY (dispute_id) REFERENCES fraud_reports(id),
                FOREIGN KEY (voter_id) REFERENCES agents(id),
                CHECK (vote IN ('confirmed', 'dismissed'))
            )
        """)

        # Create index
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dispute_votes_dispute ON dispute_votes(dispute_id)")

        conn.commit()
        logger.info("v39 migration: created dispute_votes table")

    except Exception as e:
        conn.rollback()
        logger.error("v39 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v40_migration(db_path: Optional[Path] = None) -> None:
    """Run v40 migration: auto_discovered column for federation_peers (peer autodiscovery)."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if auto_discovered column already exists
        cursor.execute("PRAGMA table_info(federation_peers)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'auto_discovered' not in existing_columns:
            cursor.execute("ALTER TABLE federation_peers ADD COLUMN auto_discovered INTEGER DEFAULT 0")
            conn.commit()
            logger.info("v40 migration: added auto_discovered column to federation_peers")
        else:
            logger.debug("v40 migration: auto_discovered column already exists, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v40 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v41_migration(db_path: Optional[Path] = None) -> None:
    """Run v41 migration: P2P task delegation chain tracking."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if delegation columns already exist
        cursor.execute("PRAGMA table_info(tasks)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        columns_added = []

        # Add parent_task_id column if it doesn't exist
        if 'parent_task_id' not in existing_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT")
            columns_added.append('parent_task_id')

        # Add delegated_by column if it doesn't exist
        if 'delegated_by' not in existing_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN delegated_by TEXT")
            columns_added.append('delegated_by')

        # Add delegation_depth column if it doesn't exist
        if 'delegation_depth' not in existing_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN delegation_depth INTEGER DEFAULT 0")
            columns_added.append('delegation_depth')

        # Add delegation_note column if it doesn't exist
        if 'delegation_note' not in existing_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN delegation_note TEXT")
            columns_added.append('delegation_note')

        # Create index on parent_task_id
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)")

        conn.commit()
        if columns_added:
            logger.info(f"v41 migration: added delegation columns {columns_added} to tasks table")
        else:
            logger.debug("v41 migration: delegation columns already exist, skipping")

    except Exception as e:
        conn.rollback()
        logger.error("v41 migration failed: %s", e)
        raise
    finally:
        conn.close()


def run_v42_migration(db_path: Optional[Path] = None) -> None:
    """Run v42 migration: agent_experiences table for narrative experience sharing."""
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Check if agent_experiences table already exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_experiences'")
        if cursor.fetchone():
            logger.debug("v42 migration: agent_experiences table already exists, skipping")
            return

        # Create agent_experiences table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_experiences (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                environment TEXT NOT NULL,
                task_type TEXT NOT NULL,
                what_worked TEXT,
                what_failed TEXT,
                context_snapshot TEXT,
                outcome REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.5,
                observations INTEGER NOT NULL DEFAULT 1,
                confirmed_by TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_exp_env_task ON agent_experiences(environment, task_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_exp_agent ON agent_experiences(agent_id, outcome DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_exp_confidence ON agent_experiences(confidence DESC)")

        conn.commit()
        logger.info("v42 migration: created agent_experiences table")

    except Exception as e:
        conn.rollback()
        logger.error("v42 migration failed: %s", e)
        raise
    finally:
        conn.close()


def decay_vouch_liability(db_path: Optional[Path] = None) -> None:
    """Decay vouch liability from 100% -> 10% over 90 days (linear).

    Called by periodic background job to update all active vouches.
    """
    import logging
    logger = logging.getLogger(__name__)
    db_path = db_path or settings.database_path

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()

        # Fetch all active vouches
        cursor.execute("""
            SELECT id, vouch_date FROM agent_vouches WHERE status = 'active'
        """)
        vouches = cursor.fetchall()

        now = datetime.utcnow()
        updated_count = 0

        for vouch in vouches:
            vouch_id = vouch[0]
            vouch_date = datetime.fromisoformat(vouch[1])
            days_since_vouch = (now - vouch_date).days

            # Linear decay: 100 -> 10 over 90 days
            # Formula: liability_pct = max(10.0, 100.0 - (days_since_vouch / 90.0) * 90.0)
            # Simplifies to: max(10.0, 100.0 - days_since_vouch)
            new_liability = max(10.0, 100.0 - days_since_vouch)

            # Update liability_pct
            cursor.execute("""
                UPDATE agent_vouches SET liability_pct = ? WHERE id = ?
            """, (new_liability, vouch_id))
            updated_count += 1

        conn.commit()
        if updated_count > 0:
            logger.info(f"Vouch liability decay: updated {updated_count} active vouches")

    except Exception as e:
        conn.rollback()
        logger.error(f"Vouch liability decay failed: {e}")
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

    # CRITICAL: Enable foreign key enforcement FIRST (before any queries)
    # Without this, cascade deletes don't work — orphaned passport/embedding rows
    # accumulate when agents are deleted, causing DB bloat and stale data bugs.
    # SQLite ships with FK enforcement OFF by default (legacy compat).
    conn.execute("PRAGMA foreign_keys = ON")

    # WAL mode allows concurrent reads during writes — required for federation
    # PUSH throughput and SSE polling while memory commons writes land.
    # Set once on first connection; subsequent calls are no-ops but cheap.
    conn.execute("PRAGMA journal_mode=WAL")
    # Busy timeout prevents "database is locked" errors under concurrent writes
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        # Warn if transaction was left uncommitted (common mistake in this codebase)
        if conn.in_transaction:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning("Database connection closed with uncommitted transaction - changes will be rolled back")
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
