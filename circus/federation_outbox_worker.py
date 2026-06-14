"""Federation outbox delivery worker for Circus.

Polls federation_outbox table and delivers memory claims to peer Circus instances
via HTTP POST to /api/v1/federation/push with Ed25519 signed headers.

Implements exponential backoff retry with peer health tracking.
"""

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from circus.database import get_db
from circus.services.instance_identity import get_instance_identity, InstanceIdentityError

logger = logging.getLogger(__name__)

# Configuration
POLL_INTERVAL_SECONDS = 10
HTTP_TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 4
RETRY_DELAYS = [0, 60, 300, 900]  # 0s, 1m, 5m, 15m
MAX_BATCH_SIZE = 20


def sign_push_challenge(instance_id: str, private_key_bytes: bytes) -> str:
    """Generate Ed25519 signature for federation push authentication.

    Signature scheme: Ed25519 sign of "push:{instance_id}:{minute_bucket}"
    where minute_bucket = int(time.time() // 60)

    Args:
        instance_id: Our instance's unique identifier
        private_key_bytes: 32-byte Ed25519 private key (raw bytes)

    Returns:
        Base64-encoded signature string
    """
    minute_bucket = int(time.time() // 60)
    challenge = f"push:{instance_id}:{minute_bucket}".encode()

    key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    sig = key.sign(challenge)

    return base64.b64encode(sig).decode()


async def deliver_to_peer(
    peer_url: str,
    payload: dict,
    instance_id: str,
    private_key_bytes: bytes,
    timeout: float = HTTP_TIMEOUT_SECONDS
) -> tuple[bool, Optional[int], Optional[str]]:
    """Deliver a memory claim to a peer instance via signed HTTP POST.

    Args:
        peer_url: Base URL of the peer instance
        payload: Memory publish payload (dict)
        instance_id: Our instance ID
        private_key_bytes: Our Ed25519 private key bytes
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success: bool, status_code: Optional[int], error: Optional[str])
    """
    push_url = f"{peer_url.rstrip('/')}/api/v1/federation/push"

    try:
        # Generate signed headers
        signature = sign_push_challenge(instance_id, private_key_bytes)

        headers = {
            'Content-Type': 'application/json',
            'X-Peer-Id': instance_id,
            'X-Peer-Signature': signature,
        }

        payload_json = json.dumps(payload)

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                push_url,
                content=payload_json.encode('utf-8'),
                headers=headers
            )

            # 2xx = success
            if 200 <= response.status_code < 300:
                logger.info(
                    f"Federation push succeeded: {peer_url} "
                    f"(status={response.status_code})"
                )
                return True, response.status_code, None
            else:
                error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    f"Federation push failed: {peer_url} - {error}"
                )
                return False, response.status_code, error

    except asyncio.TimeoutError:
        error = f"Request timeout ({timeout}s)"
        logger.warning(f"Federation push timeout: {peer_url}")
        return False, None, error

    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
        logger.warning(f"Federation push error: {peer_url} - {error}")
        return False, None, error


