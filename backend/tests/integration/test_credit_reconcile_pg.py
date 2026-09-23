"""Integration test — credit-aware reconcile, no double-count.

Proves that BootRecovery with 1 claimed offer ($100) + 3 credits ($450) produces:
  ledger.realized_exposure() == $450
  ledger.current_exposure()  == $550  (reserved=$100 + realized=$450)

NOT $650 (which would occur if the orphan ReservationClaimed reached the
ledger via bus delta AND PositionReconciled both counted it).

The offer is seeded with its audited reservation reference before recovery.
Recovery intentionally fails closed for an uncorrelated venue orphan, so the
test exercises the safe path and verifies that PositionReconciled is the only
reconcile-time exposure writer.

Run:
  cd backend && uv run pytest tests/integration/test_credit_reconcile_pg.py -v -m integration
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingCredit, ActiveFundingOffer
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
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

from .conftest import make_reservation_ref

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

    async def get_funding_available(
        self, *, ctx: AccountContext, currency: str,
    ) -> Decimal:
        return Decimal("0")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claimed_offer_plus_credits_no_double_count(pg_session_factory):
    """1 audited offer ($100) + 3 credits ($450), real ledger subscribed to
    both delta handlers AND on_position_reconciled.

    Exposure must equal venue truth (reserved=$100, realized=$450) — NOT doubled
    ($650 reserved) which would happen if a recovery claim hit the ledger via
    bus delta AND PositionReconciled both set reserved.
    """
    acct = str(uuid.uuid4())
    async with pg_session_factory() as seed_session:
        seed_session.add(
            ExchangeAccount(id=uuid.UUID(acct), venue="bitfinex", label=f"test-{acct}")
        )
        await seed_session.commit()
    ctx = AccountContext(
        account_id=acct,
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("1000"),
    )
    store = PostgresEventStore(deployment_environment=_ENV)
    ledger = PaperPositionLedger(account_id=acct)

    bus = _Bus()
    # Wire all delta handlers.  Recovery must not publish a duplicate claim for
    # an already-audited offer; PositionReconciled remains the single writer for
    # the reconcile-time absolute exposure snapshot.
    bus.subscribe(ReservationClaimed, ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, ledger.on_order_filled)
    bus.subscribe(ReservationReleased, ledger.on_reservation_released)
    # The single authority for exposure at reconcile time.
    bus.subscribe(PositionReconciled, ledger.on_position_reconciled)

    registry = _Registry()

    # 1 offer at venue ($100), with a matching audited local claim.
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

    scid = uuid.uuid4()
    async with pg_session_factory() as seed_session:
        await store.append(
            seed_session,
            ReservationClaimed(
                cid=555,
                venue_offer_id="555",
                size_usdt=Decimal("100"),
                signal_correlation_id=scid,
                account_id=acct,
                is_simulated=False,
                occurred_at_ms=1,
                symbol="fUST",
                reservation_ref=make_reservation_ref(555, scid, "555"),
            ),
        )
        await seed_session.commit()

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
    assert ledger.realized_exposure("fUST") == Decimal("450"), (
        f"realized={ledger.realized_exposure('fUST')} — expected $450 from 3 credits"
    )
    assert ledger.current_exposure("fUST") == Decimal("550"), (
        f"current={ledger.current_exposure('fUST')} — expected $550 (reserved $100 + realized $450); "
        "if $650 the orphan claim was double-applied via both delta bus AND PositionReconciled"
    )

    # No recovery FSM action is needed for an already-audited offer.
    assert registry.handled == []
