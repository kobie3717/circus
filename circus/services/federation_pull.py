"""Federation PULL endpoint service — bundle emission and query logic.

This module centralizes ALL bundle construction for outgoing federation traffic.
Both PULL (3.5a) and PUSH (3.5b) will use build_outgoing_bundle() to ensure
consistent signing and envelope format across all federation transport.

Key responsibilities:
- Query shared_memories with privacy/domain/boomerang filters
- Cursor-based pagination (opaque base64 cursors)
- Bundle envelope construction with deterministic bundle_id
- Ed25519 signature over canonical bytes
- Instance passport generation + 1-hour caching
- Boomerang suppression (don't echo memories back to their origin)

NOT responsible for:
- Authentication (handled by federation_auth)
- Route logic (handled by routes/federation.py)
- Merge semantics (handled by federation_admission, Sub-step 3.6)
"""

import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.instance_identity import get_instance_identity
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class CursorError(ValueError):
    """Raised when cursor is malformed or missing required fields."""
    pass


def encode_cursor(shared_at: str, memory_id: str) -> str:
    """Build opaque cursor from shared_at timestamp + memory_id.

    Args:
        shared_at: ISO8601 timestamp from shared_memories.shared_at
        memory_id: Memory ID from shared_memories.id

    Returns:
        Base64-encoded opaque cursor string
    """
    cursor_dict = {"shared_at": shared_at, "id": memory_id}
    cursor_json = json.dumps(cursor_dict, sort_keys=True, separators=(',', ':'))
    return base64.urlsafe_b64encode(cursor_json.encode('utf-8')).decode('ascii')


def decode_cursor(cursor_b64: str) -> tuple[str, str]:
    """Parse opaque cursor back to (shared_at, memory_id).

    Args:
        cursor_b64: Base64-encoded cursor string

    Returns:
        Tuple of (shared_at, memory_id)

    Raises:
        CursorError: If cursor is malformed or missing required fields
    """
    try:
        cursor_json = base64.urlsafe_b64decode(cursor_b64).decode('utf-8')
        cursor_dict = json.loads(cursor_json)

        # Validate required fields
        if "shared_at" not in cursor_dict or "id" not in cursor_dict:
            raise CursorError("Cursor missing required fields (shared_at or id)")

        return cursor_dict["shared_at"], cursor_dict["id"]
    except (ValueError, KeyError) as exc:
        raise CursorError(f"Invalid cursor format: {exc}")


def get_cached_passport(conn: sqlite3.Connection) -> dict:
    """Get instance passport from cache, regenerate if expired.

    Passport TTL: 1 hour. Caches both the passport JSON and expiry timestamp
    in instance_config table.

    Args:
        conn: Database connection (caller owns commit)

    Returns:
        Passport dict (AI-IQ format)
    """
    cursor = conn.cursor()

    # Check cache
    cursor.execute("""
        SELECT key, value FROM instance_config
        WHERE key IN ('passport_cache', 'passport_expires_at')
    """)
    rows = cursor.fetchall()
    config = {row["key"]: row["value"] for row in rows}

    # Validate cache
    if "passport_cache" in config and "passport_expires_at" in config:
        try:
            expires_at = datetime.fromisoformat(config["passport_expires_at"])
            if datetime.utcnow() < expires_at:
                # Cache still valid
                return json.loads(config["passport_cache"])
        except (ValueError, json.JSONDecodeError):
            # Malformed cache — regenerate
            pass

    # Cache miss or expired — regenerate passport
    cursor.execute("SELECT value FROM instance_config WHERE key = 'instance_id'")
    row = cursor.fetchone()
    if not row:
        # Should never happen — instance_id seeded in v5 migration
        raise RuntimeError("instance_id missing from instance_config")

    instance_id = row["value"]

    # Compute trust score from public memory count
    cursor.execute("SELECT COUNT(*) FROM shared_memories WHERE privacy_tier = 'public'")
    memory_count = cursor.fetchone()[0]

    # Simple heuristic: base 5.0 + 0.1 per 10 memories, capped at 10.0
    score = min(5.0 + (memory_count / 10) * 0.1, 10.0)

    now = datetime.utcnow()
    passport = {
        "identity": {
            "name": instance_id,
            "role": "circus_node",
        },
        "score": {"total": score},
        "generated_at": now.isoformat(),
        "predictions": {"confirmed": 0, "refuted": 0},
        "beliefs": {"total": 0, "contradictions": 0},
        "memory_stats": {"proof_count_avg": 0.0, "graph_connections": 0},
    }

    # Cache for 1 hour
    expires_at = (now + timedelta(hours=1)).isoformat()
    passport_json = json.dumps(passport, sort_keys=True)

    # Write cache (caller commits)
    cursor.execute("""
        INSERT OR REPLACE INTO instance_config (key, value, updated_at)
        VALUES ('passport_cache', ?, ?)
    """, (passport_json, now.isoformat()))

    cursor.execute("""
        INSERT OR REPLACE INTO instance_config (key, value, updated_at)
        VALUES ('passport_expires_at', ?, ?)
    """, (expires_at, now.isoformat()))

    return passport


