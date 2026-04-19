"""Tests for publish endpoint domain validation (Step 2.5)."""

import pytest
from circus.database import get_db
from circus.app import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def established_agent(client):
    """Create an Established tier agent (trust_score >= 30)."""
    import secrets
    unique_id = secrets.token_hex(4)

    passport = {
        "identity": {"name": f"Test Agent {unique_id}"},
        "score": 4.0,  # 0-10 scale targets ~50 trust score (Established tier)
        "prediction_accuracy": 0.6,
        "belief_stability": 0.7
    }

    response = client.post("/api/v1/agents/register", json={
        "name": f"Test Agent {unique_id}",
        "role": "tester",
        "capabilities": ["testing"],
        "home": f"http://localhost:{8000 + int(unique_id, 16) % 1000}",
        "passport": passport
    })

    assert response.status_code == 201, f"Registration failed: {response.json()}"
    return response.json()


class TestPublishWithDomain:
    """Test publish endpoint with required domain field."""

    def test_publish_with_valid_domain(self, client, established_agent):
        """Publishing with valid domain should succeed."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": "payment-flows",
                "tags": ["payfast", "webhooks"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert "memory_id" in data
        assert data["memory_id"].startswith("shmem-")

        # Verify domain is stored in DB
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT domain FROM shared_memories WHERE id = ?",
                (data["memory_id"],)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "payment-flows"

    def test_publish_with_uppercase_domain_normalized(self, client, established_agent):
        """Publishing with uppercase domain should normalize to lowercase."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": "PAYMENT-FLOWS",
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        assert response.status_code == 200
        data = response.json()

        # Verify normalized domain in DB
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT domain FROM shared_memories WHERE id = ?",
                (data["memory_id"],)
            )
            row = cursor.fetchone()
            assert row[0] == "payment-flows"

    def test_publish_with_whitespace_domain_normalized(self, client, established_agent):
        """Publishing with whitespace should normalize."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": " payment-flows ",
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        assert response.status_code == 200
        data = response.json()

        # Verify normalized domain in DB
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT domain FROM shared_memories WHERE id = ?",
                (data["memory_id"],)
            )
            row = cursor.fetchone()
            assert row[0] == "payment-flows"


class TestPublishDomainRequired:
    """Test that domain field is required on publish."""

    def test_publish_without_domain_fails(self, client, established_agent):
        """Publishing without domain should fail with 422."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        # Pydantic will catch this as a validation error
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data


class TestPublishDomainValidation:
    """Test domain validation rules on publish."""

    def test_publish_with_invalid_uppercase_fails(self, client, established_agent):
        """Publishing with invalid characters should fail.

        Note: Uppercase is normalized, but spaces/underscores/etc are invalid.
        """
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": "INVALID DOMAIN",
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        assert response.status_code == 400
        data = response.json()
        assert "invalid domain" in data["detail"].lower()

    def test_publish_with_underscore_fails(self, client, established_agent):
        """Publishing with underscore should fail."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": "payment_flows",
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        assert response.status_code == 400
        data = response.json()
        assert "invalid domain" in data["detail"].lower()

    def test_publish_with_leading_hyphen_fails(self, client, established_agent):
        """Publishing with leading hyphen should fail."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": "-payment-flows",
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        assert response.status_code == 400
        data = response.json()
        assert "invalid domain" in data["detail"].lower()

    def test_publish_with_trailing_hyphen_fails(self, client, established_agent):
        """Publishing with trailing hyphen should fail."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": "payment-flows-",
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        assert response.status_code == 400
        data = response.json()
        assert "invalid domain" in data["detail"].lower()

    def test_publish_with_empty_domain_fails(self, client, established_agent):
        """Publishing with empty domain should fail."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": "",
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        # Pydantic min_length catches this as 422, not our validator's 400
        assert response.status_code in (400, 422)
        data = response.json()
        assert "detail" in data

    def test_publish_with_too_long_domain_fails(self, client, established_agent):
        """Publishing with domain exceeding 50 chars should fail."""
        response = client.post("/api/v1/memory-commons/publish",
            headers={"Authorization": f"Bearer {established_agent['ring_token']}"},
            json={
                "content": "PayFast webhooks use IP whitelist 197.242.158.0/24",
                "category": "architecture",
                "domain": "a" * 51,
                "tags": ["payfast"],
                "privacy_tier": "public",
                "confidence": 0.9
            }
        )

        # Pydantic max_length catches this as 422, not our validator's 400
        assert response.status_code in (400, 422)
        data = response.json()
        assert "detail" in data
