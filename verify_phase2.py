#!/usr/bin/env python3
"""Verification script for Phase 2 upgrades."""

import json
import sys


def test_signing():
    """Test Ed25519 signing functionality."""
    print("1. Testing Ed25519 signing...")

    from circus.services.signing import (
        generate_keypair,
        sign_agent_card,
        verify_signature,
        encode_public_key,
        decode_public_key,
    )

    # Generate keypair
    private_key, public_key = generate_keypair()
    assert len(private_key) == 32, "Private key should be 32 bytes"
    assert len(public_key) == 32, "Public key should be 32 bytes"
    print("   ✓ Keypair generated (32 bytes each)")

    # Sign data
    test_data = {
        "agent_id": "test-agent",
        "capabilities": ["coding", "testing"],
        "role": "developer",
        "registered_at": "2026-04-09T00:00:00"
    }
    signature = sign_agent_card(test_data, private_key)
    assert isinstance(signature, str), "Signature should be base64 string"
    print(f"   ✓ Data signed: {signature[:20]}...")

    # Verify signature
    is_valid = verify_signature(test_data, signature, public_key)
    assert is_valid, "Signature should be valid"
    print("   ✓ Signature verified")

    # Test invalid signature
    invalid_data = test_data.copy()
    invalid_data["role"] = "admin"
    is_invalid = verify_signature(invalid_data, signature, public_key)
    assert not is_invalid, "Tampered data should fail verification"
    print("   ✓ Tampered data rejected")

    # Test encoding
    encoded = encode_public_key(public_key)
    decoded = decode_public_key(encoded)
    assert decoded == public_key, "Encode/decode should round-trip"
    print("   ✓ Public key encoding works")

    print("   ✅ Ed25519 signing: PASS\n")


def test_telemetry():
    """Test OpenTelemetry integration."""
    print("2. Testing OpenTelemetry...")

    from circus.middleware.telemetry import get_current_trace_id
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    # Setup tracer
    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer(__name__)

    # Without span, should return None
    trace_id = get_current_trace_id()
    assert trace_id is None, "No trace ID without active span"
    print("   ✓ No trace ID without span")

    # With span, should return trace ID
    with tracer.start_as_current_span("test-span"):
        trace_id = get_current_trace_id()
        assert trace_id is not None, "Should have trace ID in span"
        assert len(trace_id) == 32, "Trace ID should be 32 hex chars"
        print(f"   ✓ Trace ID generated: {trace_id[:16]}...")

    print("   ✅ OpenTelemetry: PASS\n")


def test_embeddings():
    """Test embeddings service (optional)."""
    print("3. Testing embeddings service...")

    try:
        from circus.services.embeddings import embed_text, embed_agent_profile

        # Test text embedding
        text = "test agent for coding and debugging"
        embedding = embed_text(text)
        assert len(embedding) == 384, "Should be 384-dimensional"
        assert all(isinstance(x, float) for x in embedding), "Should be float array"
        print(f"   ✓ Text embedded: {len(embedding)} dimensions")

        # Test agent profile embedding
        profile_embedding = embed_agent_profile(
            "Alice",
            "developer",
            ["python", "fastapi", "testing"]
        )
        assert len(profile_embedding) == 384, "Profile embedding should be 384-dim"
        print("   ✓ Agent profile embedded")

        # Test similarity (should be high for similar profiles)
        import numpy as np
        profile1 = embed_agent_profile("Bob", "developer", ["python", "coding"])
        profile2 = embed_agent_profile("Charlie", "engineer", ["python", "programming"])

        similarity = float(np.dot(profile1, profile2))
        assert similarity > 0.8, f"Similar profiles should have high similarity (got {similarity})"
        print(f"   ✓ Similarity score: {similarity:.3f}")

        print("   ✅ Embeddings: PASS\n")

    except (ImportError, RuntimeError):
        print("   ⚠️  sentence-transformers not installed (optional)")
        print("   → Install with: pip install sentence-transformers")
        print("   ✅ Embeddings: SKIP (optional)\n")


def test_database_schema():
    """Test database schema updates."""
    print("4. Testing database schema...")

    import tempfile
    from pathlib import Path
    from circus.database import init_database
    import sqlite3

    # Create temp database
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        init_database(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Check agents table has new columns
        cursor.execute("PRAGMA table_info(agents)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "public_key" in columns, "agents table should have public_key column"
        assert "signed_card" in columns, "agents table should have signed_card column"
        print("   ✓ Agents table has signing columns")

        # Check agent_embeddings table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_embeddings'")
        assert cursor.fetchone() is not None, "agent_embeddings table should exist"
        print("   ✓ Agent embeddings table exists")

        # Check embeddings table columns
        cursor.execute("PRAGMA table_info(agent_embeddings)")
        emb_columns = {row[1] for row in cursor.fetchall()}
        assert "agent_id" in emb_columns
        assert "embedding" in emb_columns
        assert "embedding_json" in emb_columns
        print("   ✓ Embeddings table has correct schema")

        conn.close()

    print("   ✅ Database schema: PASS\n")


def main():
    """Run all verification tests."""
    print("=" * 60)
    print("Phase 2 Upgrade Verification")
    print("=" * 60 + "\n")

    try:
        test_signing()
        test_telemetry()
        test_embeddings()
        test_database_schema()

        print("=" * 60)
        print("✅ All Phase 2 upgrades verified successfully!")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n❌ Verification failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
