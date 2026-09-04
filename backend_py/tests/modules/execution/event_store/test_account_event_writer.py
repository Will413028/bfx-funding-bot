from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    ProjectionHeadRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import (
    AccountEventWriter,
    ProjectionWriteError,
)
from bfx_funding_bot.modules.execution.events import ReservationClaimed

_ENV = "ci"
_ACCOUNT = UUID("00000000-0000-0000-0000-00000000a101")
_SCID = UUID("11111111-1111-1111-1111-111111111101")


async def _create_schema(session: AsyncSession) -> None:
    bind = session.bind
    assert bind is not None
    async with bind.begin() as connection:  # type: ignore[union-attr]
        await connection.run_sync(Base.metadata.create_all)
    session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="writer-test"))
    await session.commit()


def _claimed(
    *,
    cid: int,
    signal_correlation_id: UUID = _SCID,
    venue_seq: int,
    amount: str = "10",
) -> ReservationClaimed:
    return ReservationClaimed(
        cid=cid,
        venue_offer_id=f"offer-{cid}",
        amount=Decimal(amount),
        signal_correlation_id=signal_correlation_id,
        account_id=str(_ACCOUNT),
        is_simulated=True,
        venue_seq=venue_seq,
        occurred_at_ms=1_000 + venue_seq,
        symbol="fUST",
        reservation_ref=ReservationRef(
            execution_decision_id=f"decision-{cid}",
            cid=cid,
            signal_correlation_id=signal_correlation_id,
            venue_offer_id=f"offer-{cid}",
        ),
    )


@pytest.mark.asyncio
async def test_append_updates_claim_and_account_projection_head(
    sqlite_session: AsyncSession,
) -> None:
    await _create_schema(sqlite_session)
    writer = AccountEventWriter(
        store=PostgresEventStore(deployment_environment=_ENV),
        projector_version="execution-state-v1",
    )

    result = await writer.append(sqlite_session, _claimed(cid=1, venue_seq=1))

    assert result.persisted is True
    assert result.event_seq > 0
    assert result.projection_head == result.event_seq

    head = await sqlite_session.scalar(
        select(ProjectionHeadRow).where(
            ProjectionHeadRow.exchange_account_id == _ACCOUNT,
            ProjectionHeadRow.deployment_environment == _ENV,
            ProjectionHeadRow.projection_name == "execution_state",
        )
    )
    claim = await sqlite_session.scalar(
        select(OfferClaimRow).where(
            OfferClaimRow.exchange_account_id == _ACCOUNT,
            OfferClaimRow.deployment_environment == _ENV,
            OfferClaimRow.cid == 1,
        )
    )
    assert head is not None and head.last_event_seq == result.event_seq
    assert claim is not None and claim.last_event_seq == result.event_seq


@pytest.mark.asyncio
async def test_duplicate_append_returns_existing_sequence_without_new_row(
    sqlite_session: AsyncSession,
) -> None:
    await _create_schema(sqlite_session)
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))
    event = _claimed(cid=2, venue_seq=2)

    first = await writer.append(sqlite_session, event)
    second = await writer.append(sqlite_session, event)
    await sqlite_session.commit()

    count = await sqlite_session.scalar(select(func.count()).select_from(EventLogRow))
    assert first.persisted is True
    assert second.persisted is False
    assert second.event_seq == first.event_seq
    assert second.projection_head == first.projection_head
    assert count == 1


@pytest.mark.asyncio
async def test_duplicate_older_event_does_not_move_projection_head_backwards(
    sqlite_session: AsyncSession,
) -> None:
    await _create_schema(sqlite_session)
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))
    first_event = _claimed(cid=20, venue_seq=20)
    second_event = _claimed(cid=21, venue_seq=21)

    first = await writer.append(sqlite_session, first_event)
    second = await writer.append(sqlite_session, second_event)
    duplicate = await writer.append(sqlite_session, first_event)

    assert first.persisted is True
    assert second.persisted is True
    assert duplicate.persisted is False
    assert duplicate.event_seq == first.event_seq
    assert duplicate.projection_head == second.event_seq