async def process_pending_deliveries():
    """Poll for pending outbox entries and deliver them to peers.

    Called repeatedly by the background worker.
    """
    try:
        # Get instance identity for signing
        with get_db() as conn:
            try:
                identity = get_instance_identity(conn)
            except InstanceIdentityError as e:
                logger.error(f"Cannot deliver federation messages: {e}")
                return

            instance_id = identity.instance_id
            private_key_bytes = identity.private_key_bytes

            # Find pending deliveries ready to retry
            now = datetime.utcnow().isoformat()

            cursor = conn.cursor()
            rows = cursor.execute("""
                SELECT id, peer_url, memory_id, payload, attempt_count
                FROM federation_outbox
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
            """, (now, MAX_BATCH_SIZE)).fetchall()

        if not rows:
            return  # No pending deliveries

        msg = f"Processing {len(rows)} pending federation deliveries"
        logger.debug(msg)
        print(f"[Federation Outbox] {msg}", flush=True)

        # Process each delivery (outside DB context to avoid blocking)
        for row in rows:
            outbox_id = row[0]
            peer_url = row[1]
            memory_id = row[2]
            payload_json = row[3]
            attempt_count = row[4]

            # Parse payload
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                logger.error(f"Federation outbox {outbox_id} has invalid JSON payload, marking abandoned")
                with get_db() as conn:
                    conn.execute("""
                        UPDATE federation_outbox
                        SET status = 'abandoned',
                            error = 'Invalid JSON payload',
                            last_attempted_at = ?
                        WHERE id = ?
                    """, (datetime.utcnow().isoformat(), outbox_id))
                    conn.commit()
                continue

            # Attempt delivery
            success, status_code, error = await deliver_to_peer(
                peer_url,
                payload,
                instance_id,
                private_key_bytes
            )

            # Update delivery record
            with get_db() as conn:
                now_iso = datetime.utcnow().isoformat()
                new_attempt_count = attempt_count + 1

                if success:
                    # Mark as delivered
                    conn.execute("""
                        UPDATE federation_outbox
                        SET status = 'delivered',
                            delivered_at = ?,
                            attempt_count = ?,
                            last_attempted_at = ?
                        WHERE id = ?
                    """, (now_iso, new_attempt_count, now_iso, outbox_id))

                    # Update peer health (reset consecutive failures)
                    conn.execute("""
                        UPDATE federation_peers
                        SET last_seen_at = ?,
                            consecutive_failures = 0,
                            is_healthy = 1
                        WHERE url = ?
                    """, (now_iso, peer_url))

                    logger.info(f"Delivered federation outbox {outbox_id} to {peer_url} (memory {memory_id})")

                else:
                    # Determine retry strategy based on status code
                    should_abandon = False

                    # 4xx errors (except 429) = client error, don't retry
                    if status_code and 400 <= status_code < 500 and status_code != 429:
                        should_abandon = True
                        logger.warning(
                            f"Abandoning federation outbox {outbox_id} due to client error {status_code}"
                        )

                    # Max attempts reached
                    elif new_attempt_count >= MAX_ATTEMPTS:
                        should_abandon = True
                        logger.warning(
                            f"Abandoning federation outbox {outbox_id} after {MAX_ATTEMPTS} attempts"
                        )

                    if should_abandon:
                        # Mark as abandoned
                        conn.execute("""
                            UPDATE federation_outbox
                            SET status = 'abandoned',
                                attempt_count = ?,
                                last_attempted_at = ?,
                                error = ?
                            WHERE id = ?
                        """, (new_attempt_count, now_iso, error, outbox_id))

                        # Update peer health (increment consecutive failures)
                        conn.execute("""
                            UPDATE federation_peers
                            SET last_failed_at = ?,
                                consecutive_failures = consecutive_failures + 1,
                                is_healthy = CASE WHEN consecutive_failures + 1 >= 3 THEN 0 ELSE 1 END
                            WHERE url = ?
                        """, (now_iso, peer_url))

                    else:
                        # Schedule retry with exponential backoff
                        delay = RETRY_DELAYS[min(new_attempt_count - 1, len(RETRY_DELAYS) - 1)]
                        next_retry_at = (datetime.utcnow() + timedelta(seconds=delay)).isoformat()

                        conn.execute("""
                            UPDATE federation_outbox
                            SET attempt_count = ?,
                                last_attempted_at = ?,
                                next_retry_at = ?,
                                error = ?
                            WHERE id = ?
                        """, (new_attempt_count, now_iso, next_retry_at, error, outbox_id))

                        # Update peer health (increment consecutive failures)
                        conn.execute("""
                            UPDATE federation_peers
                            SET last_failed_at = ?,
                                consecutive_failures = consecutive_failures + 1,
                                is_healthy = CASE WHEN consecutive_failures + 1 >= 3 THEN 0 ELSE 1 END
                            WHERE url = ?
                        """, (now_iso, peer_url))

                        logger.debug(
                            f"Federation outbox {outbox_id} failed (attempt {new_attempt_count}/{MAX_ATTEMPTS}), "
                            f"retry in {delay}s: {error}"
                        )

                conn.commit()

            # Small delay between deliveries to avoid overwhelming peers
            await asyncio.sleep(0.1)

    except Exception as e:
        logger.error(f"Error in federation outbox processing: {e}", exc_info=True)


async def federation_outbox_worker():
    """Background worker that continuously polls for pending federation deliveries.

    Runs forever in the background, restarts after crash with 10s delay.
    """
    msg = "Federation outbox worker starting"
    logger.info(msg)
    print(f"[Federation Outbox] {msg}", flush=True)

    while True:
        try:
            await process_pending_deliveries()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            logger.info("Federation outbox worker cancelled, shutting down")
            raise

        except Exception as e:
            logger.error(f"Federation outbox worker crashed: {e}", exc_info=True)
            logger.info("Federation outbox worker will restart in 10 seconds")
            await asyncio.sleep(10)


async def start_federation_outbox_worker():
    """Entry point for starting the federation outbox worker as a background task."""
    await federation_outbox_worker()
