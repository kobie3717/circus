"""
Background scheduler — runs periodic maintenance tasks.
Started as asyncio task in app lifespan.
"""
import asyncio
import base64
import logging
import secrets
from datetime import datetime

import httpx

from circus.database import get_db
from circus.memory_engine import run_consolidation

logger = logging.getLogger(__name__)


async def consolidation_job(db_path: str):
    """
    Memory consolidation job — runs every 6 hours.
    Promotes candidates, archives low-score claims, mines recurring patterns.
    """
    print("[Scheduler] Memory consolidation job started (runs every 6 hours)")
    while True:
        await asyncio.sleep(6 * 3600)  # 6 hours
        try:
            with get_db() as conn:
                stats = run_consolidation(conn)
                msg = (
                    f"Memory consolidation complete: promoted={stats['promoted']}, "
                    f"archived={stats['archived']}, episodic->semantic={stats['episodic_to_semantic']}"
                )
                logger.info(msg)
                print(f"[Scheduler] {msg}")
        except Exception as e:
            logger.error(f"Consolidation job failed: {e}", exc_info=True)
            print(f"[Scheduler] ERROR: Consolidation job failed: {e}")


async def escrow_release_job(db_path: str):
    """
    Escrow auto-release job — runs every 30 minutes.
    Releases escrows past dispute window with no open disputes.
    """
    print("[Scheduler] Escrow auto-release job started (runs every 30 minutes)")
    while True:
        await asyncio.sleep(1800)  # 30 minutes
        try:
            released_count = auto_release_expired_escrows(db_path)
            if released_count > 0:
                msg = f"Auto-released {released_count} expired escrow(s)"
                logger.info(msg)
                print(f"[Scheduler] {msg}")
        except Exception as e:
            logger.error(f"Escrow auto-release job failed: {e}", exc_info=True)
            print(f"[Scheduler] ERROR: Escrow auto-release job failed: {e}")


async def peer_discovery_job():
    """Peer discovery job — runs every 4 hours.

    Fetches /.well-known/circus-peers.json from each known peer and
    auto-registers any peers they know that we don't.
    """
    print("[Scheduler] Peer discovery job started (runs every 4 hours)")
    while True:
        await asyncio.sleep(4 * 3600)  # 4 hours
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Get all peers (both healthy and unhealthy to check status)
                cursor.execute("SELECT url FROM federation_peers")
                peers = cursor.fetchall()

            discovered_count = 0
            updated_count = 0

            async with httpx.AsyncClient(timeout=5.0) as client:
                for peer_row in peers:
                    peer_url = peer_row["url"]

                    try:
                        # Fetch well-known endpoint
                        well_known_url = f"{peer_url.rstrip('/')}/.well-known/circus-peers.json"
                        response = await client.get(well_known_url)

                        if response.status_code != 200:
                            # Peer failed to respond
                            with get_db() as conn:
                                cursor = conn.cursor()
                                cursor.execute("""
                                    UPDATE federation_peers
                                    SET consecutive_failures = consecutive_failures + 1
                                    WHERE url = ?
                                """, (peer_url,))

                                # Mark unhealthy after 3 failures
                                cursor.execute("""
                                    UPDATE federation_peers
                                    SET is_healthy = 0
                                    WHERE url = ? AND consecutive_failures >= 3
                                """, (peer_url,))
                                conn.commit()
                            continue

                        data = response.json()

                        # Peer responded successfully
                        with get_db() as conn:
                            cursor = conn.cursor()
                            now = datetime.utcnow().isoformat()

                            # Update last_seen_at and reset consecutive_failures
                            cursor.execute("""
                                UPDATE federation_peers
                                SET last_seen_at = ?, consecutive_failures = 0, is_healthy = 1
                                WHERE url = ?
                            """, (now, peer_url))
                            updated_count += 1

                            # Process peer's known peers
                            for peer_data in data.get("peers", []):
                                peer_of_peer_url = peer_data.get("url")
                                if not peer_of_peer_url:
                                    continue

                                # Skip if we already know this peer
                                cursor.execute("SELECT url FROM federation_peers WHERE url = ?", (peer_of_peer_url,))
                                if cursor.fetchone():
                                    continue

                                # Auto-register peer-of-peer
                                peer_id = f"peer-{secrets.token_hex(8)}"
                                peer_public_key_b64 = peer_data.get("public_key", "")
                                peer_public_key_bytes = base64.b64decode(peer_public_key_b64) if peer_public_key_b64 else b'\x00' * 32

                                try:
                                    cursor.execute("""
                                        INSERT INTO federation_peers (
                                            id, url, name, public_key, created_at, registered_at,
                                            is_healthy, consecutive_failures, auto_discovered
                                        ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, 1)
                                    """, (
                                        peer_id,
                                        peer_of_peer_url,
                                        peer_data.get("name", peer_data.get("instance_id", peer_of_peer_url)),
                                        peer_public_key_bytes,
                                        now,
                                        now
                                    ))
                                    discovered_count += 1
                                except Exception as e:
                                    logger.warning(f"Failed to auto-register peer {peer_of_peer_url}: {e}")

                            conn.commit()

                    except Exception as e:
                        logger.warning(f"Peer discovery failed for {peer_url}: {e}")
                        # Increment failure count
                        with get_db() as conn:
                            cursor = conn.cursor()
                            cursor.execute("""
                                UPDATE federation_peers
                                SET consecutive_failures = consecutive_failures + 1
                                WHERE url = ?
                            """, (peer_url,))

                            # Mark unhealthy after 3 failures
                            cursor.execute("""
                                UPDATE federation_peers
                                SET is_healthy = 0
                                WHERE url = ? AND consecutive_failures >= 3
                            """, (peer_url,))
                            conn.commit()

            if discovered_count > 0 or updated_count > 0:
                msg = f"Peer discovery complete: discovered={discovered_count}, updated={updated_count}"
                logger.info(msg)
                print(f"[Scheduler] {msg}")

        except Exception as e:
            logger.error(f"Peer discovery job failed: {e}", exc_info=True)
            print(f"[Scheduler] ERROR: Peer discovery job failed: {e}")


