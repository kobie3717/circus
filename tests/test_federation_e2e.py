"""End-to-end two-node federation test (Sub-step 3.6 ship gate)."""

import base64
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from circus.database import init_database, run_v2_migration, run_v3_migration
from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.federation_admission import admit_bundle
from circus.services.federation_pull import pull_bundles
from circus.services.federation_wiring import admit_and_merge
from circus.services.signing import generate_keypair
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class Node:
    """Minimal node fixture for two-node e2e test."""

    def __init__(self, node_id: str):
        self.node_id = node_id
        self.db_path = None
        self.conn = None
        self.private_key_bytes = None
        self.public_key_bytes = None

    def setup(self):
        """Initialize database and keypair."""
        # Create temp DB
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            self.db_path = Path(f.name)

        init_database(self.db_path)
        run_v2_migration(self.db_path)
        run_v3_migration(self.db_path)

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        # Generate keypair
        self.private_key_bytes, self.public_key_bytes = generate_keypair()

        # Set instance_id to node_id (for pull_bundles to use correct peer_id)
        cursor = self.conn.cursor()
        cursor.execute("UPDATE instance_config SET value = ? WHERE key = 'instance_id'", (self.node_id,))
        self.conn.commit()

    def teardown(self):
        """Clean up database."""
        if self.conn:
            self.conn.close()
        if self.db_path:
            self.db_path.unlink(missing_ok=True)

    def register_peer(self, peer_node: "Node"):
        """Register another node as a trusted peer."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO federation_peers (
                id, name, url, public_key, trust_score, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            peer_node.node_id,
            f"Peer {peer_node.node_id}",
            f"https://{peer_node.node_id}.example.com",
            peer_node.public_key_bytes,
            60.0,  # Trusted tier
            1,
            datetime.utcnow().isoformat(),
        ))
        self.conn.commit()

    def publish_local_memory(self, memory_id: str, content: str, category: str = "tech",
                             domain: str = "general", agent_id: str = None) -> str:
        """Publish memory locally (simulating POST /publish)."""
        if agent_id is None:
            agent_id = f"agent-{self.node_id}"

        now = datetime.utcnow()
        cursor = self.conn.cursor()

        provenance = {
            "hop_count": 1,
            "original_author": agent_id,
            "original_timestamp": now.isoformat(),
            "confidence": 0.9,
        }

        cursor.execute("""
            INSERT INTO shared_memories (
                id, room_id, from_agent_id, content, category, domain,
                tags, provenance, privacy_tier, hop_count, original_author,
                confidence, age_days, effective_confidence, shared_at, trust_verified
            ) VALUES (?, 'room-memory-commons', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, 0)
        """, (
            memory_id,
            agent_id,
            content,
            category,
            domain,
            json.dumps([]),
            json.dumps(provenance),
            "public",
            agent_id,
            0.9,
            0.9,  # effective_confidence
            now.isoformat(),
        ))
        self.conn.commit()

        return memory_id

    def sign_bundle(self, bundle: dict) -> dict:
        """Sign bundle with node's private key."""
        canonical_bytes = canonicalize_for_signing(bundle)
        private_key = Ed25519PrivateKey.from_private_bytes(self.private_key_bytes)
        signature_bytes = private_key.sign(canonical_bytes)
        bundle["signature"] = base64.b64encode(signature_bytes).decode('ascii')
        return bundle


@pytest.fixture
def two_nodes():
    """Two independent Circus instances (in-memory DBs)."""
    node_a = Node("node-a")
    node_b = Node("node-b")

    node_a.setup()
    node_b.setup()

    # Cross-register peers
    node_a.register_peer(node_b)
    node_b.register_peer(node_a)

    # Override settings.database_path for each node's operations
    from circus.config import settings
    original_db_path = settings.database_path

    yield node_a, node_b

    settings.database_path = original_db_path
    node_a.teardown()
    node_b.teardown()


