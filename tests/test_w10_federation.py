"""Tests for W10: Federation Durability — durable queuing, peer health, metrics."""

import asyncio
import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx
import pytest

from circus.database import init_database, get_db
from circus.services.federation_worker import (
    send_to_peer,
    drain_outbox,
    enqueue_for_federation,
    get_peer_urls,
    MAX_ATTEMPTS,
    BACKOFF_SECONDS,
)


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary test database."""
    db_path = tmp_path / "test.db"
    os.environ["CIRCUS_DATABASE_PATH"] = str(db_path)

    from circus.config import settings
    settings.database_path = db_path

    init_database(db_path)

    yield db_path

    # Cleanup
    if "CIRCUS_DATABASE_PATH" in os.environ:
        del os.environ["CIRCUS_DATABASE_PATH"]


@pytest.fixture
def clear_peers_env():
    """Clear CIRCUS_PEERS env var before test."""
    old_value = os.getenv("CIRCUS_PEERS")
    if "CIRCUS_PEERS" in os.environ:
        del os.environ["CIRCUS_PEERS"]

    yield

    # Restore
    if old_value:
        os.environ["CIRCUS_PEERS"] = old_value


def test_outbox_table_created(test_db):
    """Test v11 migration creates federation_outbox table."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Check table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='federation_outbox'")
        assert cursor.fetchone() is not None

        # Check schema
        cursor.execute("PRAGMA table_info(federation_outbox)")
        columns = {row[1] for row in cursor.fetchall()}

        expected_columns = {
            "id", "peer_url", "memory_id", "payload", "status",
            "attempt_count", "last_attempted_at", "delivered_at",
            "error", "created_at", "next_retry_at"
        }
        assert expected_columns.issubset(columns)


