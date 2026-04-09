"""Pytest configuration for The Circus tests."""

import pytest


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate limiter state between tests."""
    from circus.middleware.rate_limiter import rate_limits
    rate_limits.clear()
    yield
    rate_limits.clear()