def serialize_memory_for_bundle(memory_row: dict) -> dict:
    """Convert shared_memories row to bundle memory format.

    Args:
        memory_row: Dict from shared_memories table

    Returns:
        Memory dict ready for bundle.memories[] array
    """
    # Parse JSON columns
    tags = json.loads(memory_row["tags"]) if memory_row.get("tags") else []
    provenance = json.loads(memory_row["provenance"]) if memory_row.get("provenance") else {}

    return {
        "id": memory_row["id"],
        "content": memory_row["content"],
        "category": memory_row["category"],
        "domain": memory_row.get("domain"),  # May be NULL in old rows
        "tags": tags,
        "provenance": provenance,
        "privacy_tier": memory_row["privacy_tier"],
        "shared_at": memory_row["shared_at"],
    }


def build_outgoing_bundle(
    conn: sqlite3.Connection,
    memory_row: dict,
    *,
    puller_peer_id: Optional[str] = None,
    now: Optional[datetime] = None
) -> dict:
    """Construct signed bundle envelope for ONE memory.

    This is the canonical bundle emission function. Both PULL (3.5a) and
    PUSH (3.5b) will call this to ensure consistent signing.

    Args:
        conn: Database connection (for identity + passport lookup)
        memory_row: Dict of shared_memories row
        puller_peer_id: Optional peer_id for boomerang check (PULL only)
        now: Optional timestamp for testing (defaults to utcnow)

    Returns:
        Fully signed bundle dict ready for emission
    """
    # Get instance identity (strict read — raises if missing)
    identity = get_instance_identity(conn)

    # Get cached passport
    passport = get_cached_passport(conn)

    # Serialize memory
    memory = serialize_memory_for_bundle(memory_row)

    # Build bundle envelope (no signature yet)
    if now is None:
        now = datetime.utcnow()
    bundle = {
        "peer_id": identity.instance_id,
        "passport": passport,
        "memories": [memory],
        "timestamp": now.isoformat(),
    }

    # Derive deterministic bundle_id (SHA256[:16] of canonical bytes)
    # NOTE: bundle_id is derived BEFORE adding it to the bundle
    canonical_bytes = canonicalize_for_signing(bundle)
    bundle_id = hashlib.sha256(canonical_bytes).hexdigest()[:16]
    bundle["bundle_id"] = bundle_id

    # Sign bundle (now WITH bundle_id included)
    # Re-canonicalize with bundle_id
    canonical_bytes_with_id = canonicalize_for_signing(bundle)
    private_key = Ed25519PrivateKey.from_private_bytes(identity.private_key_bytes)
    signature_bytes = private_key.sign(canonical_bytes_with_id)
    signature_b64 = base64.b64encode(signature_bytes).decode('ascii')
    bundle["signature"] = signature_b64

    return bundle


def pull_bundles(
    conn: sqlite3.Connection,
    *,
    puller_peer_id: str,
    since_cursor: Optional[str],
    limit: int,
    domain: Optional[str] = None
) -> tuple[list[dict], Optional[str], bool]:
    """Query shared_memories and construct signed bundles for PULL response.

    Handles:
    - Privacy filter (public-only)
    - Domain filter (optional)
    - Boomerang suppression (exclude memories authored by puller)
    - Cursor pagination (exclusive, deterministic ordering)

    Args:
        conn: Database connection (caller owns commit)
        puller_peer_id: Peer identifier from X-Peer-Id header
        since_cursor: Opaque cursor for pagination (None for first page)
        limit: Max bundles to return (already clamped to 100 by route)
        domain: Optional domain filter (narrows only)

    Returns:
        Tuple of (bundles, next_cursor, has_more)

    Raises:
        CursorError: If since_cursor is malformed
    """
    cursor = conn.cursor()

    # Parse cursor if present
    cursor_shared_at = None
    cursor_id = None
    if since_cursor:
        cursor_shared_at, cursor_id = decode_cursor(since_cursor)

    # Build query with filters
    query_params = []
    where_clauses = ["privacy_tier = 'public'"]

    # Domain filter (narrows only)
    if domain:
        where_clauses.append("domain = ?")
        query_params.append(domain)

    # Cursor exclusion (exclusive > ordering)
    if cursor_shared_at and cursor_id:
        where_clauses.append(
            "((shared_at > ?) OR (shared_at = ? AND id > ?))"
        )
        query_params.extend([cursor_shared_at, cursor_shared_at, cursor_id])

    # Build query
    query_params.append(limit)  # LIMIT parameter

    query = f"""
        SELECT id, content, category, domain, tags, provenance, privacy_tier, shared_at
        FROM shared_memories
        WHERE {' AND '.join(where_clauses)}
        ORDER BY shared_at ASC, id ASC
        LIMIT ?
    """

    cursor.execute(query, query_params)
    rows = cursor.fetchall()

    # Construct bundles with boomerang suppression
    bundles = []
    skipped_boomerang = 0

    for row in rows:
        memory_dict = dict(row)

        # Boomerang prevention: don't send memory back to its origin
        provenance = json.loads(memory_dict.get("provenance") or "{}")
        original_author = provenance.get("original_author")

        if original_author == puller_peer_id:
            skipped_boomerang += 1
            continue

        # Construct + sign bundle
        bundle = build_outgoing_bundle(conn, memory_dict, puller_peer_id=puller_peer_id)
        bundles.append(bundle)

    # Determine next_cursor and has_more
    next_cursor = None
    has_more = len(rows) == limit  # If we got exactly `limit` rows, there might be more

    if rows:
        # Build cursor from last fetched row (not last emitted bundle!)
        # This ensures pagination works even if all rows were boomeranged
        last_row = rows[-1]
        next_cursor = encode_cursor(last_row["shared_at"], last_row["id"])

    return bundles, next_cursor, has_more
