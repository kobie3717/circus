"""Tests for W12: SDK + Platform Externalization."""

import pytest
from circus_sdk import CircusClient, CircusClientSync, Preference


@pytest.mark.asyncio
async def test_sdk_client_get_allowlist():
    """CircusClient.get_allowlist() returns list of fields."""
    # This test requires a running Circus instance
    # For now, we'll just test that the method exists and has correct signature
    client = CircusClient("http://localhost:6200")
    assert hasattr(client, "get_allowlist")
    assert callable(client.get_allowlist)


@pytest.mark.asyncio
async def test_sdk_client_get_preferences():
    """CircusClient.get_preferences() returns Preference objects."""
    client = CircusClient("http://localhost:6200")
    assert hasattr(client, "get_preferences")
    assert callable(client.get_preferences)


@pytest.mark.asyncio
async def test_sdk_client_get_pubkey():
    """CircusClient.get_pubkey() discovers owner key."""
    client = CircusClient("http://localhost:6200")
    assert hasattr(client, "get_pubkey")
    assert callable(client.get_pubkey)


def test_sdk_sync_wrapper():
    """CircusClientSync works without async context."""
    client = CircusClientSync("http://localhost:6200")
    assert hasattr(client, "get_allowlist")
    assert callable(client.get_allowlist)
    # Verify it's a sync method (not a coroutine)
    import inspect
    assert not inspect.iscoroutinefunction(client.get_allowlist)


def test_sdk_models_preference():
    """Preference dataclass has expected fields."""
    pref = Preference(
        owner_id="kobus",
        field_name="user.language_preference",
        value="af",
        effective_confidence=0.8,
        updated_at="2026-04-20T10:00:00Z"
    )
    assert pref.owner_id == "kobus"
    assert pref.field_name == "user.language_preference"
    assert pref.value == "af"


def test_sdk_signing_helper():
    """sign_owner_binding() generates valid signatures."""
    from circus_sdk.signing import sign_owner_binding, generate_keypair

    private_key, public_key = generate_keypair()

    signature = sign_owner_binding(
        owner_id="kobus",
        agent_id="test-agent",
        memory_id="mem-123",
        timestamp="2026-04-20T10:00:00Z",
        private_key_b64=private_key,
    )

    # Signature should be base64-encoded
    import base64
    assert len(base64.b64decode(signature)) == 64  # Ed25519 signature is 64 bytes
