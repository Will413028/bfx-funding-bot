"""Integration test fixtures.

pg_container / pg_engine / pg_session_factory are defined in
tests/conftest.py (session-scoped) so they are available to all integration
tests regardless of directory.
"""
from .test_trading_shadow_candidate import candidate_db  # noqa: F401
