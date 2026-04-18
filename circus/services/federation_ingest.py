"""Federation ingest validation (protocol logic lands in Step 3+)."""
from .domain_validation import validate_domain, InvalidDomainError


def validate_federated_memory(payload: dict) -> dict:
    """Validate an incoming federated memory bundle's domain field.

    Returns the payload with normalized domain.
    Raises InvalidDomainError if domain is missing or invalid.

    This is the SHARED validation path — same contract as local publish.
    Step 3+ will add signature and passport verification before this runs.
    """
    domain = payload.get("domain")
    normalized = validate_domain(domain)
    return {**payload, "domain": normalized}
