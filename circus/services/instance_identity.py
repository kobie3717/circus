"""Instance cryptographic identity lifecycle management.

This module owns the instance's Ed25519 keypair and unique identifier,
providing bootstrap and read access for federation signing operations.

Functions:
    ensure_instance_keypair: Idempotent bootstrap — generate identity on first call
    get_instance_identity: Strict read — raise on missing identity
"""

import base64
import secrets
import sqlite3
from datetime import datetime
from typing import NamedTuple

from circus.services.signing import generate_keypair


class InstanceIdentity(NamedTuple):
    """Instance cryptographic identity bundle."""
    instance_id: str
    private_key_bytes: bytes  # 32-byte Ed25519 raw
    public_key_bytes: bytes   # 32-byte Ed25519 raw


class InstanceIdentityError(Exception):
    """Raised when instance_config is missing keys or corrupted."""
    pass


def ensure_instance_keypair(conn: sqlite3.Connection) -> InstanceIdentity:
    """Ensure instance has an identity; generate on first call.

    Idempotent: on subsequent calls returns the stored identity unchanged.

    Writes to conn but does NOT commit — caller owns the transaction.
    (Matches get_db() commit discipline documented in database.py.)

    Args:
        conn: SQLite connection with instance_config table present

    Returns:
        InstanceIdentity with instance_id and Ed25519 keypair

    Raises:
        InstanceIdentityError if instance_config table is missing or corrupted
        beyond auto-repair (e.g., private_key present but public_key missing).
    """
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    # Step 1: Ensure instance_id exists
    cursor.execute("SELECT value FROM instance_config WHERE key = 'instance_id'")
    row = cursor.fetchone()
    if row is None:
        # Generate new instance_id: circus-<16 hex chars>
        instance_id = f"circus-{secrets.token_hex(8)}"
        cursor.execute(
            "INSERT INTO instance_config (key, value, updated_at) VALUES (?, ?, ?)",
            ('instance_id', instance_id, now)
        )
    else:
        instance_id = row[0]

    # Step 2: Ensure keypair exists (both or neither)
    cursor.execute("""
        SELECT key, value FROM instance_config
        WHERE key IN ('private_key', 'public_key')
    """)
    key_rows = cursor.fetchall()
    existing_keys = {row[0]: row[1] for row in key_rows}

    has_private = 'private_key' in existing_keys
    has_public = 'public_key' in existing_keys

    if has_private and has_public:
        # Both present — decode and return
        private_key_bytes = base64.b64decode(existing_keys['private_key'])
        public_key_bytes = base64.b64decode(existing_keys['public_key'])
        return InstanceIdentity(instance_id, private_key_bytes, public_key_bytes)

    if not has_private and not has_public:
        # Neither present — generate new keypair
        private_key_bytes, public_key_bytes = generate_keypair()

        private_key_b64 = base64.b64encode(private_key_bytes).decode('ascii')
        public_key_b64 = base64.b64encode(public_key_bytes).decode('ascii')

        cursor.execute(
            "INSERT INTO instance_config (key, value, updated_at) VALUES (?, ?, ?)",
            ('private_key', private_key_b64, now)
        )
        cursor.execute(
            "INSERT INTO instance_config (key, value, updated_at) VALUES (?, ?, ?)",
            ('public_key', public_key_b64, now)
        )

        return InstanceIdentity(instance_id, private_key_bytes, public_key_bytes)

    # Exactly one key present — corrupted state
    missing_key = 'public_key' if has_private else 'private_key'
    raise InstanceIdentityError(
        f"Instance config is corrupted: {missing_key} is missing. "
        f"This indicates a partial keypair write or manual deletion. "
        f"Manual intervention required — do NOT auto-regenerate as this would "
        f"invalidate the instance's federation identity."
    )


def get_instance_identity(conn: sqlite3.Connection) -> InstanceIdentity:
    """Load existing identity from instance_config.

    Does NOT generate. Raises InstanceIdentityError if identity is missing
    or incomplete. Use ensure_instance_keypair() for bootstrap-safe access.

    Args:
        conn: SQLite connection with instance_config table present

    Returns:
        InstanceIdentity with instance_id and Ed25519 keypair

    Raises:
        InstanceIdentityError if any required key is missing
    """
    cursor = conn.cursor()

    # Load all three required keys
    cursor.execute("""
        SELECT key, value FROM instance_config
        WHERE key IN ('instance_id', 'private_key', 'public_key')
    """)
    rows = cursor.fetchall()
    config = {row[0]: row[1] for row in rows}

    # Strict validation — all three must be present
    missing = set(['instance_id', 'private_key', 'public_key']) - set(config.keys())
    if missing:
        raise InstanceIdentityError(
            f"Instance identity incomplete: missing keys {sorted(missing)}. "
            f"Use ensure_instance_keypair() to bootstrap."
        )

    # Decode keypair
    private_key_bytes = base64.b64decode(config['private_key'])
    public_key_bytes = base64.b64decode(config['public_key'])

    return InstanceIdentity(
        instance_id=config['instance_id'],
        private_key_bytes=private_key_bytes,
        public_key_bytes=public_key_bytes
    )
