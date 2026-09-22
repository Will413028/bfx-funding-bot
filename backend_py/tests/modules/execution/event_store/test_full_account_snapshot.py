from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingCredit,
    ActiveFundingOffer,
)
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
    ReconcileObservationRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.events import (
    FullAccountSnapshot,
    SnapshotCoverage,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

_ACCOUNT = "550e8400-e29b-41d4-a716-446655440000"
_SCID = UUID("11111111-1111-1111-1111-111111111111")


async def _create_all(session: AsyncSession) -> None:
    bind = session.bind
    assert bind is not None
    async with bind.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.create_all)


def _snapshot(*, observed_at: int = 2_000) -> VenueSnapshotObserved:
    return VenueSnapshotObserved(
        account_id=_ACCOUNT,
        environment="ci",
        query_started_at_ms=observed_at - 100,
        query_finished_at_ms=observed_at,
        offers=(
            VenueOfferObservation(
                venue_offer_id="offer-fust",
                symbol="fUST",
                amount_original=Decimal("12.5"),
                amount_remaining=Decimal("10.0"),
                rate=Decimal("0.0003"),
                period_days=2,
                status="ACTIVE",
                mts_created=observed_at - 90,
                mts_updated=observed_at - 10,
            ),
            VenueOfferObservation(
                venue_offer_id="offer-fbtc",
                symbol="fBTC",
                amount_original=Decimal("3"),
                amount_remaining=Decimal("3"),
                rate=Decimal("0.0002"),
                period_days=7,
                status="ACTIVE",
                mts_created=observed_at - 80,
                mts_updated=observed_at - 5,
            ),
        ),
        credits=(
            VenueCreditObservation(
                credit_id="credit-fust",
                symbol="fUST",
                amount=Decimal("7"),
                rate=Decimal("0.0004"),
                period_days=2,
                status="ACTIVE",
                mts_created=observed_at - 70,
                mts_updated=observed_at - 3,
            ),
        ),
        wallet_available={"fUST": Decimal("25"), "fBTC": Decimal("4")},
        coverage=SnapshotCoverage(
            active_offers_complete=True,
            active_credits_complete=True,
            wallets_complete=True,
            active_offer_pages=1,
            active_credit_pages=1,
            wallet_pages=1,
        ),
        occurred_at_ms=observed_at,
    )


def test_full_account_snapshot_is_immutable_and_round_trips() -> None:
    event = _snapshot()
    assert isinstance(event, FullAccountSnapshot)
    payload = serialize_event(event)
    assert payload["__event_type__"] == "VENUE_SNAPSHOT_OBSERVED"
    assert payload["coverage"]["active_offers_complete"] is True
    assert payload["offers"][1]["symbol"] == "fBTC"
    restored = deserialize_event("VENUE_SNAPSHOT_OBSERVED", payload)
    assert restored == event
    with pytest.raises(TypeError):
        event.wallet_available["fUST"] = Decimal("1")  # type: ignore[index]


async def test_snapshot_projects_all_symbols_and_entity_identity(
    sqlite_session: AsyncSession,
) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")

    result = await store.append(sqlite_session, _snapshot())
    assert result is True
    await sqlite_session.flush()

    offers = (await sqlite_session.execute(select(VenueOfferStateRow))).scalars().all()
    credits = (await sqlite_session.execute(select(VenueCreditStateRow))).scalars().all()
    positions = (
        await sqlite_session.execute(
            select(PositionStateRow).order_by(PositionStateRow.symbol)
        )
    ).scalars().all()
    observations = (
        await sqlite_session.execute(select(ReconcileObservationRow))
    ).scalars().all()

    assert {o.venue_offer_id for o in offers} == {"offer-fust", "offer-fbtc"}
    assert {c.credit_id for c in credits} == {"credit-fust"}
    assert [p.symbol for p in positions] == ["fBTC", "fUST"]
    fbtc, fust = positions
    assert fbtc.offered_amount == Decimal("3")
    assert fbtc.lent_amount == Decimal("0")
    assert fbtc.available_amount == Decimal("4")
    assert fust.offered_amount == Decimal("10")
    assert fust.lent_amount == Decimal("7")
    assert fust.available_amount == Decimal("25")
    assert fust.reserved == Decimal("10")  # transitional alias remains in sync
    assert fust.realized == Decimal("7")
    assert len(observations) == 2
    assert {o.symbol for o in observations} == {"fUST", "fBTC"}


