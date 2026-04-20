"""Non-fatal bridge: sync Circus preferences to AI-IQ memory-tool."""

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

MEMORY_TOOL = "memory-tool"  # global command


def sync_preference_to_aiiq(
    owner_id: str,
    field: str,
    value: str,
    confidence: float,
    reasoning: str = "",
) -> bool:
    """Write/update an activated preference to AI-IQ. Non-fatal — returns False on error."""
    try:
        content = f"{owner_id} prefers {field}={value} (confidence={confidence:.2f})"
        if reasoning:
            content += f" — {reasoning}"

        result = subprocess.run(
            [
                MEMORY_TOOL, "add", "preference", content,
                "--tags", f"circus,preference,{owner_id}",
                "--key", f"circus-pref-{owner_id}-{field.replace('.', '-')}",
                "--priority", str(max(1, min(10, int(confidence * 10)))),
            ],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            logger.info(f"AI-IQ synced: {field}={value} for {owner_id}")
            return True
        else:
            logger.warning(f"AI-IQ sync failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        logger.warning(f"AI-IQ bridge error (non-fatal): {e}")
        return False


def clear_preference_in_aiiq(owner_id: str, field: str) -> bool:
    """Search and delete the preference memory in AI-IQ. Non-fatal."""
    try:
        key = f"circus-pref-{owner_id}-{field.replace('.', '-')}"
        # Search by key first
        search = subprocess.run(
            [MEMORY_TOOL, "search", field, "--keyword", "--tags", f"circus,{owner_id}"],
            capture_output=True, text=True, timeout=10
        )
        # Parse memory IDs from output (format: "#XXXX [category]")
        import re
        ids = re.findall(r'#(\d+)\s+\[', search.stdout)
        for mid in ids[:3]:  # cap at 3 to avoid accidents
            subprocess.run([MEMORY_TOOL, "delete", mid], capture_output=True, timeout=5)
        return True
    except Exception as e:
        logger.warning(f"AI-IQ clear error (non-fatal): {e}")
        return False
