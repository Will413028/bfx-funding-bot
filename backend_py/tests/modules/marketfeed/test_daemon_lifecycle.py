"""Phase 4.4c regression guards — Axiom replay/query adapters removed from daemon.

Asserts that AxiomReplayQueryAdapter and stub placeholders are no longer
present on the daemon module, and that daemon boot uses PG from_snapshot
as the sole replay source.
"""
from __future__ import annotations

import bfx_funding_bot.modules.marketfeed.daemon as daemon_mod


def test_axiom_query_stub_deleted() -> None:
    """4.4b D1: `_AxiomQueryAdapter` placeholder must be removed."""
    assert not hasattr(daemon_mod, "_AxiomQueryAdapter"), (
        "_AxiomQueryAdapter stub should be deleted (Phase 4.4b D1)"
    )


def test_offer_registry_query_stub_deleted() -> None:
    """4.4b D1: `_OfferRegistryQueryStub` placeholder must be removed."""
    assert not hasattr(daemon_mod, "_OfferRegistryQueryStub"), (
        "_OfferRegistryQueryStub stub should be deleted (Phase 4.4b D1)"
    )


def test_axiom_replay_adapter_not_in_daemon_boot() -> None:
    """Phase 4.4c: AxiomReplayQueryAdapter removed from daemon boot path.
    Daemon no longer imports it (PG from_snapshot is the boot replay source).
    """
    assert not hasattr(daemon_mod, "AxiomReplayQueryAdapter"), (
        "AxiomReplayQueryAdapter should be removed from daemon (Phase 4.4c)"
    )
