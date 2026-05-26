"""Integration test fixtures.

pg_container / pg_engine / pg_session_factory are defined in
tests/conftest.py (session-scoped) so they are available to all integration
tests regardless of directory.

The phase4_4a harness fixtures (`domain_chain` = real DomainEventBus + real
PaperPositionLedger + real OfferRegistry, subscriber-wired; `event_sink_stub`)
are re-exported here so tests at the tests/integration/ root (not just under
phase4_4a/) can reuse the same real bus→ledger wiring.
"""
from .phase4_4a.conftest import (  # noqa: F401
    domain_chain,
    event_sink_stub,
)
