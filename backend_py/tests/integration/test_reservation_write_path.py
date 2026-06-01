"""A2 write-path integration tests — real Postgres (testcontainers).

Run: cd backend_py && uv run pytest tests/integration/test_reservation_write_path.py -q -m integration
"""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.middleware.reservation_emitting import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload

pytestmark = pytest.mark.integration
_ENV = "ci"


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    symbol="fUST")


def _ctx(account_id: str) -> AccountContext:
    return AccountContext(
        account_id=account_id,
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


def _mw(inner, pg_session_factory, *, account_simulated: bool = True) -> ReservationEmittingMiddleware:
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    return ReservationEmittingMiddleware(
        inner, bus=DomainEventBus(), persister=persister, is_simulated=account_simulated,
    )


class _PgAssertingInner:
    """On submit, asserts (read-your-writes) the INTENT is already a committed
    PENDING row before returning the chosen outcome."""
    def __init__(self, *, pg_session_factory, account_id: str, status: str, voi: str | None) -> None:
        self._sf = pg_session_factory
        self._account_id = account_id
        self._status = status
        self._voi = voi

    async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
        async with self._sf() as s:
            row = (await s.execute(select(OfferClaimRow).where(
                OfferClaimRow.cid == cid,
                OfferClaimRow.account_id == self._account_id,
                OfferClaimRow.deployment_environment == _ENV,
            ))).scalar_one()
            assert row.state == "pending"
            assert row.venue_offer_id is None
        return SubmittedOrder(cid=cid or 0, venue_offer_id=self._voi, status=self._status, raw_response=None)


async def test_intent_committed_before_submit(pg_session_factory) -> None:
    acct = "wp_intent"
    inner = _PgAssertingInner(pg_session_factory=pg_session_factory, account_id=acct,
                              status="submitted", voi="v_intent")
    mw = _mw(inner, pg_session_factory)
    await mw.submit(_decision(), _ctx(acct))  # inner asserts PENDING visible mid-flight


async def test_claimed_updates_same_cid_row(pg_session_factory) -> None:
    acct = "wp_claim"

    class _Inner:
        async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
            return SubmittedOrder(cid=cid or 0, venue_offer_id="v_claim", status="submitted", raw_response=None)

    mw = _mw(_Inner(), pg_session_factory, account_simulated=False)
    await mw.submit(_decision(), _ctx(acct))
    async with pg_session_factory() as s:
        rows = (await s.execute(select(OfferClaimRow).where(
            OfferClaimRow.account_id == acct,
            OfferClaimRow.deployment_environment == _ENV,
        ))).scalars().all()
        assert len(rows) == 1                       # PENDING promoted in place
        assert rows[0].state == "claimed"
        assert rows[0].venue_offer_id == "v_claim"
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == acct,
            PositionStateRow.deployment_environment == _ENV,
            PositionStateRow.symbol == "fUST",
        ))).scalar_one()
        assert ps.reserved == Decimal("100")   # CLAIMED reserved += size


async def test_failed_marks_failed_reserved_zero(pg_session_factory) -> None:
    acct = "wp_failed"

    class _Inner:
        async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
            return SubmittedOrder(cid=cid or 0, venue_offer_id=None, status="failed", raw_response=None)

    mw = _mw(_Inner(), pg_session_factory, account_simulated=False)
    await mw.submit(_decision(), _ctx(acct))
    async with pg_session_factory() as s:
        claim = (await s.execute(select(OfferClaimRow).where(
            OfferClaimRow.account_id == acct,
            OfferClaimRow.deployment_environment == _ENV,
        ))).scalar_one()
        assert claim.state == "failed"
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == acct,
            PositionStateRow.deployment_environment == _ENV,
            PositionStateRow.symbol == "fUST",
        ))).scalar_one()
        assert ps.reserved == Decimal("0")     # FAILED never reserves


async def test_crash_mid_flight_leaves_pending(pg_session_factory) -> None:
    acct = "wp_crash"

    class _RaisingInner:
        async def submit(self, decision, ctx, *, cid=None) -> SubmittedOrder:
            raise RuntimeError("crash between INTENT and outcome")

    mw = _mw(_RaisingInner(), pg_session_factory)
    with pytest.raises(RuntimeError):
        await mw.submit(_decision(), _ctx(acct))
    async with pg_session_factory() as s:
        rows = (await s.execute(select(OfferClaimRow).where(
            OfferClaimRow.account_id == acct,
            OfferClaimRow.deployment_environment == _ENV,
        ))).scalars().all()
        assert len(rows) == 1
        assert rows[0].state == "pending"           # durable PENDING for 3a-recovery
        assert rows[0].venue_offer_id is None
        cnt = (await s.execute(select(func.count()).select_from(OfferClaimRow).where(
            OfferClaimRow.account_id == acct))).scalar_one()
        assert cnt == 1
