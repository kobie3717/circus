"""Tests for federation wiring layer — admit_and_merge function (Sub-step 3.6)."""

import base64
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from circus.database import init_database, run_v2_migration, run_v3_migration
from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.federation_wiring import admit_and_merge
from circus.services.signing import generate_keypair
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


# Test fixtures

@pytest.fixture
def test_db():
    """Create temporary database for testing with federation tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    init_database(db_path)
    run_v2_migration(db_path)
    run_v3_migration(db_path)

    # Override settings.database_path for get_db() calls
    from circus.config import settings
    original_db_path = settings.database_path
    settings.database_path = db_path

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn

    conn.close()
    settings.database_path = original_db_path
    db_path.unlink(missing_ok=True)


def test_federated_vs_local_parity(test_db):
    """Federated memory should match local memory except hop_count and received_from."""
    from circus.routes.memory_commons import publish_memory  # Import to compare local publish

    now = datetime.utcnow()
    peer_id = "peer-remote-001"
    agent_id = "agent-original-001"

    # Test content
    content = "FlashVault uses WireGuard for VPN tunneling"
    category = "tech"
    domain = "vpn"
    tags = ["vpn", "wireguard"]
    confidence = 0.9

    # 1. Federated memory via admit_and_merge
    federated_bundle = {
        "bundle_id": "bundle-fed-001",
        "peer_id": peer_id,
        "memories": [
            {
                "id": "mem-fed-001",
                "content": content,
                "category": category,
                "domain": domain,
                "tags": tags,
                "privacy_tier": "public",
                "provenance": {
                    "hop_count": 1,
                    "original_author": agent_id,
                    "original_timestamp": (now - timedelta(hours=2)).isoformat(),
                    "confidence": confidence,
                },
            }
        ],
    }

    conflicts_fed = admit_and_merge(federated_bundle, peer_id=peer_id, now=now)

    # 2. Local memory via direct INSERT (simulating local publish)
    cursor = test_db.cursor()
    local_memory_id = "mem-local-001"
    cursor.execute("""
        INSERT INTO shared_memories (
            id, room_id, from_agent_id, content, category, domain,
            tags, provenance, privacy_tier, hop_count, original_author,
            confidence, age_days, effective_confidence, shared_at, trust_verified
        ) VALUES (?, 'room-memory-commons', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, 0)
    """, (
        local_memory_id,
        agent_id,
        content,
        category,
        domain,
        json.dumps(tags),
        json.dumps({
            "hop_count": 1,
            "original_author": agent_id,
            "original_timestamp": (now - timedelta(hours=2)).isoformat(),
            "confidence": confidence,
        }),
        "public",
        agent_id,
        confidence,
        confidence,  # effective_confidence (no decay at hop=1, age=0)
        now.isoformat(),
    ))
    test_db.commit()

    # 3. Compare rows
    cursor.execute("SELECT * FROM shared_memories WHERE id = ?", ("mem-fed-001",))
    fed_row = dict(cursor.fetchone())

    cursor.execute("SELECT * FROM shared_memories WHERE id = ?", (local_memory_id,))
    local_row = dict(cursor.fetchone())

    # Assert: content, category, domain, tags, confidence are identical
    assert fed_row["content"] == local_row["content"]
    assert fed_row["category"] == local_row["category"]
    assert fed_row["domain"] == local_row["domain"]
    assert fed_row["tags"] == local_row["tags"]
    assert fed_row["confidence"] == local_row["confidence"]

    # Assert: from_agent_id / original_author point to original publisher (not peer)
    assert fed_row["from_agent_id"] == agent_id
    assert fed_row["original_author"] == agent_id
    assert local_row["from_agent_id"] == agent_id
    assert local_row["original_author"] == agent_id

    # Assert: hop_count differs (local=1, federated=2)
    assert local_row["hop_count"] == 1
    assert fed_row["hop_count"] == 2  # Incremented from incoming 1

    # Assert: provenance.received_from present in federated, absent in local
    fed_prov = json.loads(fed_row["provenance"])
    local_prov = json.loads(local_row["provenance"])
    assert fed_prov["received_from"] == peer_id
    assert "received_from" not in local_prov

    # Assert: No conflicts detected (identical content)
    assert len(conflicts_fed) == 0


def test_idempotent_merge_on_replay(test_db):
    """Replaying same bundle should skip (no duplicate shared_memories rows)."""
    now = datetime.utcnow()
    peer_id = "peer-remote-001"

    bundle = {
        "bundle_id": "bundle-replay-001",
        "peer_id": peer_id,
        "memories": [
            {
                "id": "mem-replay-001",
                "content": "Test idempotency",
                "category": "tech",
                "privacy_tier": "public",
                "provenance": {
                    "hop_count": 2,
                    "original_author": "agent-source-001",
                    "original_timestamp": (now - timedelta(hours=1)).isoformat(),
                    "confidence": 0.85,
                },
            }
        ],
    }

    # First push
    conflicts1 = admit_and_merge(bundle, peer_id=peer_id, now=now)

    # Second push (same bundle, same memory ID)
    conflicts2 = admit_and_merge(bundle, peer_id=peer_id, now=now)

    # Assert: Only ONE shared_memories row
    cursor = test_db.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM shared_memories WHERE id = ?", ("mem-replay-001",))
    assert cursor.fetchone()["count"] == 1

    # Assert: hop_count unchanged (still 3 from first insert)
    cursor.execute("SELECT hop_count FROM shared_memories WHERE id = ?", ("mem-replay-001",))
    assert cursor.fetchone()["hop_count"] == 3  # 2 + 1

    # Assert: No duplicate belief_conflicts rows
    cursor.execute("SELECT COUNT(*) as count FROM belief_conflicts WHERE memory_id_a = ? OR memory_id_b = ?",
                   ("mem-replay-001", "mem-replay-001"))
    assert cursor.fetchone()["count"] == 0


def test_hop_count_increment(test_db):
    """hop_count should increment by exactly 1 and match provenance.hop_count."""
    now = datetime.utcnow()
    peer_id = "peer-remote-001"

    bundle = {
        "bundle_id": "bundle-hop-001",
        "peer_id": peer_id,
        "memories": [
            {
                "id": "mem-hop-zero",
                "content": "Memory at hop 0",
                "category": "tech",
                "privacy_tier": "public",
                "provenance": {
                    "hop_count": 0,
                    "original_author": "agent-source-001",
                    "original_timestamp": (now - timedelta(hours=1)).isoformat(),
                    "confidence": 0.9,
                },
            },
            {
                "id": "mem-hop-three",
                "content": "Memory at hop 3",
                "category": "tech",
                "privacy_tier": "public",
                "provenance": {
                    "hop_count": 3,
                    "original_author": "agent-source-002",
                    "original_timestamp": (now - timedelta(hours=2)).isoformat(),
                    "confidence": 0.8,
                },
            },
        ],
    }

    admit_and_merge(bundle, peer_id=peer_id, now=now)

    cursor = test_db.cursor()

    # Memory 1: hop_count should be 1 (0 + 1)
    cursor.execute("SELECT hop_count, provenance FROM shared_memories WHERE id = ?", ("mem-hop-zero",))
    row1 = cursor.fetchone()
    assert row1["hop_count"] == 1
    prov1 = json.loads(row1["provenance"])
    assert prov1["hop_count"] == 1

    # Memory 2: hop_count should be 4 (3 + 1)
    cursor.execute("SELECT hop_count, provenance FROM shared_memories WHERE id = ?", ("mem-hop-three",))
    row2 = cursor.fetchone()
    assert row2["hop_count"] == 4
    prov2 = json.loads(row2["provenance"])
    assert prov2["hop_count"] == 4


def test_provenance_chain_preservation(test_db):
    """Original provenance fields must be preserved verbatim."""
    now = datetime.utcnow()
    peer_id = "peer-remote-001"

    original_author = "agent-source-001"
    original_timestamp = "2026-04-15T10:00:00"
    confidence = 0.85
    derived_from = ["mem-parent-001"]
    citations = ["https://example.com/source"]
    reasoning = "Because X implies Y"

    bundle = {
        "bundle_id": "bundle-prov-001",
        "peer_id": peer_id,
        "memories": [
            {
                "id": "mem-prov-001",
                "content": "Test provenance preservation",
                "category": "tech",
                "domain": "vpn",
                "privacy_tier": "public",
                "provenance": {
                    "hop_count": 2,
                    "original_author": original_author,
                    "original_timestamp": original_timestamp,
                    "confidence": confidence,
                    "derived_from": derived_from,
                    "citations": citations,
                    "reasoning": reasoning,
                },
            }
        ],
    }

    admit_and_merge(bundle, peer_id=peer_id, now=now)

    cursor = test_db.cursor()
    cursor.execute("SELECT from_agent_id, original_author, provenance FROM shared_memories WHERE id = ?",
                   ("mem-prov-001",))
    row = cursor.fetchone()
    prov = json.loads(row["provenance"])

    # Assert: original_author preserved (NOT overwritten with peer_id)
    assert row["from_agent_id"] == original_author
    assert row["original_author"] == original_author
    assert prov["original_author"] == original_author

    # Assert: original_timestamp preserved (NOT overwritten with now)
    assert prov["original_timestamp"] == original_timestamp

    # Assert: confidence preserved
    assert prov["confidence"] == confidence

    # Assert: derived_from, citations, reasoning preserved
    assert prov["derived_from"] == derived_from
    assert prov["citations"] == citations
    assert prov["reasoning"] == reasoning

    # Assert: hop_count incremented (2 -> 3)
    assert prov["hop_count"] == 3

    # Assert: NEW fields added (received_from, received_at)
    assert prov["received_from"] == peer_id
    assert prov["received_at"] == now.isoformat()


def test_merge_pipeline_fires_on_federation(test_db):
    """Federated memory should trigger merge pipeline and detect conflicts."""
    now = datetime.utcnow()
    peer_id = "peer-remote-001"

    # Seed local memory
    cursor = test_db.cursor()
    cursor.execute("""
        INSERT INTO shared_memories (
            id, room_id, from_agent_id, content, category, domain,
            tags, provenance, privacy_tier, hop_count, original_author,
            confidence, age_days, effective_confidence, shared_at, trust_verified
        ) VALUES (?, 'room-memory-commons', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, 0)
    """, (
        "mem-local-vpn",
        "agent-local",
        "FlashVault uses WireGuard",
        "tech",
        "vpn",
        json.dumps([]),
        json.dumps({
            "hop_count": 1,
            "original_author": "agent-local",
            "original_timestamp": (now - timedelta(hours=1)).isoformat(),
            "confidence": 0.9,
        }),
        "public",
        "agent-local",
        0.9,
        0.9,
        (now - timedelta(hours=1)).isoformat(),
    ))
    test_db.commit()

    # Send federated memory with contradicting content
    bundle = {
        "bundle_id": "bundle-conflict-001",
        "peer_id": peer_id,
        "memories": [
            {
                "id": "mem-remote-vpn",
                "content": "FlashVault does not use WireGuard",
                "category": "tech",
                "domain": "vpn",
                "privacy_tier": "public",
                "provenance": {
                    "hop_count": 1,
                    "original_author": "agent-remote",
                    "original_timestamp": (now - timedelta(minutes=30)).isoformat(),
                    "confidence": 0.8,
                },
            }
        ],
    }

    conflicts = admit_and_merge(bundle, peer_id=peer_id, now=now)

    # Assert: Conflict detected
    assert len(conflicts) == 1

    # Assert: Conflict row written
    cursor.execute("SELECT COUNT(*) as count FROM belief_conflicts WHERE memory_id_a = ? OR memory_id_b = ?",
                   ("mem-local-vpn", "mem-remote-vpn"))
    assert cursor.fetchone()["count"] == 1

    # Assert: Conflict type is contradiction (negation detected)
    cursor.execute("""
        SELECT conflict_type, memory_id_a, memory_id_b FROM belief_conflicts
        WHERE (memory_id_a = ? OR memory_id_b = ?) OR (memory_id_a = ? OR memory_id_b = ?)
    """, ("mem-local-vpn", "mem-local-vpn", "mem-remote-vpn", "mem-remote-vpn"))
    conflict_row = cursor.fetchone()
    assert conflict_row["conflict_type"] == "contradiction"

    # Assert: Both memory IDs present in conflict row
    conflict_ids = {conflict_row["memory_id_a"], conflict_row["memory_id_b"]}
    assert conflict_ids == {"mem-local-vpn", "mem-remote-vpn"}
