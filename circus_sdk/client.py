"""Circus SDK HTTP client."""

import asyncio
from typing import Optional

import httpx

from circus_sdk.models import Preference, OwnerKey, AuditEvent
from circus_sdk.signing import sign_owner_binding


class CircusClient:
    """Async HTTP client for The Circus API."""

    def __init__(self, base_url: str, ring_token: Optional[str] = None):
        """Initialize Circus client.

        Args:
            base_url: Circus API base URL (e.g., "http://localhost:6200")
            ring_token: Authentication token (optional, required for authenticated endpoints)
        """
        self.base_url = base_url.rstrip("/")
        self.ring_token = ring_token
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Async context manager entry."""
        headers = {}
        if self.ring_token:
            headers["Authorization"] = f"Bearer {self.ring_token}"

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=30.0,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()

    async def register(self, name: str, role: str, capabilities: list[str]) -> dict:
        """Register agent and get ring token.

        Args:
            name: Agent name
            role: Agent role (e.g., "assistant", "worker")
            capabilities: List of capability strings

        Returns:
            Registration response with ring_token
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with CircusClient(...) as client'")

        response = await self._client.post(
            "/api/v1/agents/register",
            json={"name": name, "role": role, "capabilities": capabilities}
        )
        response.raise_for_status()
        return response.json()

    async def publish_preference(
        self,
        owner_id: str,
        field: str,
        value: str,
        confidence: float,
        private_key_b64: str,
        room_slug: str = "#preferences",
    ) -> dict:
        """Sign and publish a preference memory.

        Args:
            owner_id: Owner identifier (must match server's CIRCUS_OWNER_ID)
            field: Preference field name
            value: Preference value
            confidence: Confidence score (0.0-1.0)
            private_key_b64: Base64-encoded Ed25519 private key
            room_slug: Target room (default: #preferences)

        Returns:
            Decision trace showing admission result
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with CircusClient(...) as client'")

        from datetime import datetime
        import secrets

        memory_id = f"mem-{secrets.token_hex(8)}"
        timestamp = datetime.utcnow().isoformat() + "Z"

        # Get agent ID from token
        # In real usage, this would be extracted from the ring_token
        agent_id = "sdk-agent"  # Placeholder

        # Sign owner binding
        signature = sign_owner_binding(
            owner_id=owner_id,
            agent_id=agent_id,
            memory_id=memory_id,
            timestamp=timestamp,
            private_key_b64=private_key_b64,
        )

        # Build memory content
        content = {
            "text": f"Set {field} to {value}",
            "provenance": {
                "owner_id": owner_id,
                "preference_field": field,
                "preference_value": value,
                "owner_binding": {
                    "agent_id": agent_id,
                    "memory_id": memory_id,
                    "timestamp": timestamp,
                    "signature": signature,
                }
            }
        }

        response = await self._client.post(
            f"/api/v1/rooms/{room_slug}/share",
            json={
                "content": content,
                "category": "preference",
            }
        )
        response.raise_for_status()
        return response.json()

    async def get_preferences(self, owner_id: str) -> list[Preference]:
        """Get active preferences for owner.

        Args:
            owner_id: Owner identifier

        Returns:
            List of active preferences
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with CircusClient(...) as client'")

        response = await self._client.get(f"/api/v1/preferences/{owner_id}")
        response.raise_for_status()

        data = response.json()
        return [
            Preference(
                owner_id=p["owner_id"],
                field_name=p["field_name"],
                value=p["value"],
                effective_confidence=p["effective_confidence"],
                updated_at=p["updated_at"],
                source_memory_id=p.get("source_memory_id"),
                conflict_count=p.get("conflict_count", 0),
            )
            for p in data.get("preferences", [])
        ]

    async def get_allowlist(self) -> list[dict]:
        """Get preference field allowlist.

        Returns:
            List of allowed field definitions
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with CircusClient(...) as client'")

        response = await self._client.get("/api/v1/preferences/allowlist")
        response.raise_for_status()
        return response.json().get("fields", [])

    async def get_pubkey(self, owner_id: str) -> Optional[str]:
        """Discover owner's public key.

        Args:
            owner_id: Owner identifier

        Returns:
            Base64-encoded public key, or None if not found
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with CircusClient(...) as client'")

        response = await self._client.get(f"/api/v1/keys/discover/{owner_id}")

        if response.status_code == 404:
            return None

        response.raise_for_status()
        data = response.json()
        return data.get("public_key")

    async def rotate_key(
        self,
        owner_id: str,
        new_public_key_b64: str,
        reason: str = "key_rotation",
    ) -> dict:
        """Rotate owner's public key.

        Args:
            owner_id: Owner identifier
            new_public_key_b64: New public key (base64)
            reason: Rotation reason

        Returns:
            Rotation result
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with CircusClient(...) as client'")

        response = await self._client.post(
            f"/api/v1/keys/rotate/{owner_id}",
            json={"new_public_key": new_public_key_b64, "reason": reason}
        )
        response.raise_for_status()
        return response.json()

    async def get_federation_metrics(self) -> dict:
        """Get federation delivery stats.

        Returns:
            Federation metrics
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with CircusClient(...) as client'")

        response = await self._client.get("/api/v1/federation/metrics")
        response.raise_for_status()
        return response.json()

    async def list_quarantine(self, owner_id: Optional[str] = None) -> list[dict]:
        """List quarantined memories.

        Args:
            owner_id: Filter by owner (optional)

        Returns:
            List of quarantine entries
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with CircusClient(...) as client'")

        params = {}
        if owner_id:
            params["owner_id"] = owner_id

        response = await self._client.get("/api/v1/governance/quarantine", params=params)
        response.raise_for_status()
        return response.json().get("quarantined", [])


class CircusClientSync:
    """Synchronous wrapper around CircusClient.

    For use in non-async codebases.
    """

    def __init__(self, base_url: str, ring_token: Optional[str] = None):
        """Initialize sync client."""
        self.base_url = base_url
        self.ring_token = ring_token

    def _run(self, coro):
        """Run async coroutine in sync context."""
        return asyncio.run(coro)

    def register(self, name: str, role: str, capabilities: list[str]) -> dict:
        """Register agent (sync)."""
        async def _impl():
            async with CircusClient(self.base_url, self.ring_token) as client:
                return await client.register(name, role, capabilities)
        return self._run(_impl())

    def get_preferences(self, owner_id: str) -> list[Preference]:
        """Get active preferences (sync)."""
        async def _impl():
            async with CircusClient(self.base_url, self.ring_token) as client:
                return await client.get_preferences(owner_id)
        return self._run(_impl())

    def get_allowlist(self) -> list[dict]:
        """Get field allowlist (sync)."""
        async def _impl():
            async with CircusClient(self.base_url, self.ring_token) as client:
                return await client.get_allowlist()
        return self._run(_impl())

    def get_pubkey(self, owner_id: str) -> Optional[str]:
        """Discover public key (sync)."""
        async def _impl():
            async with CircusClient(self.base_url, self.ring_token) as client:
                return await client.get_pubkey(owner_id)
        return self._run(_impl())

    def publish_preference(
        self,
        owner_id: str,
        field: str,
        value: str,
        confidence: float,
        private_key_b64: str,
        room_slug: str = "#preferences",
    ) -> dict:
        """Publish preference (sync)."""
        async def _impl():
            async with CircusClient(self.base_url, self.ring_token) as client:
                return await client.publish_preference(
                    owner_id, field, value, confidence, private_key_b64, room_slug
                )
        return self._run(_impl())
