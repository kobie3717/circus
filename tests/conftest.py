"""Pytest configuration for The Circus tests.

IMPORTANT: This file isolates tests from the production database.
Before this isolation was added, tests ran against ~/.circus/circus.db and
multiple fixtures executed `DELETE FROM owner_keys` (no WHERE clause),
wiping the live owner's public key and breaking preference activation in prod.

The autouse `isolate_database` fixture redirects `settings.database_path` to a
throwaway file under tmp_path_factory for the entire pytest session, then
restores the original path on teardown.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_database(tmp_path_factory):
    """Redirect all DB access to an isolated tmp file for the pytest session.

    Prevents tests from mutating ~/.circus/circus.db (the prod DB used by
    circus-api, friday-bot, claw-bot, etc.). Applies before any test runs.
    """
    from circus.config import settings
    from circus.database import init_database

    original_path = settings.database_path
    tmp_db = tmp_path_factory.mktemp("circus-test-db") / "circus.db"
    settings.database_path = tmp_db

    # Build schema + run all migrations on the isolated DB
    init_database(tmp_db)

    yield tmp_db

    # Restore prod path so post-session tooling (coverage reports, etc.)
    # doesn't keep pointing at the tmp file.
    settings.database_path = original_path


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate limiter state between tests."""
    from circus.middleware.rate_limiter import rate_limits
    rate_limits.clear()
    yield
    rate_limits.clear()
