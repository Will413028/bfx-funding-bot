"""Phase 4.4b D1 — daemon wires real AxiomReplayQueryAdapter.

The 4.3/4.4a placeholder stubs (`_AxiomQueryAdapter` for ledger,
`_OfferRegistryQueryStub` for registry) must be deleted; a single
`AxiomReplayQueryAdapter` instance now satisfies both consumer protocols
via duck typing (Hexagonal port-per-consumer).

The smoke `AxiomSmokeQueryAdapter` (renamed from `AxiomEventQueryAdapter`
in T5) is a separate concern for L3 verification and stays wired.
"""
from __future__ import annotations

import bfx_funding_bot.modules.marketfeed.daemon as daemon_mod
from bfx_funding_bot.modules.execution.axiom_event_query import (
    AxiomReplayQueryAdapter,
)


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


def test_axiom_replay_adapter_imported_in_daemon() -> None:
    """Real `AxiomReplayQueryAdapter` is imported by daemon for replay wiring."""
    assert getattr(daemon_mod, "AxiomReplayQueryAdapter", None) is AxiomReplayQueryAdapter
