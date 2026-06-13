"""
Webhook delivery worker — polls webhook_deliveries for pending rows and POSTs them.
Runs as a background asyncio task started in app lifespan.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import httpx

from circus.database import get_db

logger = logging.getLogger(__name__)

# Configuration
POLL_INTERVAL_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3
RETRY_DELAYS = {
    1: 0,          # immediate
    2: 30,         # 30s after first failure
    3: 300,        # 5min after second failure
}


def compute_signature(payload_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook payload."""
    return hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()


async def deliver_webhook(
    delivery_id: int,
    subscription_id: int,
    url: str,
    event_type: str,
    payload: dict,
    secret: Optional[str],
    attempt_count: int
) -> tuple[bool, Optional[int], Optional[str]]:
    """
    Deliver a single webhook via HTTP POST.

    Returns: (success: bool, status_code: Optional[int], error: Optional[str])
    """
    try:
        payload_json = json.dumps(payload)
        payload_bytes = payload_json.encode('utf-8')

        headers = {
            'Content-Type': 'application/json',
            'X-Circus-Event-Type': event_type,
            'X-Circus-Delivery-ID': str(delivery_id),
        }

        # Add HMAC signature if secret provided
        if secret:
            signature = compute_signature(payload_bytes, secret)
            headers['X-Circus-Signature'] = f'sha256={signature}'

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                content=payload_bytes,
                headers=headers
            )

            # 2xx = success
            if 200 <= response.status_code < 300:
                logger.info(
                    f"Webhook delivery {delivery_id} succeeded: {url} "
                    f"(status={response.status_code})"
                )
                return True, response.status_code, None
            else:
                error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    f"Webhook delivery {delivery_id} failed: {url} - {error}"
                )
                return False, response.status_code, error

    except asyncio.TimeoutError:
        error = "Request timeout (10s)"
        logger.warning(f"Webhook delivery {delivery_id} timeout: {url}")
        return False, None, error

    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
        logger.warning(f"Webhook delivery {delivery_id} error: {url} - {error}")
        return False, None, error


async def process_pending_deliveries():
    """
    Poll for pending webhook deliveries and process them.
    Called repeatedly by the background worker.
    """
    try:
        with get_db() as conn:
            # Find pending deliveries that are ready to retry
            # (either never attempted, or retry delay has passed)
            now = datetime.utcnow().isoformat()

            cursor = conn.cursor()

            # Get pending deliveries with subscription details
            # Schema from v36: webhook_deliveries has subscription_id, event_type, payload, status, attempt_count
            # Schema from v36: webhook_subscriptions has url, secret
            rows = cursor.execute("""
                SELECT
                    d.id,
                    d.subscription_id,
                    d.event_type,
                    d.payload,
                    s.url,
                    s.secret,
                    d.attempt_count
                FROM webhook_deliveries d
                JOIN webhook_subscriptions s ON s.id = d.subscription_id
                WHERE d.status = 'pending'
                  AND s.is_active = 1
                  AND s.failure_count < 5
                ORDER BY d.created_at ASC
                LIMIT 50
            """).fetchall()

            if not rows:
                return  # No pending deliveries

            logger.debug(f"Processing {len(rows)} pending webhook deliveries")

        # Process each delivery (outside DB context to avoid blocking)
        for row in rows:
            delivery_id = row[0]
            subscription_id = row[1]
            event_type = row[2]
            payload_json = row[3]
            url = row[4]
            secret = row[5]
            attempt_count = row[6]

            # Parse payload
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                logger.error(f"Webhook delivery {delivery_id} has invalid JSON payload, marking failed")
                with get_db() as conn:
                    conn.execute("""
                        UPDATE webhook_deliveries
                        SET status = 'failed',
                            last_error = 'Invalid JSON payload',
                            attempted_at = ?
                        WHERE id = ?
                    """, (datetime.utcnow().isoformat(), delivery_id))
                    conn.commit()
                continue

            # Check if we should retry (based on attempt count and retry delay)
            if attempt_count >= MAX_ATTEMPTS:
                # Max attempts reached, mark as failed
                logger.warning(f"Webhook delivery {delivery_id} max attempts reached, marking failed")
                with get_db() as conn:
                    conn.execute("""
                        UPDATE webhook_deliveries
                        SET status = 'failed'
                        WHERE id = ?
                    """, (delivery_id,))
                    conn.execute("""
                        UPDATE webhook_subscriptions
                        SET failure_count = failure_count + 1
                        WHERE id = ?
                    """, (subscription_id,))
                    conn.commit()
                continue

            # Attempt delivery
            success, status_code, error = await deliver_webhook(
                delivery_id,
                subscription_id,
                url,
                event_type,
                payload,
                secret,
                attempt_count + 1
            )

            # Update delivery record
            with get_db() as conn:
                now = datetime.utcnow().isoformat()

                if success:
                    # Mark as delivered
                    conn.execute("""
                        UPDATE webhook_deliveries
                        SET status = 'delivered',
                            response_status = ?,
                            attempt_count = ?,
                            attempted_at = ?,
                            delivered_at = ?
                        WHERE id = ?
                    """, (status_code, attempt_count + 1, now, now, delivery_id))

                    # Update subscription success metadata
                    conn.execute("""
                        UPDATE webhook_subscriptions
                        SET last_triggered_at = ?,
                            failure_count = 0
                        WHERE id = ?
                    """, (now, subscription_id))

                else:
                    # Increment attempt count, keep as pending or mark failed
                    new_attempt_count = attempt_count + 1

                    if new_attempt_count >= MAX_ATTEMPTS:
                        # Final failure
                        conn.execute("""
                            UPDATE webhook_deliveries
                            SET status = 'failed',
                                response_status = ?,
                                attempt_count = ?,
                                last_error = ?,
                                attempted_at = ?
                            WHERE id = ?
                        """, (status_code, new_attempt_count, error, now, delivery_id))

                        # Increment subscription failure count
                        conn.execute("""
                            UPDATE webhook_subscriptions
                            SET failure_count = failure_count + 1
                            WHERE id = ?
                        """, (subscription_id,))

                        # Auto-disable subscription if too many failures
                        conn.execute("""
                            UPDATE webhook_subscriptions
                            SET is_active = 0
                            WHERE id = ? AND failure_count >= 5
                        """, (subscription_id,))
                    else:
                        # Keep pending for retry
                        conn.execute("""
                            UPDATE webhook_deliveries
                            SET response_status = ?,
                                attempt_count = ?,
                                last_error = ?,
                                attempted_at = ?
                            WHERE id = ?
                        """, (status_code, new_attempt_count, error, now, delivery_id))

                conn.commit()

            # Add delay between deliveries to avoid overwhelming target servers
            await asyncio.sleep(0.1)

    except Exception as e:
        logger.error(f"Error in webhook delivery processing: {e}", exc_info=True)


async def webhook_worker():
    """
    Background worker that continuously polls for pending webhook deliveries.
    Runs forever in the background, restarts after crash with 10s delay.
    """
    logger.info("Webhook delivery worker starting")

    while True:
        try:
            await process_pending_deliveries()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            logger.info("Webhook worker cancelled, shutting down")
            raise

        except Exception as e:
            logger.error(f"Webhook worker crashed: {e}", exc_info=True)
            logger.info("Webhook worker will restart in 10 seconds")
            await asyncio.sleep(10)


async def start_webhook_worker():
    """Entry point for starting the webhook worker as a background task."""
    await webhook_worker()
