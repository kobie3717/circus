"""Background worker that drains the federation_outbox.

Implements durable queuing with exponential backoff for federation push.
Memories are queued in the outbox when published, then asynchronously
delivered to federation peers with retry logic.
"""

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

import httpx

from circus.database import get_db

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = [30, 60, 300, 900, 3600]  # 30s, 1m, 5m, 15m, 1h


async def send_to_peer(peer_url: str, payload: dict, timeout: float = 10.0) -> tuple[bool, Optional[str]]:
    """Attempt HTTP POST to peer's /api/v1/memory-commons/publish.

    Args:
        peer_url: Base URL of the peer (e.g., "http://peer:6200")
        payload: Memory publish payload (dict)
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success: bool, error_message: Optional[str])
    """
    publish_url = f"{peer_url.rstrip('/')}/api/v1/memory-commons/publish"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(publish_url, json=payload)

            if response.status_code in (200, 201):
                return (True, None)
            else:
                error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                return (False, error_msg)

    except httpx.TimeoutException:
        return (False, f"timeout after {timeout}s")
    except httpx.ConnectError as e:
        return (False, f"connection failed: {str(e)[:200]}")
    except Exception as e:
        return (False, f"unexpected error: {str(e)[:200]}")


async def drain_outbox():
    """Process pending outbox entries. Called periodically."""
    now = datetime.utcnow()

    with get_db() as conn:
        cursor = conn.cursor()
        # Get pending entries where next_retry_at <= now
        cursor.execute("""
            SELECT id, peer_url, memory_id, payload, attempt_count
            FROM federation_outbox
            WHERE status = 'pending' AND next_retry_at <= ?
            ORDER BY created_at ASC
            LIMIT 50
        """, (now.isoformat(),))
        entries = cursor.fetchall()

    if not entries:
        return  # Nothing to process

    logger.debug(f"Processing {len(entries)} outbox entries")

    for entry in entries:
        outbox_id = entry["id"]
        peer_url = entry["peer_url"]
        memory_id = entry["memory_id"]
        payload_json = entry["payload"]
        attempt_count = entry["attempt_count"]

        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as e:
            logger.error(f"Malformed payload in outbox {outbox_id}: {e}")
            # Mark as abandoned
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE federation_outbox
                    SET status='abandoned', error=?, last_attempted_at=?
                    WHERE id=?
                """, (f"malformed JSON: {str(e)}", now.isoformat(), outbox_id))
                conn.commit()
            continue

        success, error_msg = await send_to_peer(peer_url, payload)

        with get_db() as conn:
            cursor = conn.cursor()

            if success:
                # Mark delivered
                cursor.execute("""
                    UPDATE federation_outbox
                    SET status='delivered', delivered_at=?, attempt_count=?, last_attempted_at=?
                    WHERE id=?
                """, (now.isoformat(), attempt_count + 1, now.isoformat(), outbox_id))

                # Update peer health (reset consecutive failures)
                cursor.execute("""
                    UPDATE federation_peers
                    SET last_seen_at=?, consecutive_failures=0, is_healthy=1
                    WHERE url=?
                """, (now.isoformat(), peer_url))

                logger.info(f"Delivered {outbox_id} to {peer_url} (memory {memory_id})")
            else:
                # Mark failed
                next_attempt = attempt_count + 1

                if next_attempt >= MAX_ATTEMPTS:
                    # Abandon after max attempts
                    status = 'abandoned'
                    next_retry = None
                    logger.warning(
                        f"Abandoned {outbox_id} after {MAX_ATTEMPTS} attempts (peer {peer_url}): {error_msg}"
                    )
                else:
                    # Schedule retry with exponential backoff
                    status = 'pending'
                    delay = BACKOFF_SECONDS[min(attempt_count, len(BACKOFF_SECONDS) - 1)]
                    next_retry = (now + timedelta(seconds=delay)).isoformat()
                    logger.debug(
                        f"Failed {outbox_id} (attempt {next_attempt}/{MAX_ATTEMPTS}), retry in {delay}s: {error_msg}"
                    )

                cursor.execute("""
                    UPDATE federation_outbox
                    SET status=?, attempt_count=?, last_attempted_at=?, next_retry_at=?, error=?
                    WHERE id=?
                """, (status, next_attempt, now.isoformat(), next_retry, error_msg, outbox_id))

                # Update peer health (increment consecutive failures)
                cursor.execute("""
                    UPDATE federation_peers
                    SET last_failed_at=?, consecutive_failures=consecutive_failures+1,
                        is_healthy=CASE WHEN consecutive_failures+1 >= 3 THEN 0 ELSE 1 END
                    WHERE url=?
                """, (now.isoformat(), peer_url))

            conn.commit()


async def run_federation_worker(interval_seconds: int = 30):
    """Main loop. Runs drain_outbox every interval_seconds.

    Args:
        interval_seconds: Seconds between drain cycles (default 30)
    """
    logger.info(f"Federation worker started (interval={interval_seconds}s)")

    while True:
        try:
            await drain_outbox()
        except Exception as e:
            logger.error(f"Federation worker error: {e}", exc_info=True)

        await asyncio.sleep(interval_seconds)


def get_peer_urls() -> list[str]:
    """Get list of configured peer URLs from CIRCUS_PEERS env var.

    Returns:
        List of peer base URLs (empty list if not configured)
    """
    peers_str = os.getenv("CIRCUS_PEERS", "")
    if not peers_str:
        return []

    # Split by comma, strip whitespace
    return [url.strip() for url in peers_str.split(",") if url.strip()]


def enqueue_for_federation(memory_id: str, payload: dict):
    """Enqueue a memory for federation to all configured peers.

    Called from the publish route after a memory is successfully saved.

    Args:
        memory_id: The memory ID being federated
        payload: Full memory publish payload (dict)
    """
    peer_urls = get_peer_urls()

    if not peer_urls:
        # No peers configured, skip silently
        return

    now = datetime.utcnow()

    with get_db() as conn:
        cursor = conn.cursor()

        for peer_url in peer_urls:
            outbox_id = f"fout-{secrets.token_hex(16)}"

            cursor.execute("""
                INSERT INTO federation_outbox (
                    id, peer_url, memory_id, payload, created_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                outbox_id,
                peer_url,
                memory_id,
                json.dumps(payload),
                now.isoformat(),
                now.isoformat()  # Retry immediately
            ))

        conn.commit()

    logger.debug(f"Enqueued memory {memory_id} for federation to {len(peer_urls)} peers")
