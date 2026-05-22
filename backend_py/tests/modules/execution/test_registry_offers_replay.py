from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import (
    OfferRegistry,
    RegistryState,
)


class _StubAxiomQuery:
    """Stub mirrors Phase 4.3 _AxiomQueryAdapter shape."""
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows or []

    async def fetch_events(self, **kwargs: Any) -> list[dict]:
        return self._rows


@pytest.mark.asyncio
async def test_handle_claim_adds_record() -> None:
    reg = OfferRegistry(axiom_query=_StubAxiomQuery(), clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=500,
    ))
    snap = reg.snapshot()
    assert "v1" in snap
    assert snap["v1"].state == RegistryState.CLAIMED


@pytest.mark.asyncio
async def test_snapshot_is_immutable_view() -> None:
    reg = OfferRegistry(axiom_query=_StubAxiomQuery(), clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
    ))
    snap = reg.snapshot()
    snap["v2"] = "tampered"  # type: ignore[assignment]
    assert "v2" not in reg.snapshot()


@pytest.mark.asyncio
async def test_replay_from_axiom_with_stub_returns_empty() -> None:
    """4.4a stub returns [] — replay is no-op until real adapter ships."""
    reg = OfferRegistry(axiom_query=_StubAxiomQuery(rows=[]), clock=lambda: 1000)
    await reg.replay_from_axiom()
    assert reg.snapshot() == {}


@pytest.mark.asyncio
async def test_cleanup_terminal_keeps_recent_released_records() -> None:
    """Within TTL — RELEASED record kept."""
    reg = OfferRegistry(axiom_query=_StubAxiomQuery(), clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=500,
    ))
    await reg.handle(ReservationReleased(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        reason="user_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=False, occurred_at_ms=900,
    ))
    assert reg.snapshot()["v1"].state == RegistryState.RELEASED
    # clock=1000, last_updated_ms=1000, threshold = 1000 - 86_400_000 = very negative
    # record's last_updated >= threshold, so kept
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v1" in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_preserves_claimed_records() -> None:
    """CLAIMED never cleaned, regardless of age."""
    reg = OfferRegistry(axiom_query=_StubAxiomQuery(), clock=lambda: 1_000_000_000_000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=0,
    ))
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v1" in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_removes_old_released_records() -> None:
    """Past TTL — RELEASED record removed."""
    reg = OfferRegistry(axiom_query=_StubAxiomQuery(), clock=lambda: 100_000_000_000)
    # Manually inject a RELEASED record with very old last_updated_ms
    # (simulating an old release whose TTL has passed)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v_old", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=100,
    ))
    # release at clock=100_000_000_000
    await reg.handle(ReservationReleased(
        cid=42, venue_offer_id="v_old", size_usdt=Decimal("100"),
        reason="user_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=False, occurred_at_ms=200,
    ))
    # Move clock forward by 25hr
    reg._clock = lambda: 100_000_000_000 + 25 * 3600 * 1000  # type: ignore[method-assign]
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v_old" not in reg.snapshot()
