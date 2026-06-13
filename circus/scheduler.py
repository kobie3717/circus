"""
Background scheduler — runs periodic maintenance tasks.
Started as asyncio task in app lifespan.
"""
import asyncio
import logging
from datetime import datetime

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
    """
    tasks = [
        asyncio.create_task(consolidation_job(db_path), name="consolidation"),
        asyncio.create_task(escrow_release_job(db_path), name="escrow-release"),
    ]
    # Log to both logger and stdout to ensure visibility
    msg = f"Scheduler started: {len(tasks)} jobs (consolidation, escrow-release)"
    logger.info(msg)
    print(f"[Scheduler] {msg}")
    return tasks
