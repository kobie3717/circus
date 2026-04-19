#!/usr/bin/env python3
"""Circus Memory Commons — Week 4 Preference Demo.

Dead-simple demonstration of the product moment:
One agent learns a user preference → every agent serving that owner immediately behaves differently.

Run this script, see the magic in 10 seconds.

Usage:
    ./scripts/demo_preference_flow.py
    # or
    python scripts/demo_preference_flow.py
"""

import os
import sys
import tempfile
from pathlib import Path
from contextlib import contextmanager, redirect_stdout, redirect_stderr

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from circus.database import init_database, get_db
from circus.config import settings
from circus.services.preference_application import get_active_preferences
from fastapi.testclient import TestClient


# ── Bot Response Simulator ──

def bot_response(conn, owner_id: str, user_message: str) -> dict:
    """Simulate a bot consulting preferences and producing observable output.

    This is what every Circus-aware bot does on each turn:
    1. Read active_preferences for the owner
    2. Adjust response construction based on preferences

    In production this would call a real LLM/translator. Here we use canned responses
    to make the behavior delta visually obvious.
    """
    prefs = get_active_preferences(conn, owner_id)

    language = prefs.get("user.language_preference", "en")
    verbosity = prefs.get("user.response_verbosity", "normal")

    # Canned responses — visually different outputs for each preference combo
    if language == "af" and verbosity == "terse":
        text = "Ja, reg so."
    elif language == "af":
        text = "Ja, dit is reg — hier is die antwoord."
    elif verbosity == "terse":
        text = "Yes, done."
    else:
        text = "Yes, here's the answer you requested."

    return {
        "text": text,
        "language": language,
        "verbosity": verbosity,
    }


# ── Quiet Context Manager ──

@contextmanager
def suppress_output():
    """Suppress all stdout/stderr noise during API calls."""
    with open(os.devnull, 'w') as devnull:
        with redirect_stdout(devnull), redirect_stderr(devnull):
            yield


# ── Demo Runner ──

def run_demo():
    """Run the full BEFORE → PUBLISH → AFTER demo flow."""

    # Suppress all noise: telemetry, logging, warnings
    import logging
    import warnings
    logging.disable(logging.CRITICAL)
    warnings.filterwarnings("ignore")
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

    # Setup: temp database
    temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(temp_db.name)
    temp_db.close()

    original_path = settings.database_path
    settings.database_path = db_path
    init_database(db_path)

    # Set owner for this demo
    os.environ["CIRCUS_OWNER_ID"] = "kobus"

    # Import app AFTER setting env vars
    from circus.app import app
    client = TestClient(app, raise_server_exceptions=False)

    # Register a test agent
    passport = {
        "identity": {"name": "demo-agent", "role": "demo"},
        "capabilities": ["memory", "preference"],
        "predictions": {"confirmed": 5, "refuted": 1},
        "beliefs": {"total": 10, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 2.0, "graph_connections": 10},
        "score": {"total": 7.5},
        "graph_summary": {"entities": []},
        "traits": {}
    }

    register_payload = {
        "name": "demo-agent",
        "role": "demo",
        "capabilities": ["memory", "preference"],
        "home": "http://demo.local",
        "passport": passport
    }

    with suppress_output():
        resp = client.post("/api/v1/agents/register", json=register_payload)
        if resp.status_code != 201:
            print(f"\nERROR: Agent registration failed: {resp.json()}")
            return 1

        token = resp.json()["ring_token"]
        client.headers = {"Authorization": f"Bearer {token}"}

    # ── Print Demo Header ──
    print()
    print("╔" + "═" * 68 + "╗")
    print("║" + " " * 68 + "║")
    print("║" + "   Circus Memory Commons — Week 4 Preference Demo".center(68) + "║")
    print("║" + " " * 68 + "║")
    print("║" + "   One agent learns, every agent serving that owner".center(68) + "║")
    print("║" + "   immediately behaves differently.".center(68) + "║")
    print("║" + " " * 68 + "║")
    print("╚" + "═" * 68 + "╝")
    print()

    # ── BEFORE: Default bot behavior ──
    print("── BEFORE: Claw responds to Kobus (no preferences set) ──")
    print()

    with get_db() as conn:
        before = bot_response(conn, owner_id="kobus", user_message="Help me please")

    print(f"    Response: \"{before['text']}\"")
    print(f"    Language: {before['language']} · Verbosity: {before['verbosity']}")
    print()

    # ── TRIGGER: Publish preferences ──
    print("── TRIGGER: Friday publishes preferences for Kobus ──")
    print()

    # Publish language preference
    pref1 = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "Kobus prefers Afrikaans for bot responses",
        "confidence": 0.85,
        "provenance": {
            "owner_id": "kobus",
            "reasoning": "User explicitly requested Afrikaans in multiple sessions"
        },
        "preference": {
            "field": "user.language_preference",
            "value": "af"
        },
    }

    with suppress_output():
        resp = client.post("/api/v1/memory-commons/publish", json=pref1)
        if resp.status_code != 200:
            print(f"    ERROR: Language preference publish failed: {resp.json()}")
            return 1

    print(f"    ✓ user.language_preference = af      (confidence 0.85)")

    # Publish verbosity preference
    pref2 = {
        "category": "user_preference",
        "domain": "preference.user",
        "content": "Kobus prefers terse responses",
        "confidence": 0.9,
        "provenance": {
            "owner_id": "kobus",
            "reasoning": "User frequently says 'just tell me' and 'short answer please'"
        },
        "preference": {
            "field": "user.response_verbosity",
            "value": "terse"
        },
    }

    with suppress_output():
        resp = client.post("/api/v1/memory-commons/publish", json=pref2)
        if resp.status_code != 200:
            print(f"    ERROR: Verbosity preference publish failed: {resp.json()}")
            return 1

    print(f"    ✓ user.response_verbosity = terse   (confidence 0.9)")
    print()

    # ── AFTER: Fresh connection read shows new behavior ──
    print("── AFTER: Claw's next turn, fresh connection read ──")
    print()

    with get_db() as fresh_conn:
        after = bot_response(fresh_conn, owner_id="kobus", user_message="Help me please")

    print(f"    Response: \"{after['text']}\"")
    print(f"    Language: {after['language']} · Verbosity: {after['verbosity']}")
    print()

    # ── Verification ──
    if before['text'] != after['text'] and before['language'] != after['language']:
        print("✓ Behavior changed. No bot restart. No code change. Just memory.")
        print()
        cleanup(db_path, original_path)
        return 0
    else:
        print("✗ ERROR: Behavior did NOT change. Something is broken.")
        print(f"  Before: {before}")
        print(f"  After:  {after}")
        print()
        cleanup(db_path, original_path)
        return 1


def cleanup(db_path: Path, original_db_path: Path):
    """Clean up temp database."""
    settings.database_path = original_db_path
    try:
        db_path.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    exit_code = run_demo()
    sys.exit(exit_code)
