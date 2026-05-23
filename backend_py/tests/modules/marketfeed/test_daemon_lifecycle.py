"""Phase 4.4b D1 — daemon wires real AxiomReplayQueryAdapter.

Phase 4.4c update: AxiomReplayQueryAdapter removed from daemon boot path
(replaced by PG from_snapshot). The import and Daemon.axiom_query field are
removed. Tests adjusted accordingly.
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


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_daemon_reads_new_env_event_replay_days_with_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4.4b D4: BFX_EVENT_REPLAY_DAYS preferred; BFX_LEDGER_REPLAY_DAYS fallback
    for 1 deploy cycle transition. _resolve_event_replay_days() is kept in daemon
    for reference; boot path no longer calls it (Phase 4.4c)."""
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