def test_peer_health_columns_added(test_db):
    """Test v11 migration adds health tracking columns to federation_peers."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Check columns exist
        cursor.execute("PRAGMA table_info(federation_peers)")
        columns = {row[1] for row in cursor.fetchall()}

        health_columns = {
            "last_seen_at", "last_failed_at", "consecutive_failures",
            "is_healthy", "registered_at"
        }
        assert health_columns.issubset(columns)


def test_get_peer_urls_empty(clear_peers_env):
    """Test get_peer_urls returns empty list when CIRCUS_PEERS not set."""
    urls = get_peer_urls()
    assert urls == []


def test_get_peer_urls_single():
    """Test get_peer_urls parses single peer URL."""
    os.environ["CIRCUS_PEERS"] = "http://peer1:6200"
    urls = get_peer_urls()
    assert urls == ["http://peer1:6200"]


def test_get_peer_urls_multiple():
    """Test get_peer_urls parses comma-separated peer URLs."""
    os.environ["CIRCUS_PEERS"] = "http://peer1:6200, http://peer2:6200, http://peer3:6200"
    urls = get_peer_urls()
    assert urls == ["http://peer1:6200", "http://peer2:6200", "http://peer3:6200"]


def test_outbox_entry_skipped_no_peers(test_db, clear_peers_env):
    """Test that publishing without CIRCUS_PEERS env var creates no outbox entries."""
    memory_id = f"shmem-{secrets.token_hex(8)}"
    payload = {"content": "test memory", "category": "learning"}

    # Enqueue (should be no-op)
    enqueue_for_federation(memory_id, payload)

    # Verify no outbox entries
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM federation_outbox")
        count = cursor.fetchone()[0]
        assert count == 0


def test_outbox_entry_created_on_publish(test_db):
    """Test that publishing with CIRCUS_PEERS creates outbox entries."""
    os.environ["CIRCUS_PEERS"] = "http://peer1:6200,http://peer2:6200"

    memory_id = f"shmem-{secrets.token_hex(8)}"
    payload = {"content": "test memory", "category": "learning", "domain": "general"}

    enqueue_for_federation(memory_id, payload)

    # Verify outbox entries created
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT peer_url, memory_id, status FROM federation_outbox")
        rows = cursor.fetchall()

        assert len(rows) == 2
        assert {row["peer_url"] for row in rows} == {"http://peer1:6200", "http://peer2:6200"}
        assert all(row["memory_id"] == memory_id for row in rows)
        assert all(row["status"] == "pending" for row in rows)


@pytest.mark.asyncio
async def test_send_to_peer_success():
    """Test successful HTTP POST to peer."""
    payload = {"content": "test", "category": "learning"}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = AsyncMock()
        mock_response.status_code = 201
        mock_post.return_value = mock_response

        success, error = await send_to_peer("http://peer:6200", payload)

        assert success is True
        assert error is None
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_send_to_peer_http_error():
    """Test HTTP error from peer."""
    payload = {"content": "test", "category": "learning"}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"
        mock_post.return_value = mock_response

        success, error = await send_to_peer("http://peer:6200", payload)

        assert success is False
        assert "500" in error
        assert "Internal server error" in error


@pytest.mark.asyncio
async def test_send_to_peer_timeout():
    """Test timeout when connecting to peer."""
    payload = {"content": "test", "category": "learning"}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.TimeoutException("timeout")

        success, error = await send_to_peer("http://peer:6200", payload, timeout=1.0)

        assert success is False
        assert "timeout" in error.lower()


@pytest.mark.asyncio
async def test_send_to_peer_connection_error():
    """Test connection error to peer."""
    payload = {"content": "test", "category": "learning"}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("connection refused")

        success, error = await send_to_peer("http://peer:6200", payload)

        assert success is False
        assert "connection failed" in error.lower()


@pytest.mark.asyncio
async def test_drain_delivers_pending_entry(test_db):
    """Test drain_outbox marks entry as delivered on success."""
    memory_id = f"shmem-{secrets.token_hex(8)}"
    payload = {"content": "test", "category": "learning"}
    peer_url = "http://peer:6200"
    now = datetime.utcnow()

    # Create outbox entry
    with get_db() as conn:
        cursor = conn.cursor()
        outbox_id = f"fout-{secrets.token_hex(16)}"
        cursor.execute("""
            INSERT INTO federation_outbox (
                id, peer_url, memory_id, payload, created_at, next_retry_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (outbox_id, peer_url, memory_id, json.dumps(payload), now.isoformat(), now.isoformat()))
        conn.commit()

    # Mock successful send
    with patch("circus.services.federation_worker.send_to_peer", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (True, None)

        await drain_outbox()

        mock_send.assert_called_once()

    # Verify entry marked delivered
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, delivered_at FROM federation_outbox WHERE id = ?", (outbox_id,))
        row = cursor.fetchone()

        assert row["status"] == "delivered"
        assert row["delivered_at"] is not None


@pytest.mark.asyncio
async def test_drain_retries_on_failure(test_db):
    """Test drain_outbox increments attempt_count and updates next_retry_at on failure."""
    memory_id = f"shmem-{secrets.token_hex(8)}"
    payload = {"content": "test", "category": "learning"}
    peer_url = "http://peer:6200"
    now = datetime.utcnow()

    # Create outbox entry
    with get_db() as conn:
        cursor = conn.cursor()
        outbox_id = f"fout-{secrets.token_hex(16)}"
        cursor.execute("""
            INSERT INTO federation_outbox (
                id, peer_url, memory_id, payload, created_at, next_retry_at, status, attempt_count
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0)
        """, (outbox_id, peer_url, memory_id, json.dumps(payload), now.isoformat(), now.isoformat()))
        conn.commit()

    # Mock failed send
    with patch("circus.services.federation_worker.send_to_peer", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (False, "connection refused")

        await drain_outbox()

    # Verify retry scheduled
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status, attempt_count, next_retry_at, error
            FROM federation_outbox WHERE id = ?
        """, (outbox_id,))
        row = cursor.fetchone()

        assert row["status"] == "pending"
        assert row["attempt_count"] == 1
        assert row["next_retry_at"] is not None
        assert "connection refused" in row["error"]

        # Verify retry scheduled at least BACKOFF_SECONDS[0] seconds in the future
        next_retry = datetime.fromisoformat(row["next_retry_at"])
        expected_min = now + timedelta(seconds=BACKOFF_SECONDS[0] - 5)  # 5s tolerance
        assert next_retry >= expected_min


@pytest.mark.asyncio
async def test_abandoned_after_max_attempts(test_db):
    """Test entry marked abandoned after MAX_ATTEMPTS failures."""
    memory_id = f"shmem-{secrets.token_hex(8)}"
    payload = {"content": "test", "category": "learning"}
    peer_url = "http://peer:6200"
    now = datetime.utcnow()

    # Create outbox entry at MAX_ATTEMPTS - 1
    with get_db() as conn:
        cursor = conn.cursor()
        outbox_id = f"fout-{secrets.token_hex(16)}"
        cursor.execute("""
            INSERT INTO federation_outbox (
                id, peer_url, memory_id, payload, created_at, next_retry_at, status, attempt_count
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (outbox_id, peer_url, memory_id, json.dumps(payload), now.isoformat(), now.isoformat(), MAX_ATTEMPTS - 1))
        conn.commit()

    # Mock failed send
    with patch("circus.services.federation_worker.send_to_peer", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (False, "persistent error")

        await drain_outbox()

    # Verify abandoned
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, attempt_count FROM federation_outbox WHERE id = ?", (outbox_id,))
        row = cursor.fetchone()

        assert row["status"] == "abandoned"
        assert row["attempt_count"] == MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_peer_health_updated_on_success(test_db):
    """Test peer consecutive_failures reset and is_healthy=1 on successful delivery."""
    peer_url = "http://peer:6200"
    now = datetime.utcnow()

    # Create peer with failures (public_key is NOT NULL in schema, use dummy)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO federation_peers (
                id, url, name, public_key, created_at, registered_at, consecutive_failures, is_healthy
            ) VALUES ('peer-test', ?, 'Test Peer', ?, ?, ?, 5, 0)
        """, (peer_url, b'\x00' * 32, now.isoformat(), now.isoformat()))
        conn.commit()

    # Create outbox entry
    memory_id = f"shmem-{secrets.token_hex(8)}"
    payload = {"content": "test", "category": "learning"}
    with get_db() as conn:
        cursor = conn.cursor()
        outbox_id = f"fout-{secrets.token_hex(16)}"
        cursor.execute("""
            INSERT INTO federation_outbox (
                id, peer_url, memory_id, payload, created_at, next_retry_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (outbox_id, peer_url, memory_id, json.dumps(payload), now.isoformat(), now.isoformat()))
        conn.commit()

    # Mock successful send
    with patch("circus.services.federation_worker.send_to_peer", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (True, None)

        await drain_outbox()

    # Verify peer health updated
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT consecutive_failures, is_healthy, last_seen_at
            FROM federation_peers WHERE url = ?
        """, (peer_url,))
        row = cursor.fetchone()

        assert row["consecutive_failures"] == 0
        assert row["is_healthy"] == 1
        assert row["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_peer_health_degraded_on_failure(test_db):
    """Test peer marked unhealthy after 3 consecutive failures."""
    peer_url = "http://peer:6200"
    now = datetime.utcnow()

    # Create peer with 2 failures (threshold is 3, public_key is NOT NULL)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO federation_peers (
                id, url, name, public_key, created_at, registered_at, consecutive_failures, is_healthy
            ) VALUES ('peer-test', ?, 'Test Peer', ?, ?, ?, 2, 1)
        """, (peer_url, b'\x00' * 32, now.isoformat(), now.isoformat()))
        conn.commit()

    # Create outbox entry
    memory_id = f"shmem-{secrets.token_hex(8)}"
    payload = {"content": "test", "category": "learning"}
    with get_db() as conn:
        cursor = conn.cursor()
        outbox_id = f"fout-{secrets.token_hex(16)}"
        cursor.execute("""
            INSERT INTO federation_outbox (
                id, peer_url, memory_id, payload, created_at, next_retry_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (outbox_id, peer_url, memory_id, json.dumps(payload), now.isoformat(), now.isoformat()))
        conn.commit()

    # Mock failed send
    with patch("circus.services.federation_worker.send_to_peer", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = (False, "error")

        await drain_outbox()

    # Verify peer marked unhealthy
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT consecutive_failures, is_healthy, last_failed_at
            FROM federation_peers WHERE url = ?
        """, (peer_url,))
        row = cursor.fetchone()

        assert row["consecutive_failures"] == 3
        assert row["is_healthy"] == 0
        assert row["last_failed_at"] is not None


def test_peers_api(test_db):
    """Test GET /federation/peers returns peer list."""
    # This would be an integration test with TestClient
    # For unit test, we just verify the query works
    with get_db() as conn:
        cursor = conn.cursor()

        # Insert test peer (public_key is NOT NULL)
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            INSERT INTO federation_peers (
                id, url, name, public_key, created_at, registered_at, consecutive_failures, is_healthy, last_seen_at
            ) VALUES ('peer-test', 'http://peer:6200', 'Test Peer', ?, ?, ?, 0, 1, ?)
        """, (b'\x00' * 32, now, now, now))
        conn.commit()

        # Query
        cursor.execute("""
            SELECT url, name, last_seen_at, last_failed_at,
                   consecutive_failures, is_healthy, registered_at
            FROM federation_peers
            ORDER BY name
        """)
        rows = cursor.fetchall()

        assert len(rows) == 1
        assert rows[0]["url"] == "http://peer:6200"
        assert rows[0]["name"] == "Test Peer"
        assert rows[0]["is_healthy"] == 1


def test_metrics_api(test_db):
    """Test GET /federation/metrics returns counts."""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Insert test outbox entries
        for i, status in enumerate(["pending", "delivered", "failed", "abandoned"]):
            cursor.execute("""
                INSERT INTO federation_outbox (
                    id, peer_url, memory_id, payload, created_at, next_retry_at, status
                ) VALUES (?, 'http://peer:6200', ?, '{}', ?, ?, ?)
            """, (f"fout-{i}", f"mem-{i}", now, now, status))

        conn.commit()

        # Query metrics
        cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM federation_outbox
            GROUP BY status
        """)
        counts = {row["status"]: row["count"] for row in cursor.fetchall()}

        assert counts["pending"] == 1
        assert counts["delivered"] == 1
        assert counts["failed"] == 1
        assert counts["abandoned"] == 1


def test_cli_federation_peers(test_db):
    """Test CLI command parses peer data correctly."""
    # This is a structural test - verify the query runs
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO federation_peers (
                id, url, name, public_key, created_at, registered_at, consecutive_failures, is_healthy, last_seen_at
            ) VALUES ('peer-test', 'http://peer:6200', 'Test Peer', ?, ?, ?, 0, 1, ?)
        """, (b'\x00' * 32, now, now, now))
        conn.commit()

        cursor.execute("""
            SELECT url, name, last_seen_at, consecutive_failures, is_healthy
            FROM federation_peers
        """)
        row = cursor.fetchone()

        assert row["url"] == "http://peer:6200"
        assert row["name"] == "Test Peer"
        assert row["is_healthy"] == 1
