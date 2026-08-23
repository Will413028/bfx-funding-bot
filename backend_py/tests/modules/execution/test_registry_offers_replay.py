from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import (
    OfferRegistry,
    RegistryState,
    ReservationCorrelationError,
)


def _claim(voi: str, *, occurred_at_ms: int = 500) -> ReservationClaimed:
    scid = uuid4()
    return ReservationClaimed(
        cid=42, venue_offer_id=voi, size_usdt=Decimal("100"),
        signal_correlation_id=scid, account_id="default", is_simulated=False,
        occurred_at_ms=occurred_at_ms, symbol="fUST", reservation_ref=ReservationRef(
            execution_decision_id="d-registry", cid=42,
            signal_correlation_id=scid, venue_offer_id=voi,
        ),
    )


def _release(claim: ReservationClaimed, *, occurred_at_ms: int) -> ReservationReleased:
    return ReservationReleased(
        cid=claim.cid, venue_offer_id=claim.venue_offer_id, size_usdt=claim.size_usdt,
        reason="user_cancel", signal_correlation_id=claim.signal_correlation_id,
        account_id=claim.account_id, is_simulated=False, occurred_at_ms=occurred_at_ms,
        symbol=claim.symbol, reservation_ref=claim.reservation_ref,
    )


@pytest.mark.asyncio
async def test_handle_claim_adds_record() -> None:
    reg = OfferRegistry(clock=lambda: 1000)
    await reg.handle(_claim("v1"))
    snap = reg.snapshot()
    assert "v1" in snap
    assert snap["v1"].state == RegistryState.CLAIMED


@pytest.mark.asyncio
async def test_snapshot_is_immutable_view() -> None:
    reg = OfferRegistry(clock=lambda: 1000)
    await reg.handle(_claim("v1"))
    snap = reg.snapshot()
    snap["v2"] = "tampered"  # type: ignore[assignment]
    assert "v2" not in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_keeps_recent_released_records() -> None:
    """Within TTL — RELEASED record kept."""
    reg = OfferRegistry(clock=lambda: 1000)
    claim = _claim("v1")
    await reg.handle(claim)
    await reg.handle(_release(claim, occurred_at_ms=900))
    assert reg.snapshot()["v1"].state == RegistryState.RELEASED
    # clock=1000, last_updated_ms=1000, threshold = 1000 - 86_400_000 = very negative
    # record's last_updated >= threshold, so kept
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v1" in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_preserves_claimed_records() -> None:
    """CLAIMED never cleaned, regardless of age."""
    reg = OfferRegistry(clock=lambda: 1_000_000_000_000)
    await reg.handle(_claim("v1", occurred_at_ms=0))
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v1" in reg.snapshot()


@pytest.mark.asyncio
async def test_cleanup_terminal_removes_old_released_records() -> None:
    """Past TTL — RELEASED record removed."""
    reg = OfferRegistry(clock=lambda: 100_000_000_000)
    claim = _claim("v_old", occurred_at_ms=100)
    await reg.handle(claim)
    # release at clock=100_000_000_000
    await reg.handle(_release(claim, occurred_at_ms=200))
    # Move clock forward by 25hr
    reg._clock = lambda: 100_000_000_000 + 25 * 3600 * 1000  # type: ignore[method-assign]
    reg.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
    assert "v_old" not in reg.snapshot()


@pytest.mark.asyncio
async def test_snapshot_duplicate_venue_offer_id_fails_closed_as_ambiguous() -> None:
    row = SimpleNamespace(
        venue_offer_id="v-ambiguous", cid=42, signal_correlation_id=str(uuid4()),
        size_usdt=Decimal("100"), account_id="default", state="claimed",
        occurred_at_ms=1000, last_updated_ms=1000, symbol="fUST",
        execution_decision_id="d-42",
    )
    duplicate = SimpleNamespace(**{**row.__dict__, "cid": 43})

    class _Result:
        def scalars(self) -> "_Result":
            return self

        def all(self) -> list[SimpleNamespace]:
            return [row, duplicate]

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

    with pytest.raises(ReservationCorrelationError, match="ambiguous"):
        await OfferRegistry.from_snapshot(
            _Session(), account_id="default", deployment_environment="ci",  # type: ignore[arg-type]
        )
