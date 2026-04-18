"""Hull integrity monitoring for The Circus.

Reads actual token counts from Claude Code JSONL session files and maps
them to Green/Amber/Red/Critical thresholds. Provides a squadron readiness
board aggregating all registered agents' context health.

Inspired by harrymunro/nelson's hull integrity system.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Thresholds (% remaining capacity)
# ---------------------------------------------------------------------------

THRESHOLDS = {
    "Green": 75,    # >= 75% remaining → no action
    "Amber": 60,    # >= 60% remaining → monitor closely
    "Red": 40,      # >= 40% remaining → plan relief
    "Critical": 0,  # < 40% remaining → immediate action
}

DEFAULT_TOKEN_LIMIT = 200_000


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def get_status(pct_remaining: int) -> str:
    """Return Green/Amber/Red/Critical for a given remaining percentage."""
    if pct_remaining >= THRESHOLDS["Green"]:
        return "Green"
    if pct_remaining >= THRESHOLDS["Amber"]:
        return "Amber"
    if pct_remaining >= THRESHOLDS["Red"]:
        return "Red"
    return "Critical"


def count_tokens_from_jsonl(path: str) -> Optional[int]:
    """Read a Claude Code JSONL session file and extract actual token count.

    Finds the last assistant message with usage data and sums:
    input_tokens + cache_creation_input_tokens + cache_read_input_tokens

    Returns None if no usage data found.
    """
    last_usage = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "assistant":
                    continue
                msg = record.get("message")
                if not isinstance(msg, dict) or "usage" not in msg:
                    continue
                last_usage = msg["usage"]
    except (OSError, IOError):
        return None

    if last_usage is None:
        return None

    return (
        last_usage.get("input_tokens", 0)
        + last_usage.get("cache_creation_input_tokens", 0)
        + last_usage.get("cache_read_input_tokens", 0)
    )


def build_report(
    agent_name: str,
    token_count: int,
    token_limit: int = DEFAULT_TOKEN_LIMIT,
) -> dict:
    """Build a hull integrity report dict."""
    remaining = max(token_limit - token_count, 0)
    pct = int((remaining / token_limit) * 100) if token_limit > 0 else 0
    status = get_status(pct)

    return {
        "agent_name": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_count": token_count,
        "token_limit": token_limit,
        "hull_integrity_pct": pct,
        "hull_integrity_status": status,
        "relief_requested": status in ("Red", "Critical"),
    }


def scan_session_dir(
    session_dir: str,
    token_limit: int = DEFAULT_TOKEN_LIMIT,
) -> list[dict]:
    """Scan a Claude Code session directory for flagship + subagent JSONL files.

    Layout:
        {session-id}/
            subagents/
                agent-{agentId}.jsonl
        {session-id}.jsonl  (sibling file = flagship)

    Returns list of hull reports for all ships found.
    """
    reports = []
    session_dir = session_dir.rstrip("/")
    session_id = os.path.basename(session_dir)

    # Flagship JSONL is the sibling file
    flagship_path = session_dir + ".jsonl"
    if os.path.isfile(flagship_path):
        token_count = count_tokens_from_jsonl(flagship_path)
        if token_count is not None:
            reports.append(build_report("Flagship", token_count, token_limit))

    # Subagent JSONLs
    subagents_dir = os.path.join(session_dir, "subagents")
    if os.path.isdir(subagents_dir):
        for jsonl_path in sorted(glob.glob(os.path.join(subagents_dir, "agent-*.jsonl"))):
            filename = os.path.basename(jsonl_path)
            agent_id = filename.replace("agent-", "").replace(".jsonl", "")
            token_count = count_tokens_from_jsonl(jsonl_path)
            if token_count is not None:
                reports.append(build_report(f"agent-{agent_id}", token_count, token_limit))

    return reports


def readiness_board(reports: list[dict]) -> str:
    """Format a compact readiness board summary string.

    Example: 'Flagship (Green 82%) | agent-abc (Amber 65%) | agent-xyz (Critical 22%)'
    """
    if not reports:
        return "No ships reporting."

    STATUS_ICONS = {
        "Green": "🟢",
        "Amber": "🟡",
        "Red": "🔴",
        "Critical": "💀",
    }

    parts = []
    for r in reports:
        icon = STATUS_ICONS.get(r["hull_integrity_status"], "❓")
        parts.append(
            f"{icon} {r['agent_name']} ({r['hull_integrity_status']} {r['hull_integrity_pct']}%)"
        )
    return " | ".join(parts)


def check_session(
    session_path: str,
    agent_name: str = "Agent",
    token_limit: int = DEFAULT_TOKEN_LIMIT,
) -> Optional[dict]:
    """Convenience: check a single JSONL file. Returns report or None."""
    token_count = count_tokens_from_jsonl(session_path)
    if token_count is None:
        return None
    return build_report(agent_name, token_count, token_limit)