async def test_rebuild_from_snapshot_event_is_deterministic(
    sqlite_session: AsyncSession,
) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, _snapshot())
    await sqlite_session.flush()

    await store.rebuild_snapshot_from_log(
        sqlite_session,
        account_id=_ACCOUNT,
        deployment_environment="ci",
    )
    await sqlite_session.flush()
    first = (
        await sqlite_session.execute(
            select(PositionStateRow).order_by(PositionStateRow.symbol)
        )
    ).scalars().all()
    first_projection = [
        (p.symbol, p.offered_amount, p.lent_amount, p.available_amount, p.last_event_seq)
        for p in first
    ]

    await store.rebuild_snapshot_from_log(
        sqlite_session,
        account_id=_ACCOUNT,
        deployment_environment="ci",
    )
    await sqlite_session.flush()
    second = (
        await sqlite_session.execute(
            select(PositionStateRow).order_by(PositionStateRow.symbol)
        )
    ).scalars().all()
    second_projection = [
        (p.symbol, p.offered_amount, p.lent_amount, p.available_amount, p.last_event_seq)
        for p in second
    ]
    assert second_projection == first_projection


async def test_complete_snapshot_closes_missing_venue_entities(
    sqlite_session: AsyncSession,
) -> None:
    """A complete active-object snapshot makes absent prior entities terminal."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, _snapshot(observed_at=2_000))
    await store.append(
        sqlite_session,
        replace(
            _snapshot(observed_at=3_000),
            offers=(),
            credits=(),
            wallet_available={},
        ),
    )
    await sqlite_session.flush()

    offers = (await sqlite_session.execute(select(VenueOfferStateRow))).scalars().all()
    credits = (await sqlite_session.execute(select(VenueCreditStateRow))).scalars().all()
    positions = {
        row.symbol: row
        for row in (await sqlite_session.execute(select(PositionStateRow))).scalars().all()
    }
    assert {row.status for row in offers} == {"absent"}
    assert all(row.is_terminal for row in offers)
    assert {row.status for row in credits} == {"absent"}
    assert all(row.is_terminal for row in credits)
    assert positions["fUST"].offered_amount == Decimal("0")
    assert positions["fUST"].lent_amount == Decimal("0")


async def test_incomplete_snapshot_preserves_unobserved_exposure(
    sqlite_session: AsyncSession,
) -> None:
    """Partial endpoint coverage cannot turn an unknown dimension into zero."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    first = _snapshot(observed_at=2_000)
    await store.append(sqlite_session, first)
    partial_coverage = replace(first.coverage, active_offers_complete=False, active_offer_pages=0)
    await store.append(
        sqlite_session,
        replace(
            _snapshot(observed_at=3_000),
            offers=(),
            coverage=partial_coverage,
        ),
    )
    await sqlite_session.flush()

    positions = {
        row.symbol: row
        for row in (await sqlite_session.execute(select(PositionStateRow))).scalars().all()
    }
    assert positions["fUST"].offered_amount == Decimal("10")
    assert positions["fBTC"].offered_amount == Decimal("3")


async def test_newer_snapshot_cannot_rewind_object_timestamp(
    sqlite_session: AsyncSession,
) -> None:
    """A per-object stale venue update cannot lower aggregate exposure."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    first = _snapshot(observed_at=2_000)
    await store.append(sqlite_session, first)
    stale_offer = replace(
        first.offers[0],
        amount_remaining=Decimal("1"),
        mts_updated=first.offers[0].mts_updated - 100,
    )
    stale_credit = replace(
        first.credits[0],
        amount=Decimal("1"),
        mts_updated=(first.credits[0].mts_updated or 0) - 100,
    )
    await store.append(
        sqlite_session,
        replace(
            first,
            query_started_at_ms=2_900,
            query_finished_at_ms=3_000,
            offers=(stale_offer, first.offers[1]),
            credits=(stale_credit,),
            occurred_at_ms=3_000,
            event_id=uuid4(),
        ),
    )
    await sqlite_session.flush()

    positions = {
        row.symbol: row
        for row in (await sqlite_session.execute(select(PositionStateRow))).scalars().all()
    }
    assert positions["fUST"].offered_amount == Decimal("10")
    assert positions["fUST"].lent_amount == Decimal("7")


async def test_quarantine_event_is_idempotent_by_venue_offer(
    sqlite_session: AsyncSession,
) -> None:
    """Repeated reconcile ticks keep one durable orphan breadcrumb."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    event = VenueOfferQuarantined(
        venue_offer_id="orphan-1",
        symbol="fUST",
        amount=Decimal("4"),
        account_id=_ACCOUNT,
        observed_at_ms=2_000,
    )
    await store.append(sqlite_session, event)
    await store.append(sqlite_session, replace(event, observed_at_ms=3_000, event_id=uuid4()))
    await sqlite_session.flush()

    rows = (
        await sqlite_session.execute(
            select(EventLogRow).where(EventLogRow.event_type == "VENUE_OFFER_QUARANTINED")
        )
    ).scalars().all()
    assert len(rows) == 1


