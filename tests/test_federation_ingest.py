"""Tests for federation ingest validation (Step 2 — domain validation only)."""

import pytest
from circus.services.federation_ingest import validate_federated_memory
from circus.services.domain_validation import InvalidDomainError


class TestValidateFederatedMemory:
    """Test federated memory domain validation."""

    def test_valid_domain(self):
        """Valid domain should return normalized payload."""
        payload = {"domain": "payment-flows", "content": "test"}
        result = validate_federated_memory(payload)
        assert result["domain"] == "payment-flows"
        assert result["content"] == "test"

    def test_uppercase_domain(self):
        """Uppercase domain should be normalized to lowercase."""
        payload = {"domain": "PAYMENT-FLOWS", "content": "test"}
        result = validate_federated_memory(payload)
        assert result["domain"] == "payment-flows"

    def test_whitespace_domain(self):
        """Domain with whitespace should be normalized."""
        payload = {"domain": " payment-flows ", "content": "test"}
        result = validate_federated_memory(payload)
        assert result["domain"] == "payment-flows"

    def test_missing_domain(self):
        """Missing domain should raise InvalidDomainError."""
        payload = {"content": "test"}
        with pytest.raises(InvalidDomainError, match="domain is required"):
            validate_federated_memory(payload)

    def test_none_domain(self):
        """None domain should raise InvalidDomainError."""
        payload = {"domain": None, "content": "test"}
        with pytest.raises(InvalidDomainError, match="domain is required"):
            validate_federated_memory(payload)

    def test_empty_domain(self):
        """Empty domain should raise InvalidDomainError."""
        payload = {"domain": "", "content": "test"}
        with pytest.raises(InvalidDomainError, match="domain cannot be empty"):
            validate_federated_memory(payload)

    def test_invalid_characters(self):
        """Invalid characters should raise InvalidDomainError."""
        payload = {"domain": "payment_flows", "content": "test"}
        with pytest.raises(InvalidDomainError, match="invalid"):
            validate_federated_memory(payload)

    def test_leading_hyphen(self):
        """Leading hyphen should raise InvalidDomainError."""
        payload = {"domain": "-payment-flows", "content": "test"}
        with pytest.raises(InvalidDomainError, match="no leading/trailing separator"):
            validate_federated_memory(payload)

    def test_trailing_hyphen(self):
        """Trailing hyphen should raise InvalidDomainError."""
        payload = {"domain": "payment-flows-", "content": "test"}
        with pytest.raises(InvalidDomainError, match="no leading/trailing separator"):
            validate_federated_memory(payload)

    def test_preserves_other_fields(self):
        """Should preserve all other fields in payload."""
        payload = {
            "domain": "payment-flows",
            "content": "test content",
            "category": "architecture",
            "tags": ["payfast", "webhooks"],
            "provenance": {
                "hop_count": 2,
                "original_author": "agent-123"
            }
        }
        result = validate_federated_memory(payload)
        assert result["domain"] == "payment-flows"
        assert result["content"] == "test content"
        assert result["category"] == "architecture"
        assert result["tags"] == ["payfast", "webhooks"]
        assert result["provenance"]["hop_count"] == 2


class TestParityWithLocalPublish:
    """Test that federation validation matches local publish validation."""

    def test_same_domain_valid(self):
        """Same valid domain should work for both paths."""
        from circus.services.domain_validation import validate_domain

        domain = "user-preferences"

        # Local publish path
        local_result = validate_domain(domain)

        # Federation path
        fed_result = validate_federated_memory({"domain": domain})

        assert local_result == fed_result["domain"]

    def test_same_domain_normalization(self):
        """Same normalization should apply to both paths."""
        from circus.services.domain_validation import validate_domain

        domain = " User-Preferences "

        # Local publish path
        local_result = validate_domain(domain)

        # Federation path
        fed_result = validate_federated_memory({"domain": domain})

        assert local_result == fed_result["domain"]
        assert local_result == "user-preferences"

    def test_same_domain_invalid(self):
        """Same invalid domain should fail for both paths."""
        from circus.services.domain_validation import validate_domain

        domain = "INVALID DOMAIN"

        # Local publish path should fail
        with pytest.raises(InvalidDomainError):
            validate_domain(domain)

        # Federation path should fail the same way
        with pytest.raises(InvalidDomainError):
            validate_federated_memory({"domain": domain})

    def test_same_domain_none(self):
        """None domain should fail the same way for both paths."""
        from circus.services.domain_validation import validate_domain

        # Local publish path should fail
        with pytest.raises(InvalidDomainError, match="domain is required"):
            validate_domain(None)

        # Federation path should fail the same way
        with pytest.raises(InvalidDomainError, match="domain is required"):
            validate_federated_memory({"domain": None})
