"""Boot reconciliation integration tests — real Postgres (testcontainers).

Run: cd backend_py && uv run pytest tests/integration/test_boot_recovery_pg.py -q -m integration

Isolation: the testcontainer is session-scoped for startup cost, while the
``pg_engine`` fixture resets the application schemas before each test. Account
IDs still use canonical UUID strings so the runtime path matches production.
"""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.boot_recovery import (
    BootRecovery,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationIntent,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

from .conftest import make_reservation_ref

pytestmark = pytest.mark.integration
_ENV = "ci"


class _StubAuthRest:
    def __init__(self, offers):
        self._offers = offers
    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        return self._offers
    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        return []
    async def get_funding_available(self, *, ctx, currency):
        return Decimal("0")


async def _ignore_unknown(_event) -> None:
    return None


def _ctx(account_id):
    return AccountContext(
        account_id=account_id, credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


async def _seed_account(session_factory, account_id: str) -> None:
    """Create the canonical owner required by normalized venue projections."""
    async with session_factory() as session:
        session.add(
            ExchangeAccount(
                id=UUID(account_id), venue="bitfinex", label=f"test-{account_id}"
            )
        )
        await session.commit()


def _recovery(offers, store, session_factory, account_id, *, clock=lambda: 5_000_000):
    return BootRecovery(
        store=store, session_factory=session_factory, auth_rest=_StubAuthRest(offers),
        account_ctx=_ctx(account_id), deployment_environment=_ENV, bus=DomainEventBus(),
        is_simulated=False, grace_ms=120_000, clock=clock, symbol="fUST",
        uncertainty_handler=_ignore_unknown,
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
                PositionStateRow.symbol == "fUST",
            )
        )).scalar_one_or_none()
        return Decimal(str(row.reserved)) if row else Decimal("0")


@pytest.mark.asyncio
async def test_orphan_at_venue_is_quarantined_without_audited_reference(pg_session_factory):
    acct = "00000000-0000-0000-0000-000000000021"
    await _seed_account(pg_session_factory, acct)
    store = PostgresEventStore(deployment_environment=_ENV)
    offers = [ActiveFundingOffer("777", "fUST", Decimal("250"), 0.0003, 2, 1_000, "ACTIVE")]
    result = await _recovery(offers, store, pg_session_factory, acct).run()

    assert result.n_quarantined == 1
    assert await _claims(pg_session_factory, acct) == []
    # Quarantine preserves the venue truth in the canonical snapshot without
    # fabricating a local CID/reservation reference.
    assert await _reserved(pg_session_factory, acct) == Decimal("250")
    async with pg_session_factory() as session:
        event_types = (
            await session.execute(
                select(EventLogRow.event_type).where(
                    EventLogRow.exchange_account_id == UUID(acct),
                    EventLogRow.deployment_environment == _ENV,
                )
            )
        ).scalars().all()
    assert "VENUE_OFFER_QUARANTINED" in event_types


@pytest.mark.asyncio
async def test_crash_mid_flight_pending_converges_unknown(pg_session_factory):
    acct = "00000000-0000-0000-0000-000000000022"
    await _seed_account(pg_session_factory, acct)
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(
        store=store,
        session_factory=pg_session_factory,
        compatibility_mode=True,
    )
    await persister.persist(ReservationIntent(
        cid=7, execution_decision_id="d-recovery-7", size_usdt=Decimal("60"), symbol="fUST", signal_correlation_id=uuid4(),
        account_id=acct, is_simulated=False, occurred_at_ms=1_000,
    ))
    result = await _recovery([], store, pg_session_factory, acct).run()

    claims = await _claims(pg_session_factory, acct)
    assert result.n_unknown == 1
    assert len(claims) == 1 and claims[0].state == "unknown"
    assert await _reserved(pg_session_factory, acct) == Decimal("0")


@pytest.mark.asyncio
async def test_missing_from_venue_releases(pg_session_factory):
    acct = "00000000-0000-0000-0000-000000000023"
    await _seed_account(pg_session_factory, acct)
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(
        store=store,
        session_factory=pg_session_factory,
        compatibility_mode=True,
    )
    scid = uuid4()
    await persister.persist(
        ReservationIntent(cid=42, execution_decision_id="d-recovery-42", size_usdt=Decimal("80"), symbol="fUST",
                          signal_correlation_id=scid,
                          account_id=acct, is_simulated=False, occurred_at_ms=1_000),
        ReservationClaimed(cid=42, venue_offer_id="999", size_usdt=Decimal("80"),
                          signal_correlation_id=scid, account_id=acct, is_simulated=False,
                          occurred_at_ms=2_000, symbol="fUST",
                          reservation_ref=make_reservation_ref(
                              42, scid, "999", execution_decision_id="d-recovery-42")),
    )
    assert await _reserved(pg_session_factory, acct) == Decimal("80")

    await _recovery([], store, pg_session_factory, acct).run()  # venue has nothing now

    claims = await _claims(pg_session_factory, acct)
    assert claims[0].state == "released"
    assert await _reserved(pg_session_factory, acct) == Decimal("0")


@pytest.mark.asyncio
async def test_recovery_is_idempotent(pg_session_factory):
    acct = "00000000-0000-0000-0000-000000000024"
    await _seed_account(pg_session_factory, acct)
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(
        store=store,
        session_factory=pg_session_factory,
        compatibility_mode=True,
    )
    scid = uuid4()
    await persister.persist(ReservationClaimed(
        cid=77, venue_offer_id="777", size_usdt=Decimal("250"),
        signal_correlation_id=scid, account_id=acct, is_simulated=False,
        occurred_at_ms=1_000, symbol="fUSD",
        reservation_ref=make_reservation_ref(77, scid, "777"),
    ))
    offers = [ActiveFundingOffer("777", "fUST", Decimal("250"), 0.0003, 2, 1_000, "ACTIVE")]
    await _recovery(offers, store, pg_session_factory, acct).run()
    await _recovery(offers, store, pg_session_factory, acct).run()  # second boot

    assert await _reserved(pg_session_factory, acct) == Decimal("250")  # not double-counted
    assert len(await _claims(pg_session_factory, acct)) == 1