@pytest.mark.asyncio
async def test_append_replays_event_log_gap_before_advancing_projection_head(
    sqlite_session: AsyncSession,
) -> None:
    await _create_schema(sqlite_session)
    store = PostgresEventStore(deployment_environment=_ENV)
    writer = AccountEventWriter(store=store)
    historical = _claimed(cid=30, venue_seq=30)
    historical_payload = serialize_event(historical)
    sqlite_session.add(
        EventLogRow(
            account_id=str(_ACCOUNT),
            exchange_account_id=_ACCOUNT,
            deployment_environment=_ENV,
            event_type="RESERVATION_CLAIMED",
            cid=historical.cid,
            venue_offer_id=historical.venue_offer_id,
            venue_seq=historical.venue_seq,
            event_id=historical.event_id,
            schema_version=historical.schema_version,
            payload=historical_payload,
            occurred_at_ms=historical.occurred_at_ms or 0,
        )
    )
    await sqlite_session.flush()

    current = _claimed(cid=31, venue_seq=31)
    result = await writer.append(sqlite_session, current)

    claim_cids = (
        await sqlite_session.execute(
            select(OfferClaimRow.cid).order_by(OfferClaimRow.cid.asc())
        )
    ).scalars().all()
    assert result.persisted is True
    assert claim_cids == [30, 31]
    assert result.projection_head == result.event_seq


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_event_claim_and_head(
    sqlite_session: AsyncSession,
) -> None:
    await _create_schema(sqlite_session)
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))
    await writer.append(sqlite_session, _claimed(cid=3, venue_seq=3))

    conflicting = _claimed(
        cid=3,
        signal_correlation_id=UUID("22222222-2222-2222-2222-222222222203"),
        venue_seq=4,
    )
    with pytest.raises(ProjectionWriteError):
        await writer.append(sqlite_session, conflicting)
    await sqlite_session.rollback()

    assert await sqlite_session.scalar(select(func.count()).select_from(EventLogRow)) == 0
    assert await sqlite_session.scalar(select(func.count()).select_from(OfferClaimRow)) == 0
    assert await sqlite_session.scalar(select(func.count()).select_from(ProjectionHeadRow)) == 0


@pytest.mark.asyncio
async def test_append_batch_orders_by_domain_time_and_advances_head(
    sqlite_session: AsyncSession,
) -> None:
    await _create_schema(sqlite_session)
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))

    late = _claimed(cid=4, venue_seq=50)
    early = _claimed(cid=5, venue_seq=40)
    results = await writer.append_batch(
        sqlite_session,
        [late, early],
    )

    assert [result.persisted for result in results] == [True, True]
    assert results[0].event_seq < results[1].event_seq
    assert results[-1].projection_head == results[-1].event_seq
    rows = (
        await sqlite_session.execute(
            select(EventLogRow).order_by(EventLogRow.event_seq.asc())
        )
    ).scalars().all()
    assert [row.cid for row in rows] == [5, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["c2e3f4a5b6c7", "cd5e6f708192"])
async def test_projector_migration_readiness_accepts_seeded_revisions(
    sqlite_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    revision: str,
) -> None:
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))

    async def current_revision(_session: AsyncSession) -> tuple[str, ...]:
        return (revision,)

    monkeypatch.setattr(writer, "_database_migration_revisions", current_revision)

    await writer._assert_projector_migration_ready(sqlite_session)


@pytest.mark.asyncio
async def test_projector_migration_readiness_rejects_pre_seed_revision(
    sqlite_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))

    async def intermediate_revision(_session: AsyncSession) -> tuple[str, ...]:
        return ("bc4d5e6f7081",)

    monkeypatch.setattr(writer, "_database_migration_revisions", intermediate_revision)

    with pytest.raises(ValueError, match="cursor migration incomplete"):
        await writer._assert_projector_migration_ready(sqlite_session)
