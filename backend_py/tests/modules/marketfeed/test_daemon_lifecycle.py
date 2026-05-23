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


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_daemon_reads_new_env_event_replay_days_with_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4.4b D4: BFX_EVENT_REPLAY_DAYS preferred; BFX_LEDGER_REPLAY_DAYS fallback
    for 1 deploy cycle transition."""
    monkeypatch.setenv("BFX_EVENT_REPLAY_DAYS", "45")
    monkeypatch.delenv("BFX_LEDGER_REPLAY_DAYS", raising=False)
    from bfx_funding_bot.modules.marketfeed.daemon import _resolve_event_replay_days
    assert _resolve_event_replay_days() == 45


@pytest.mark.asyncio
async def test_daemon_falls_back_to_old_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BFX_EVENT_REPLAY_DAYS", raising=False)
    monkeypatch.setenv("BFX_LEDGER_REPLAY_DAYS", "20")
    from bfx_funding_bot.modules.marketfeed.daemon import _resolve_event_replay_days
    assert _resolve_event_replay_days() == 20


@pytest.mark.asyncio
async def test_daemon_default_30_when_neither_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BFX_EVENT_REPLAY_DAYS", raising=False)
    monkeypatch.delenv("BFX_LEDGER_REPLAY_DAYS", raising=False)
    from bfx_funding_bot.modules.marketfeed.daemon import _resolve_event_replay_days
    assert _resolve_event_replay_days() == 30
