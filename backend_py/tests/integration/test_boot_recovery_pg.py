"""Boot reconciliation integration tests — real Postgres (testcontainers).

Run: cd backend_py && uv run pytest tests/integration/test_boot_recovery_pg.py -q -m integration

Isolation: the testcontainer DB is session-scoped and tables are created via
Base.metadata (no per-test reset), so rows accumulate across tests. Each test
therefore uses a UNIQUE account_id and every query filters by it — the same
convention as test_reservation_write_path.py.
"""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery, synth_orphan_cid
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationIntent,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

pytestmark = pytest.mark.integration
_ENV = "ci"


class _StubAuthRest:
    def __init__(self, offers):
        self._offers = offers
    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        return self._offers


def _ctx(account_id):
    return AccountContext(
        account_id=account_id, credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


def _recovery(offers, store, session_factory, account_id, *, clock=lambda: 5_000_000):
    return BootRecovery(
        store=store, session_factory=session_factory, auth_rest=_StubAuthRest(offers),
        account_ctx=_ctx(account_id), deployment_environment=_ENV, bus=DomainEventBus(),
        is_simulated=False, grace_ms=120_000, clock=clock,
    )


async def _claims(session_factory, account_id):
    async with session_factory() as s:
        return (await s.execute(
            select(OfferClaimRow).where(
                OfferClaimRow.deployment_environment == _ENV,
                OfferClaimRow.account_id == account_id,
            )
        )).scalars().all()


async def _reserved(session_factory, account_id) -> Decimal:
    async with session_factory() as s:
        row = (await s.execute(
            select(PositionStateRow).where(
                PositionStateRow.deployment_environment == _ENV,
                PositionStateRow.account_id == account_id,
            )
        )).scalar_one_or_none()
        return Decimal(str(row.reserved_usdt)) if row else Decimal("0")


@pytest.mark.asyncio
async def test_orphan_at_venue_is_claimed_and_reserved(pg_session_factory):
    acct = "br_orphan"
    store = PostgresEventStore(deployment_environment=_ENV)
    offers = [ActiveFundingOffer("777", "fUSD", Decimal("250"), 0.0003, 2, 1_000, "ACTIVE")]
    await _recovery(offers, store, pg_session_factory, acct).run()

    claims = await _claims(pg_session_factory, acct)
    assert len(claims) == 1
    assert claims[0].cid == synth_orphan_cid("777")
    assert claims[0].state == "claimed" and claims[0].venue_offer_id == "777"
    assert await _reserved(pg_session_factory, acct) == Decimal("250")


@pytest.mark.asyncio
async def test_crash_mid_flight_pending_converges_failed(pg_session_factory):
    acct = "br_pending"
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    await persister.persist(ReservationIntent(
        cid=7, size_usdt=Decimal("60"), signal_correlation_id=uuid4(),
        account_id=acct, is_simulated=False, occurred_at_ms=1_000,
    ))
    await _recovery([], store, pg_session_factory, acct).run()

    claims = await _claims(pg_session_factory, acct)
    assert len(claims) == 1 and claims[0].state == "failed"
    assert await _reserved(pg_session_factory, acct) == Decimal("0")


@pytest.mark.asyncio
async def test_missing_from_venue_releases(pg_session_factory):
    acct = "br_missing"
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    scid = uuid4()
    await persister.persist(
        ReservationIntent(cid=42, size_usdt=Decimal("80"), signal_correlation_id=scid,
                          account_id=acct, is_simulated=False, occurred_at_ms=1_000),
        ReservationClaimed(cid=42, venue_offer_id="999", size_usdt=Decimal("80"),
                          signal_correlation_id=scid, account_id=acct, is_simulated=False,
                          occurred_at_ms=2_000),
    )
    assert await _reserved(pg_session_factory, acct) == Decimal("80")

    await _recovery([], store, pg_session_factory, acct).run()  # venue has nothing now

    claims = await _claims(pg_session_factory, acct)
    assert claims[0].state == "released"
    assert await _reserved(pg_session_factory, acct) == Decimal("0")


@pytest.mark.asyncio
async def test_recovery_is_idempotent(pg_session_factory):
    acct = "br_idem"
    store = PostgresEventStore(deployment_environment=_ENV)
    offers = [ActiveFundingOffer("777", "fUSD", Decimal("250"), 0.0003, 2, 1_000, "ACTIVE")]
    await _recovery(offers, store, pg_session_factory, acct).run()
    await _recovery(offers, store, pg_session_factory, acct).run()  # second boot

    assert await _reserved(pg_session_factory, acct) == Decimal("250")  # not double-counted
    assert len(await _claims(pg_session_factory, acct)) == 1