def test_active_wire_models_expose_normalized_observation_fields() -> None:
    offer = ActiveFundingOffer(
        venue_offer_id="1",
        symbol="fUST",
        amount=Decimal("2"),
        rate=0.0003,
        period_days=2,
        mts_created=1,
        status="ACTIVE",
        amount_original=Decimal("3"),
        mts_updated=2,
    )
    credit = ActiveFundingCredit(
        credit_id="2",
        symbol="fUST",
        amount=Decimal("4"),
        rate=0.0004,
        period_days=2,
        status="ACTIVE",
        mts_created=3,
        mts_updated=4,
    )
    assert offer.amount_original == Decimal("3")
    assert offer.mts_updated == 2
    assert credit.mts_created == 3
    assert credit.mts_updated == 4


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[object]:
        return []


class _Session:
    async def execute(self, _statement: object) -> _EmptyResult:
        return _EmptyResult()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _SessionContext:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SessionFactory:
    def __call__(self) -> _SessionContext:
        return _SessionContext()


class _Bus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class _Auth:
    def __init__(self) -> None:
        self.offer_symbols: list[str | None] = []
        self.credit_symbols: list[str | None] = []

    async def get_active_funding_offers(self, *, ctx: AccountContext, symbol: str | None = None) -> list[ActiveFundingOffer]:
        self.offer_symbols.append(symbol)
        return [
            ActiveFundingOffer(
                venue_offer_id="unknown-symbol-offer",
                symbol="fBTC",
                amount=Decimal("2"),
                amount_original=Decimal("2"),
                rate=0.0002,
                period_days=7,
                mts_created=1,
                mts_updated=2,
                status="ACTIVE",
            )
        ]

    async def get_active_funding_loans(self, **kwargs):
        # Lent but not yet drawn into a position; none in this fixture.
        return []

    async def get_active_funding_credits(self, *, ctx: AccountContext, symbol: str | None = None) -> list[ActiveFundingCredit]:
        self.credit_symbols.append(symbol)
        return []

    async def get_funding_available_all(self, *, ctx: AccountContext) -> dict[str, Decimal]:
        return {"fBTC": Decimal("5")}


class _Store:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def append(self, _session: object, event: object) -> bool:
        self.events.append(event)
        return True

    async def append_snapshot(self, _session: object, event: VenueSnapshotObserved) -> object:
        self.events.append(event)
        from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift

        return SnapshotDrift(Decimal("0"), Decimal("0"))


@pytest.mark.asyncio
async def test_boot_reconcile_queries_full_account_and_keeps_unknown_symbol() -> None:
    auth = _Auth()
    store = _Store()
    bus = _Bus()
    recovery = BootRecovery(
        store=store, session_factory=_SessionFactory(), auth_rest=auth,
        account_ctx=AccountContext(
            account_id=_ACCOUNT,
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("100"),
        ),
        deployment_environment="ci", bus=bus, symbols=["fUST"],
        max_attempts=1, backoff_base_s=0, clock=lambda: 2_000,
    )

    result = await recovery.run()

    assert auth.offer_symbols == [None]
    assert auth.credit_symbols == [None]
    snapshots = [event for event in store.events if isinstance(event, VenueSnapshotObserved)]
    assert len(snapshots) == 1
    assert snapshots[0].offers[0].symbol == "fBTC"
    assert result.n_quarantined == 1
    assert {event.symbol for event in bus.events} == {"fBTC", "fUST"}
