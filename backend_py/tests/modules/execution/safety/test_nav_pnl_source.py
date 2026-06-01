"""ReconcileNavTracker — NAV-based source feeding the L2 loss-limiter guards.

Replaces _StubPnLSource (which returned 0/0, making RealizedLossGuard +
DrawdownGuard no-ops on the real-money canary). NAV (account equity) is sampled
from each PositionReconciled venue snapshot:

    NAV = available + reserved + realized

i.e. total funding-wallet capital for the account's currency — idle funds +
open offers + lent principal. Interest paid raises `available` → NAV up; capital
lost for ANY reason (bug, socialized loss, withdrawal) → NAV down.

  * realized_loss_pct_24h() = (highest-NAV-in-last-24h − latest NAV) / that-high × 100
  * drawdown_pct()          = (all-time-peak NAV − latest NAV) / all-time-peak × 100

Both are PERCENTAGES of the relevant high-water NAV, so the guards auto-scale
with funded capital — no manual re-anchoring when you add/withdraw funds.

Two horizons on purpose: the 24h window catches fast recent bleeding; the
all-time peak catches slow sustained decline. The 24h window is trimmed against
the latest sample's occurred_at_ms (the reconcile clock), so the source needs no
wall-clock injection and is fully deterministic.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.safety.nav_pnl_source import (
    ReconcileNavTracker,
)

_ACC = "default"
_HOUR_MS = 3_600_000
_T0 = 2_000_000_000


def _reconciled(
    *,
    available: str = "0",
    reserved: str = "0",
    realized: str = "0",
    ts: int = _T0,
    account_id: str = _ACC,
    symbol: str = "fUST",
) -> PositionReconciled:
    return PositionReconciled(
        account_id=account_id,
        symbol=symbol,
        reserved=Decimal(reserved),
        realized=Decimal(realized),
        available=Decimal(available),
        n_offers=0,
        n_credits=0,
        occurred_at_ms=ts,
    )


def test_cold_start_returns_zero_before_any_reconcile() -> None:
    """No NAV history yet → permissive (the hard guards + BuyingPowerGuard
    already gate deployment; an L2 breaker must not block on cold start)."""
    t = ReconcileNavTracker(account_id=_ACC)
    assert t.realized_loss_pct_24h() == 0.0
    assert t.drawdown_pct() == 0.0


@pytest.mark.asyncio
async def test_nav_drop_yields_drawdown_and_loss() -> None:
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(_reconciled(available="100", ts=_T0))
    await t.on_position_reconciled(
        _reconciled(available="85", ts=_T0 + _HOUR_MS)
    )
    assert t.realized_loss_pct_24h() == pytest.approx(15.0)
    assert t.drawdown_pct() == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_nav_rise_is_not_a_loss_or_drawdown() -> None:
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(_reconciled(available="100", ts=_T0))
    await t.on_position_reconciled(
        _reconciled(available="110", ts=_T0 + _HOUR_MS)
    )
    assert t.realized_loss_pct_24h() == 0.0
    assert t.drawdown_pct() == 0.0


@pytest.mark.asyncio
async def test_nav_is_available_plus_reserved_plus_realized() -> None:
    """NAV must include lent principal + open offers, not just idle funds."""
    t = ReconcileNavTracker(account_id=_ACC)
    # NAV = 50 + 30 + 20 = 100
    await t.on_position_reconciled(
        _reconciled(available="50", reserved="30", realized="20", ts=_T0)
    )
    # NAV = 40 + 30 + 20 = 90  (available dropped 10)
    await t.on_position_reconciled(
        _reconciled(
            available="40", reserved="30", realized="20", ts=_T0 + _HOUR_MS
        )
    )
    assert t.realized_loss_pct_24h() == pytest.approx(10.0)
    assert t.drawdown_pct() == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_24h_window_evicts_old_high_for_loss_but_peak_is_all_time() -> None:
    """The two horizons diverge: the 24h loss ignores a high >24h old, while
    drawdown still measures from the all-time high-water mark."""
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(_reconciled(available="200", ts=_T0))
    await t.on_position_reconciled(
        _reconciled(available="100", ts=_T0 + 25 * _HOUR_MS)
    )
    await t.on_position_reconciled(
        _reconciled(available="90", ts=_T0 + 25 * _HOUR_MS + 60_000)
    )
    # window (last 24h) high = 100, latest = 90 → 24h loss = 10
    assert t.realized_loss_pct_24h() == pytest.approx(10.0)
    # all-time peak = 200 → drawdown = (200 − 90) / 200 × 100 = 55%
    assert t.drawdown_pct() == pytest.approx(55.0)


@pytest.mark.asyncio
async def test_recovery_from_trough_tracks_latest_not_trough() -> None:
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(_reconciled(available="100", ts=_T0))
    await t.on_position_reconciled(
        _reconciled(available="80", ts=_T0 + _HOUR_MS)
    )
    await t.on_position_reconciled(
        _reconciled(available="95", ts=_T0 + 2 * _HOUR_MS)
    )
    assert t.realized_loss_pct_24h() == pytest.approx(5.0)
    assert t.drawdown_pct() == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_new_peak_after_recovery_resets_drawdown() -> None:
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(_reconciled(available="100", ts=_T0))
    await t.on_position_reconciled(
        _reconciled(available="120", ts=_T0 + _HOUR_MS)
    )  # new high-water mark
    assert t.drawdown_pct() == 0.0
    await t.on_position_reconciled(
        _reconciled(available="108", ts=_T0 + 2 * _HOUR_MS)
    )
    assert t.drawdown_pct() == pytest.approx(10.0)  # (120 − 108) / 120 × 100


@pytest.mark.asyncio
async def test_ignores_other_account() -> None:
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(_reconciled(available="100", ts=_T0))
    await t.on_position_reconciled(
        _reconciled(available="10", ts=_T0 + _HOUR_MS, account_id="other")
    )
    assert t.realized_loss_pct_24h() == 0.0
    assert t.drawdown_pct() == 0.0


@pytest.mark.asyncio
async def test_single_symbol_global_api_identical_to_pre_per_symbol() -> None:
    """One active symbol ⇒ global realized_loss_24h()/drawdown_pct() behave
    exactly as the pre-per-symbol tracker (the SUM is over the one bucket)."""
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(
        _reconciled(available="50", reserved="30", realized="20", ts=_T0)
    )  # NAV(fUST) = 100
    await t.on_position_reconciled(
        _reconciled(
            available="40", reserved="30", realized="20", ts=_T0 + _HOUR_MS,
        )
    )  # NAV(fUST) = 90
    assert t.realized_loss_pct_24h() == pytest.approx(10.0)
    assert t.drawdown_pct() == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_two_symbols_global_nav_is_sum_of_latest_per_symbol() -> None:
    """Global NAV = Σ latest-per-symbol NAV. The peak forms at the moment both
    symbols are at their joint high; a later drop in one symbol's bucket shows
    as a global loss/drawdown — never via cross-symbol cancellation."""
    t = ReconcileNavTracker(account_id=_ACC)
    # t0: fUST NAV = 100  → global = 100
    await t.on_position_reconciled(_reconciled(available="100", ts=_T0, symbol="fUST"))
    # t1: fUSD NAV = 100  → global = 100 (fUST) + 100 (fUSD) = 200  (joint peak)
    await t.on_position_reconciled(
        _reconciled(available="100", ts=_T0 + _HOUR_MS, symbol="fUSD")
    )
    # t2: fUST drops to 70 → global = 70 (fUST) + 100 (fUSD) = 170
    await t.on_position_reconciled(
        _reconciled(available="70", ts=_T0 + 2 * _HOUR_MS, symbol="fUST")
    )
    # window high (24h) = 200, latest 170 -> loss pct = (200-170)/200*100 = 15%
    assert t.realized_loss_pct_24h() == pytest.approx(15.0)
    # all-time peak global NAV = 200 → drawdown = (200 − 170)/200 × 100 = 15%
    assert t.drawdown_pct() == pytest.approx(15.0)
