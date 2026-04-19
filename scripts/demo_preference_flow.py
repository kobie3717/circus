#!/usr/bin/env python3
"""Circus Memory Commons — Week 5 Preference Demo.

Dead-simple demonstration of the product moment:
One agent learns a user preference → every agent serving that owner immediately behaves differently.

W5 UPDATE: Now with signed owner bindings — cryptographic proof that the publishing
agent is authorized to act on behalf of the claimed owner.

Run this script, see the magic in 10 seconds.

Usage:
    ./scripts/demo_preference_flow.py
    # or
    python scripts/demo_preference_flow.py
"""

import os
import sys
import tempfile
import base64
import json
from pathlib import Path
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from unittest.mock import patch

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from circus.database import init_database, get_db
from circus.config import settings
from circus.services.preference_application import get_active_preferences
from circus.services.signing import generate_keypair, encode_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
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


# ── Owner Signature Helper ──

def sign_owner_binding(
    owner_id: str,
    agent_id: str,
    memory_id: str,
    timestamp: str,
    private_key_bytes: bytes
) -> str:
    """Sign an owner binding payload with the owner's private key.

    Replicates the canonical signing used in owner_verification.py.

    Args:
        owner_id: Owner identifier
        agent_id: Publishing agent identifier
        memory_id: Memory ID being signed
        timestamp: ISO8601 timestamp
        private_key_bytes: Raw 32-byte Ed25519 private key

    Returns:
        Base64-encoded signature
    """
    # Construct canonical payload (same 4 fields as verification, sorted keys)
    payload = {
        "agent_id": agent_id,
        "memory_id": memory_id,
        "owner_id": owner_id,
        "timestamp": timestamp,
    }

    # Canonicalize (same as bundle_signing.canonicalize_for_signing)
    canonical_json = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    message = canonical_json.encode('utf-8')

    # Sign with owner's private key
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    signature = private_key.sign(message)

    return base64.b64encode(signature).decode('ascii')


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

    # W5 REQUIREMENT: Owner private key MUST be provided via env var
    owner_key_path = os.getenv("CIRCUS_OWNER_PRIVATE_KEY_PATH")
    if not owner_key_path:
        print("\nERROR: CIRCUS_OWNER_PRIVATE_KEY_PATH is not set.")
        print("Run: python -m circus.cli owner-keygen --owner demo-owner --output /tmp/demo-owner.key")
        print("Then: export CIRCUS_OWNER_PRIVATE_KEY_PATH=/tmp/demo-owner.key")
        print("Then: export CIRCUS_OWNER_ID=demo-owner")
        return 1

    # Verify key file exists
    key_file = Path(owner_key_path)
    if not key_file.exists():
        print(f"\nERROR: CIRCUS_OWNER_PRIVATE_KEY_PATH is set but file does not exist: {owner_key_path}")
        print("Run: python -m circus.cli owner-keygen --owner demo-owner --output /tmp/demo-owner.key")
        return 1

    # Set owner for this demo
    owner_id = os.getenv("CIRCUS_OWNER_ID", "kobus")
    os.environ["CIRCUS_OWNER_ID"] = owner_id

    # Read private key from file
    private_b64 = key_file.read_text().strip()
    owner_private_bytes = base64.b64decode(private_b64)

    # Derive public key from private key
    private_key = Ed25519PrivateKey.from_private_bytes(owner_private_bytes)
    owner_public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    owner_public_b64 = encode_public_key(owner_public_bytes)

    # Insert owner public key into owner_keys table (for this demo session)
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO owner_keys (owner_id, public_key, created_at, description)
            VALUES (?, ?, ?, ?)
        """, (owner_id, owner_public_b64, now, "Demo owner key"))
        conn.commit()

    # Import app AFTER setting env vars
    from circus.app import app
    client = TestClient(app, raise_server_exceptions=True)

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

    # Get agent ID from registration response for owner binding
    agent_id = "demo-agent"  # Will be set from registration

    # ── Print Demo Header ──
    print()
    print("╔" + "═" * 68 + "╗")
    print("║" + " " * 68 + "║")
    print("║" + "   Circus Memory Commons — Week 5 Preference Demo".center(68) + "║")
    print("║" + " " * 68 + "║")
    print("║" + "   One agent learns, every agent serving that owner".center(68) + "║")
    print("║" + "   immediately behaves differently.".center(68) + "║")
    print("║" + "   (Now with cryptographic owner signatures)".center(68) + "║")
    print("║" + " " * 68 + "║")
    print("╚" + "═" * 68 + "╝")
    print()

    # ── BEFORE: Default bot behavior ──
    print(f"── BEFORE: Claw responds to {owner_id} (no preferences set) ──")
    print()

    with get_db() as conn:
        before = bot_response(conn, owner_id=owner_id, user_message="Help me please")

    print(f"    Response: \"{before['text']}\"")
    print(f"    Language: {before['language']} · Verbosity: {before['verbosity']}")
    print()

    # ── TRIGGER: Publish preferences ──
    print(f"── TRIGGER: Friday publishes preferences for {owner_id} (with signatures) ──")
    print()

    # Helper to publish a signed preference
    def publish_signed_preference(field: str, value: str, content: str, confidence: float, reasoning: str, token_suffix: str):
        # Mock secrets.token_hex to make memory_id deterministic
        # This allows us to sign the owner_binding with the correct memory_id before publishing
        expected_memory_id = f"shmem-{token_suffix}"
        # Use timezone-aware UTC timestamp (fixed in 5.4.1 hotfix)
        timestamp = datetime.now(timezone.utc).isoformat()

        # Sign the owner binding with the expected memory_id
        signature = sign_owner_binding(
            owner_id=owner_id,
            agent_id=agent_id,
            memory_id=expected_memory_id,
            timestamp=timestamp,
            private_key_bytes=owner_private_bytes
        )

        # Construct preference payload with signed owner_binding
        pref = {
            "category": "user_preference",
            "domain": "preference.user",
            "content": content,
            "confidence": confidence,
            "provenance": {
                "owner_id": owner_id,
                "owner_binding": {
                    "agent_id": agent_id,
                    "memory_id": expected_memory_id,
                    "timestamp": timestamp,
                    "signature": signature
                },
                "reasoning": reasoning
            },
            "preference": {
                "field": field,
                "value": value
            },
        }

        # Mock secrets.token_hex to return deterministic memory_id
        with patch('secrets.token_hex', return_value=token_suffix):
            resp = client.post("/api/v1/memory-commons/publish", json=pref)
            if resp.status_code != 200:
                print(f"    ERROR: {field} publish failed (status {resp.status_code})")
                print(f"    Response: {resp.text}")
                # Try to get error detail
                try:
                    error_detail = resp.json()
                    print(f"    Detail: {error_detail}")
                except:
                    pass
                return False
        return True

    # Publish language preference
    if not publish_signed_preference(
        field="user.language_preference",
        value="af",
        content=f"{owner_id} prefers Afrikaans for bot responses",
        confidence=0.85,
        reasoning="User explicitly requested Afrikaans in multiple sessions",
        token_suffix="demolangpref001"
    ):
        return 1

    print(f"    ✓ user.language_preference = af      (confidence 0.85, signed)")

    # Publish verbosity preference
    if not publish_signed_preference(
        field="user.response_verbosity",
        value="terse",
        content=f"{owner_id} prefers terse responses",
        confidence=0.9,
        reasoning="User frequently says 'just tell me' and 'short answer please'",
        token_suffix="demoverbpref002"
    ):
        return 1

    print(f"    ✓ user.response_verbosity = terse   (confidence 0.9, signed)")
    print()

    # ── AFTER: Fresh connection read shows new behavior ──
    print("── AFTER: Claw's next turn, fresh connection read ──")
    print()

    with get_db() as fresh_conn:
        after = bot_response(fresh_conn, owner_id=owner_id, user_message="Help me please")

    print(f"    Response: \"{after['text']}\"")
    print(f"    Language: {after['language']} · Verbosity: {after['verbosity']}")
    print()

    # ── Verification ──
    if before['text'] != after['text'] and before['language'] != after['language']:
        print("✓ Behavior changed. No bot restart. No code change. Just memory.")
        print("  (With cryptographic proof of owner authorization)")
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