def auto_release_expired_escrows(db_path: str) -> int:
    """
    Auto-release escrows past dispute window (30 days) with no open disputes.
    Returns count of escrows released.

    Logic:
    - Query task_escrow WHERE status='locked' AND release_date <= now()
    - For each escrow, check if there's an open dispute (fraud_reports.status='open')
    - If no open dispute, set status='released', released_at=now()
    - Return count of released escrows
    """
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow()

        # Find eligible escrows (past release date, still locked)
        eligible = cursor.execute(
            "SELECT id, task_id FROM task_escrow WHERE status = 'locked' AND release_date <= ?",
            (now.isoformat(),)
        ).fetchall()

        released_count = 0

        for escrow_id, task_id in eligible:
            # Check for open disputes
            open_dispute = cursor.execute(
                "SELECT id FROM fraud_reports WHERE task_id = ? AND status = 'open'",
                (task_id,)
            ).fetchone()

            if open_dispute:
                # Skip — dispute still open
                continue

            # Release escrow
            cursor.execute(
                "UPDATE task_escrow SET status = 'released', released_at = ? WHERE id = ?",
                (now.isoformat(), escrow_id)
            )
            released_count += 1

        conn.commit()
        return released_count


async def start_scheduler(db_path: str) -> list[asyncio.Task]:
    """
    Start all scheduler jobs. Returns tasks for cancellation on shutdown.

    Jobs:
    - Memory consolidation (every 6 hours)
    - Escrow auto-release (every 30 minutes)
    - Peer discovery (every 4 hours)
    """
    tasks = [
        asyncio.create_task(consolidation_job(db_path), name="consolidation"),
        asyncio.create_task(escrow_release_job(db_path), name="escrow-release"),
        asyncio.create_task(peer_discovery_job(), name="peer-discovery"),
    ]
    # Log to both logger and stdout to ensure visibility
    msg = f"Scheduler started: {len(tasks)} jobs (consolidation, escrow-release, peer-discovery)"
    logger.info(msg)
    print(f"[Scheduler] {msg}")
    return tasks
