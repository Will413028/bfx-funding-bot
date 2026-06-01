from decimal import Decimal
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


@pytest.mark.asyncio
async def test_handle_claim_adds_record() -> None:
    reg = OfferRegistry(clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=500,
    symbol="fUST"))
    snap = reg.snapshot()
    assert "v1" in snap
    assert snap["v1"].state == RegistryState.CLAIMED


@pytest.mark.asyncio
async def test_snapshot_is_immutable_view() -> None:
    reg = OfferRegistry(clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
    symbol="fUST"))
    snap = reg.snapshot()
    snap["v2"] = "tampered"  # type: ignore[assignment]
    assert "v2" not in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_keeps_recent_released_records() -> None:
    """Within TTL — RELEASED record kept."""
    reg = OfferRegistry(clock=lambda: 1000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=500,
    symbol="fUST"))
    await reg.handle(ReservationReleased(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        reason="user_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=False, occurred_at_ms=900,
    symbol="fUST"))
    assert reg.snapshot()["v1"].state == RegistryState.RELEASED
    # clock=1000, last_updated_ms=1000, threshold = 1000 - 86_400_000 = very negative
    # record's last_updated >= threshold, so kept
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v1" in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_preserves_claimed_records() -> None:
    """CLAIMED never cleaned, regardless of age."""
    reg = OfferRegistry(clock=lambda: 1_000_000_000_000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v1", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=0,
    symbol="fUST"))
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v1" in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_removes_old_released_records() -> None:
    """Past TTL — RELEASED record removed."""
    reg = OfferRegistry(clock=lambda: 100_000_000_000)
    await reg.handle(ReservationClaimed(
        cid=42, venue_offer_id="v_old", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=100,
    symbol="fUST"))
    # release at clock=100_000_000_000
    await reg.handle(ReservationReleased(
        cid=42, venue_offer_id="v_old", size_usdt=Decimal("100"),
        reason="user_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=False, occurred_at_ms=200,
    symbol="fUST"))
    # Move clock forward by 25hr
    reg._clock = lambda: 100_000_000_000 + 25 * 3600 * 1000  # type: ignore[method-assign]
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v_old" not in reg.snapshot()


