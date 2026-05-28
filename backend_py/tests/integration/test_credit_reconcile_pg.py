"""Integration test — credit-aware reconcile, no double-count.

Proves that BootRecovery with 1 orphan offer ($100) + 3 credits ($450) produces:
  ledger.realized_exposure() == $450
  ledger.current_exposure()  == $550  (reserved=$100 + realized=$450)

NOT $650 (which would occur if the orphan ReservationClaimed reached the
ledger via bus delta AND PositionReconciled both counted it).

Routing invariant under test:
  - ReservationClaimed for orphan → _route_fsm → offer_registry.handle  (NOT bus)
  - PositionReconciled           → _safe_publish → bus → ledger.on_position_reconciled
  => ledger sees the absolute snapshot once; no delta double-count.

Run:
  cd backend_py && uv run pytest tests/integration/test_credit_reconcile_pg.py -v -m integration
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingCredit, ActiveFundingOffer
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

pytestmark = pytest.mark.integration

_ENV = "ci"


# ---------------------------------------------------------------------------
# Minimal in-process bus — subscribe by event type, publish fans out.
# ---------------------------------------------------------------------------

class _Bus:
    def __init__(self) -> None:
        self._subs: dict = {}

    def subscribe(self, etype: type, cb) -> None:  # type: ignore[type-arg]
        self._subs.setdefault(etype, []).append(cb)

    async def publish(self, ev: object) -> None:
        for cb in self._subs.get(type(ev), []):
            await cb(ev)


# ---------------------------------------------------------------------------
# Registry stub — FSM sink that records every handled event.
# ---------------------------------------------------------------------------

class _Registry:
    def __init__(self) -> None:
        self.handled: list[object] = []

    async def handle(self, ev: object) -> None:
        self.handled.append(ev)


# ---------------------------------------------------------------------------
# Auth REST stub — returns scripted offers + credits.
# ---------------------------------------------------------------------------

class _AuthRest:
    def __init__(
        self,
        offers: list[ActiveFundingOffer],
        credits: list[ActiveFundingCredit],
    ) -> None:
        self._o = offers
        self._c = credits

    async def get_active_funding_offers(
        self, *, ctx: AccountContext, symbol: str = "fUST"
    ) -> list[ActiveFundingOffer]:
        return self._o

    async def get_active_funding_credits(
        self, *, ctx: AccountContext, symbol: str = "fUST"
    ) -> list[ActiveFundingCredit]:
        return self._c


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orphan_offer_plus_credits_no_double_count(pg_session_factory):
    """1 orphan offer ($100) + 3 credits ($450), real ledger subscribed to both
    delta handlers AND on_position_reconciled.

    Exposure must equal venue truth (reserved=$100, realized=$450) — NOT doubled
    ($650 reserved) which would happen if the orphan claim hit the ledger via bus
    delta AND PositionReconciled both set reserved.
    """
    acct = f"it-{uuid.uuid4().hex[:8]}"
    ctx = AccountContext(
        account_id=acct,
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("1000"),
    )
    store = PostgresEventStore(deployment_environment=_ENV)
    ledger = PaperPositionLedger(account_id=acct)

    bus = _Bus()
    # Wire all delta handlers — these must NOT fire for the orphan claim because
    # BootRecovery routes FSM events to offer_registry, not the bus.
    bus.subscribe(ReservationClaimed, ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, ledger.on_order_filled)
    bus.subscribe(ReservationReleased, ledger.on_reservation_released)
    # The single authority for exposure at reconcile time.
    bus.subscribe(PositionReconciled, ledger.on_position_reconciled)

    registry = _Registry()

    # 1 orphan offer at venue ($100) — no matching local claim in the event store.
    offers = [
        ActiveFundingOffer(
            venue_offer_id="555",
            symbol="fUST",
            amount=Decimal("100"),
            rate=0.0003,
            period_days=2,
            mts_created=1,
            status="ACTIVE",
        )
    ]
    # 3 credits ($150 each → $450 total realized).
    credits = [
        ActiveFundingCredit(
            credit_id=str(i),
            symbol="fUST",
            amount=Decimal("150"),
            rate=0.0003,
            period_days=2,
            status="ACTIVE",
        )
        for i in range(3)
    ]

    rec = BootRecovery(
        store=store,
        session_factory=pg_session_factory,
        auth_rest=_AuthRest(offers, credits),
        account_ctx=ctx,
        deployment_environment=_ENV,
        bus=bus,
        offer_registry=registry,
        symbol="fUST",
        clock=lambda: 2_000_000,
    )

    await rec.run()

    # --- Assertions ---

    # Venue truth: reserved=$100 (1 offer), realized=$450 (3×$150 credits).
    assert ledger.realized_exposure() == Decimal("450"), (
        f"realized={ledger.realized_exposure()} — expected $450 from 3 credits"
    )
    assert ledger.current_exposure() == Decimal("550"), (
        f"current={ledger.current_exposure()} — expected $550 (reserved $100 + realized $450); "
        "if $650 the orphan claim was double-applied via both delta bus AND PositionReconciled"
    )

    # The orphan ReservationClaimed was routed to the FSM registry, not the bus.
    assert any(isinstance(e, ReservationClaimed) for e in registry.handled), (
        "orphan ReservationClaimed must reach the offer_registry (FSM), not be dropped"
    )
