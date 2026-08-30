"""PositionReconciled event + PaperPositionLedger.on_position_reconciled unit tests.

Bug reproduction context (2026-05-29):
  Canary has 3 active credits ($450 total). Bot ledger shows realized=$300.
  Root cause: reconcile checked /offers only. An offer that matched into a
  credit disappeared from /offers and was released ("missing_from_venue"),
  but capital remained lent (the credit was still active at the venue).
  Fix: periodic reconcile also fetches /credits and emits PositionReconciled
  with absolute realized = Σ(active credits). on_position_reconciled does an
  absolute SET — not a delta — so stale state is immediately corrected.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger

_ACC = "default"
_NOW = 2_000_000_000


def _reconciled(
    *,
    reserved: str = "0",
    realized: str = "0",
    available: str = "0",
    n_offers: int = 0,
    n_credits: int = 0,
    account_id: str = _ACC,
) -> PositionReconciled:
    return PositionReconciled(
        account_id=account_id,
        symbol="fUSD",
        reserved_usdt=Decimal(reserved),
        realized_usdt=Decimal(realized),
        available_usdt=Decimal(available),
        n_offers=n_offers,
        n_credits=n_credits,
        occurred_at_ms=_NOW,
    )


# ── Bug reproduction ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_offer_filled_into_credit_but_ws_missed_reconcile_sets_realized():
    """The canary incident: ledger shows reserved=$150, realized=$0 (WS fill missed).
    The credit IS active at the venue. One reconcile corrects realized to $150.
    Without the fix: old reconcile released the offer → realized stayed $0.
    With the fix: PositionReconciled(realized=150) → realized = $150.
    """
    ledger = PaperPositionLedger(account_id=_ACC)
    ledger._reserved["fUSD"] = Decimal("150")  # stale: offer was "reserved"
    ledger._realized["fUSD"] = Decimal("0")    # WS fill was missed

    await ledger.on_position_reconciled(_reconciled(reserved="0", realized="150", n_credits=1))

    assert ledger._reserved["fUSD"] == Decimal("0")
    assert ledger._realized["fUSD"] == Decimal("150")
    assert ledger.current_exposure("fUSD") == Decimal("150")


@pytest.mark.asyncio
async def test_three_credits_reconcile_corrects_full_canary_incident():
    """Full $450 canary scenario: 3 credits, ledger drifted to $300."""
    ledger = PaperPositionLedger(account_id=_ACC)
    ledger._reserved["fUSD"] = Decimal("0")
    ledger._realized["fUSD"] = Decimal("300")  # bot thinks only 2 credits

    await ledger.on_position_reconciled(_reconciled(realized="450", n_credits=3))

    assert ledger._realized["fUSD"] == Decimal("450")
    assert ledger.current_exposure("fUSD") == Decimal("450")


# ── Absolute set semantics ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_position_reconciled_overwrites_previous_state():
    """Second reconcile replaces first — it is a SET, not ADD."""
    ledger = PaperPositionLedger(account_id=_ACC)
    await ledger.on_position_reconciled(_reconciled(reserved="500", realized="300"))
    await ledger.on_position_reconciled(_reconciled(reserved="50", realized="400"))

    assert ledger._reserved["fUSD"] == Decimal("50")
    assert ledger._realized["fUSD"] == Decimal("400")


@pytest.mark.asyncio
async def test_current_exposure_equals_offers_plus_credits():
    """current_exposure = reserved (open offers Σ) + realized (active credits Σ)."""
    ledger = PaperPositionLedger(account_id=_ACC)
    await ledger.on_position_reconciled(
        _reconciled(reserved="100", realized="200", n_offers=1, n_credits=2)
    )

    assert ledger.current_exposure("fUSD") == Decimal("300")
    assert ledger.realized_exposure("fUSD") == Decimal("200")


@pytest.mark.asyncio
async def test_on_position_reconciled_ignores_foreign_account():
    ledger = PaperPositionLedger(account_id=_ACC)
    await ledger.on_position_reconciled(_reconciled(realized="999", account_id="other"))

    assert ledger._realized == {}  # unchanged — dict stays empty


# ── Convergence scenarios ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_credit_matured_reconcile_decrements_realized():
    """Credit returned to lender → next reconcile sees fewer credits → realized down."""
    ledger = PaperPositionLedger(account_id=_ACC)
    await ledger.on_position_reconciled(_reconciled(realized="450", n_credits=3))
    await ledger.on_position_reconciled(_reconciled(realized="300", n_credits=2))

    assert ledger._realized["fUSD"] == Decimal("300")
    assert ledger.current_exposure("fUSD") == Decimal("300")


@pytest.mark.asyncio
async def test_offer_cancelled_unfilled_reserved_drops_realized_flat():
    """Offer leaves /offers without a fill (cancel/expire) → reserved down, realized flat."""
    ledger = PaperPositionLedger(account_id=_ACC)
    # 1 offer ($100 reserved), 1 credit ($200 realized)
    await ledger.on_position_reconciled(
        _reconciled(reserved="100", realized="200", n_offers=1, n_credits=1)
    )
    # Offer cancelled: no longer in /offers, no new credit
    await ledger.on_position_reconciled(
        _reconciled(reserved="0", realized="200", n_offers=0, n_credits=1)
    )

    assert ledger._reserved["fUSD"] == Decimal("0")
    assert ledger._realized["fUSD"] == Decimal("200")


# ── WS delta interaction ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_fill_after_reconcile_overwritten_by_next_reconcile():
    """
    WS fill that arrived AFTER the snapshot was taken causes a temporary delta.
    The next reconcile (which sees the fill already in Σcredits) sets the absolute
    value — no double-count.
    """
    ledger = PaperPositionLedger(account_id=_ACC)
    # Snapshot at t=0: 1 credit = $150 realized
    await ledger.on_position_reconciled(
        _reconciled(reserved="0", realized="150", n_credits=1)
    )

    # WS foc EXECUTED arrives for a NEW fill (not yet in the snapshot)
    scid = uuid4()
    fill = OrderFilled(
        cid=1, venue_offer_id="v1", credit_id=None,
        size_usdt=Decimal("150"), fill_rate=0.0003,
        signal_correlation_id=scid, account_id=_ACC, is_simulated=False,
        symbol="fUSD",
        reservation_ref=ReservationRef(
            execution_decision_id="d-position-reconciled", cid=1,
            signal_correlation_id=scid, venue_offer_id="v1",
        ),
    )
    await ledger.on_order_filled(fill)
    # In-memory: realized = 300 (snapshot 150 + WS delta 150)

    # Next reconcile at t+90s sees 2 credits = $300 — sets absolute truth
    await ledger.on_position_reconciled(
        _reconciled(reserved="0", realized="300", n_credits=2)
    )

    assert ledger._realized["fUSD"] == Decimal("300")  # not 450
    assert ledger.current_exposure("fUSD") == Decimal("300")