def test_two_node_federation_e2e(two_nodes):
    """End-to-end: Node A publishes → Node B pulls → admits → merges.

    Verifies full PULL → PUSH → merge flow with two independent databases.
    Service-layer only (no HTTP).
    """
    from circus.config import settings

    node_a, node_b = two_nodes
    now = datetime.utcnow()

    # Step 1: Node A publishes memory locally
    memory_id = "mem-e2e-001"
    content = "FlashVault VPN uses WireGuard protocol"
    category = "tech"
    domain = "vpn"
    agent_id = "agent-node-a"

    node_a.publish_local_memory(memory_id, content, category, domain, agent_id)

    # Verify Node A has memory in shared_memories
    cursor_a = node_a.conn.cursor()
    cursor_a.execute("SELECT COUNT(*) as count FROM shared_memories WHERE id = ?", (memory_id,))
    assert cursor_a.fetchone()["count"] == 1

    # Step 2: Node B pulls from Node A (simulating federation PULL)
    # Override settings to point to Node A's DB for pull operation
    settings.database_path = node_a.db_path
    bundles, next_cursor, has_more = pull_bundles(
        node_a.conn,
        puller_peer_id=node_b.node_id,
        since_cursor=None,
        limit=10
    )
    settings.database_path = node_b.db_path

    assert len(bundles) == 1
    bundle = bundles[0]

    # Verify bundle structure
    assert bundle["peer_id"] == node_a.node_id
    assert len(bundle["memories"]) == 1
    assert bundle["memories"][0]["id"] == memory_id

    # Generate passport for Node A (required for admission)
    bundle["passport"] = {
        "identity": {
            "name": node_a.node_id,
            "role": "agent",
        },
        "score": {"total": 7.5},
        "generated_at": now.isoformat(),
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.2, "graph_connections": 15},
    }

    # Sign bundle with Node A's private key
    bundle = node_a.sign_bundle(bundle)

    # Step 3: Node B admits bundle (verification pipeline)
    settings.database_path = node_b.db_path
    admission_result = admit_bundle(bundle, now=now)

    assert admission_result.admitted is True
    assert admission_result.decision == "admitted"
    assert admission_result.memories_new == 1
    assert admission_result.memories_skipped == 0

    # Step 4: Node B calls admit_and_merge (wiring layer)
    conflicts = admit_and_merge(bundle, peer_id=node_a.node_id, now=now)

    # Assertions on Node B
    cursor_b = node_b.conn.cursor()

    # Verify shared_memories row exists
    cursor_b.execute("SELECT * FROM shared_memories WHERE id = ?", (memory_id,))
    row_b = dict(cursor_b.fetchone())

    assert row_b["content"] == content
    assert row_b["category"] == category
    assert row_b["domain"] == domain

    # Verify hop_count incremented (Node A published at hop=1, Node B should have hop=2)
    assert row_b["hop_count"] == 2

    # Verify from_agent_id preserved (original author, not peer_id)
    assert row_b["from_agent_id"] == agent_id
    assert row_b["original_author"] == agent_id

    # Verify provenance chain
    prov_b = json.loads(row_b["provenance"])
    assert prov_b["hop_count"] == 2
    assert prov_b["original_author"] == agent_id
    assert prov_b["received_from"] == node_a.node_id
    assert prov_b["received_at"] == now.isoformat()

    # Verify no conflicts (no contradicting local memories)
    assert len(conflicts) == 0

    cursor_b.execute("SELECT COUNT(*) as count FROM belief_conflicts WHERE memory_id_a = ? OR memory_id_b = ?",
                     (memory_id, memory_id))
    assert cursor_b.fetchone()["count"] == 0


def test_two_node_federation_with_conflict(two_nodes):
    """Extended e2e: Node B has local memory → Node A pushes contradicting memory → conflict detected."""
    from circus.config import settings

    node_a, node_b = two_nodes
    now = datetime.utcnow()

    # Step 1: Node B publishes local memory
    local_memory_id = "mem-local-b"
    node_b.publish_local_memory(local_memory_id, "FlashVault uses WireGuard", category="tech", domain="vpn")

    # Step 2: Node A publishes contradicting memory
    remote_memory_id = "mem-remote-a"
    node_a.publish_local_memory(remote_memory_id, "FlashVault does not use WireGuard", category="tech", domain="vpn")

    # Step 3: Node B pulls from Node A
    settings.database_path = node_a.db_path
    bundles, next_cursor, has_more = pull_bundles(
        node_a.conn,
        puller_peer_id=node_b.node_id,
        since_cursor=None,
        limit=10
    )
    settings.database_path = node_b.db_path

    bundle = bundles[0]

    # Add passport and sign
    bundle["passport"] = {
        "identity": {"name": node_a.node_id, "role": "agent"},
        "score": {"total": 7.5},
        "generated_at": now.isoformat(),
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 3.2, "graph_connections": 15},
    }
    bundle = node_a.sign_bundle(bundle)

    # Step 4: Node B admits and merges
    settings.database_path = node_b.db_path
    admission_result = admit_bundle(bundle, now=now)
    assert admission_result.admitted is True

    conflicts = admit_and_merge(bundle, peer_id=node_a.node_id, now=now)

    # Assert: Conflict detected
    assert len(conflicts) == 1

    # Assert: Conflict row exists
    cursor_b = node_b.conn.cursor()
    cursor_b.execute("SELECT COUNT(*) as count FROM belief_conflicts")
    assert cursor_b.fetchone()["count"] == 1

    # Assert: Conflict type is contradiction
    cursor_b.execute("SELECT conflict_type, memory_id_a, memory_id_b FROM belief_conflicts")
    conflict_row = cursor_b.fetchone()
    assert conflict_row["conflict_type"] == "contradiction"

    # Assert: Both memory IDs present
    conflict_ids = {conflict_row["memory_id_a"], conflict_row["memory_id_b"]}
    assert conflict_ids == {local_memory_id, remote_memory_id}
