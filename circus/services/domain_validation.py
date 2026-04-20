"""Domain name validation shared between publish and federation ingest."""

import re

DOMAIN_PATTERN = re.compile(r'^[a-z0-9]+([-.][a-z0-9]+)*$')
# Explanation: lowercase alphanumerics, hyphens and dots only as single separators.
# No leading hyphen/dot, no trailing hyphen/dot, no consecutive separators.
# Length bound (1-50 chars) enforced via Pydantic Field + DOMAIN_MAX_LENGTH check.
DOMAIN_MAX_LENGTH = 50

# W11: Allowed domain families (exact matches or prefixes)
ALLOWED_DOMAIN_FAMILIES = [
    'preference.user',  # User preferences (W4)
    'knowledge.',       # Cross-agent shared knowledge (W11 - prefix match)
]


class InvalidDomainError(ValueError):
    """Raised when a domain name fails validation."""


def validate_domain(domain: str | None) -> str:
    """Validate and normalize a domain name.

    Returns the normalized domain (lowercased, stripped).
    Raises InvalidDomainError if invalid.

    Rules:
    - Must be non-empty, non-whitespace
    - Length 1-50 chars after strip
    - Lowercase a-z, 0-9, hyphen and dot only
    - No leading or trailing hyphen/dot
    - No consecutive separators (enforced by regex ^[a-z0-9]+([-.][a-z0-9]+)*$)
    - Must match an allowed domain family (exact or prefix)
    """
    if domain is None:
        raise InvalidDomainError("domain is required")

    stripped = domain.strip().lower()

    if not stripped:
        raise InvalidDomainError("domain cannot be empty")

    if len(stripped) > DOMAIN_MAX_LENGTH:
        raise InvalidDomainError(f"domain exceeds max length ({DOMAIN_MAX_LENGTH})")

    if not DOMAIN_PATTERN.match(stripped):
        raise InvalidDomainError(
            f"domain '{domain}' invalid — must be lowercase alphanumeric + hyphens/dots, "
            "no leading/trailing separator"
        )

    # W11: Check against allowed domain families
    allowed = False
    for family in ALLOWED_DOMAIN_FAMILIES:
        if family.endswith('.'):
            # Prefix match (e.g., 'knowledge.' matches 'knowledge.whatsauction')
            if stripped.startswith(family):
                allowed = True
                break
        else:
            # Exact match (e.g., 'preference.user')
            if stripped == family:
                allowed = True
                break

    if not allowed:
        raise InvalidDomainError(
            f"domain '{domain}' not in allowed families: {', '.join(ALLOWED_DOMAIN_FAMILIES)}"
        )

    return stripped
