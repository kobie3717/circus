"""Tests for domain validation shared between publish and federation."""

import pytest
from circus.services.domain_validation import validate_domain, InvalidDomainError


class TestValidDomains:
    """Test valid domain name patterns."""

    def test_simple_lowercase(self):
        """Single lowercase letter should be valid."""
        assert validate_domain("a") == "a"

    def test_alphanumeric(self):
        """Lowercase alphanumeric should be valid."""
        assert validate_domain("a1b2") == "a1b2"

    def test_with_hyphens(self):
        """Hyphens in the middle should be valid."""
        assert validate_domain("user-preferences") == "user-preferences"

    def test_multiple_hyphens(self):
        """Multiple hyphens as separators should be valid."""
        assert validate_domain("my-test-domain") == "my-test-domain"

    def test_single_hyphen_separator(self):
        """Single hyphen between alphanumeric runs should be valid."""
        assert validate_domain("a-b") == "a-b"

    def test_max_length(self):
        """50-character domain should be valid."""
        domain = "a" * 50
        assert validate_domain(domain) == domain

    def test_normalization_uppercase(self):
        """Uppercase should be normalized to lowercase."""
        assert validate_domain("USER-PREFERENCES") == "user-preferences"

    def test_normalization_mixed_case(self):
        """Mixed case should be normalized."""
        assert validate_domain("User-Preferences") == "user-preferences"

    def test_normalization_whitespace(self):
        """Leading/trailing whitespace should be stripped."""
        assert validate_domain(" user-preferences ") == "user-preferences"

    def test_normalization_tabs(self):
        """Tabs should be stripped."""
        assert validate_domain("\tuser-preferences\t") == "user-preferences"

    def test_starts_with_number(self):
        """Domain starting with number should be valid."""
        assert validate_domain("1user") == "1user"

    def test_ends_with_number(self):
        """Domain ending with number should be valid."""
        assert validate_domain("user1") == "user1"


class TestInvalidDomains:
    """Test invalid domain name patterns."""

    def test_none(self):
        """None should raise InvalidDomainError."""
        with pytest.raises(InvalidDomainError, match="domain is required"):
            validate_domain(None)

    def test_empty_string(self):
        """Empty string should raise InvalidDomainError."""
        with pytest.raises(InvalidDomainError, match="domain cannot be empty"):
            validate_domain("")

    def test_whitespace_only(self):
        """Whitespace-only should raise InvalidDomainError."""
        with pytest.raises(InvalidDomainError, match="domain cannot be empty"):
            validate_domain("   ")

    def test_tabs_only(self):
        """Tabs-only should raise InvalidDomainError."""
        with pytest.raises(InvalidDomainError, match="domain cannot be empty"):
            validate_domain("\t\t")

    def test_exceeds_max_length(self):
        """51-character domain should raise InvalidDomainError."""
        domain = "a" * 51
        with pytest.raises(InvalidDomainError, match="exceeds max length"):
            validate_domain(domain)

    def test_very_long(self):
        """100-character domain should raise InvalidDomainError."""
        domain = "x" * 100
        with pytest.raises(InvalidDomainError, match="exceeds max length"):
            validate_domain(domain)

    def test_with_space(self):
        """Space should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid.*lowercase alphanumeric"):
            validate_domain("with space")

    def test_with_underscore(self):
        """Underscore should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid.*lowercase alphanumeric"):
            validate_domain("with_underscore")

    def test_with_dot(self):
        """Dot should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid.*lowercase alphanumeric"):
            validate_domain("with.dot")

    def test_leading_hyphen(self):
        """Leading hyphen should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid.*no leading/trailing hyphen"):
            validate_domain("-leading")

    def test_trailing_hyphen(self):
        """Trailing hyphen should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid.*no leading/trailing hyphen"):
            validate_domain("trailing-")

    def test_consecutive_hyphens(self):
        """Consecutive hyphens should be invalid (enforces canonical form)."""
        with pytest.raises(InvalidDomainError, match="invalid"):
            validate_domain("a--b")

    def test_triple_hyphens(self):
        """Three consecutive hyphens should also be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid"):
            validate_domain("foo---bar")

    def test_both_hyphens(self):
        """Both leading and trailing hyphens should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid.*no leading/trailing hyphen"):
            validate_domain("-both-")

    def test_special_chars(self):
        """Special characters should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid"):
            validate_domain("special!char")

    def test_at_sign(self):
        """@ should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid"):
            validate_domain("user@domain")

    def test_slash(self):
        """/ should be invalid."""
        with pytest.raises(InvalidDomainError, match="invalid"):
            validate_domain("user/domain")


class TestErrorType:
    """Test that InvalidDomainError is a ValueError subclass."""

    def test_is_value_error(self):
        """InvalidDomainError should be a ValueError subclass."""
        assert issubclass(InvalidDomainError, ValueError)

    def test_can_catch_as_value_error(self):
        """Should be catchable as ValueError."""
        with pytest.raises(ValueError):
            validate_domain(None)
