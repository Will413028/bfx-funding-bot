from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationReleased,
)

_SCID = UUID("11111111-1111-1111-1111-111111111111")


async def _create_all(session: AsyncSession) -> None:
    bind = session.bind
    assert bind is not None
    async with bind.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.create_all)


def _claimed(seq: int) -> ReservationClaimed:
    return ReservationClaimed(cid=100 + seq, venue_offer_id=f"v{seq}", size_usdt=Decimal("5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=seq, occurred_at_ms=1000 + seq)


async def test_append_inserts_event_row(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, _claimed(1))
    await sqlite_session.flush()
    rows = (await sqlite_session.execute(select(EventLogRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "RESERVATION_CLAIMED"
    assert rows[0].cid == 101
    assert rows[0].deployment_environment == "ci"
    assert rows[0].payload["size_usdt"] == "5"


async def test_claim_then_release_updates_offer_claims(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, _claimed(5))           # cid 105, v5
    await sqlite_session.flush()
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 105))).scalar_one()
    assert row.state == "claimed"
    assert row.venue_offer_id == "v5"

    await store.append(sqlite_session, ReservationReleased(cid=105, venue_offer_id="v5",
        size_usdt=Decimal("5"), reason="venue_cancel", signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, venue_seq=6, occurred_at_ms=2000))
    await sqlite_session.flush()
    row2 = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 105))).scalar_one()
    assert row2.state == "released"


async def test_append_fill_dedup_skips_duplicate(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    fill = OrderFilled(cid=200, venue_offer_id="v9", credit_id=None, size_usdt=Decimal("2"),
        fill_rate=0.0, signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=9, occurred_at_ms=2000)
    inserted_first = await store.append(sqlite_session, fill)
    inserted_second = await store.append(sqlite_session, fill)  # same (voi, venue_seq)
    await sqlite_session.flush()
    count = (await sqlite_session.execute(
        select(func.count()).select_from(EventLogRow))).scalar_one()
    assert inserted_first is True
    assert inserted_second is False
    assert count == 1


async def test_intent_creates_pending_claim_with_null_voi(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=300, size_usdt=Decimal("8"), signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, occurred_at_ms=1000))
    await sqlite_session.flush()
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 300))).scalar_one()
    assert row.state == "pending"
    assert row.venue_offer_id is None
    assert row.size_usdt == Decimal("8")


async def test_intent_then_claimed_updates_same_cid_row(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=301, size_usdt=Decimal("8"), signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, occurred_at_ms=1000))
    await store.append(sqlite_session, ReservationClaimed(
        cid=301, venue_offer_id="v301", size_usdt=Decimal("8"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=1, occurred_at_ms=1100))
    await sqlite_session.flush()
    rows = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 301))).scalars().all()
    assert len(rows) == 1  # cid-keyed: PENDING row promoted in place, not a 2nd row
    assert rows[0].state == "claimed"
    assert rows[0].venue_offer_id == "v301"


async def test_intent_then_failed_marks_failed_reserved_untouched(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=302, size_usdt=Decimal("8"), signal_correlation_id=_SCID,
        account_id="acctF", is_simulated=True, occurred_at_ms=1000))
    await store.append(sqlite_session, ReservationFailed(
        cid=302, size_usdt=Decimal("8"), signal_correlation_id=_SCID,
        account_id="acctF", is_simulated=True, reason="submit_failed",
        occurred_at_ms=1100))
    await sqlite_session.flush()
    claim = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 302))).scalar_one()
    assert claim.state == "failed"
    ps = (await sqlite_session.execute(
        select(PositionStateRow).where(PositionStateRow.account_id == "acctF"))).scalar_one()
    assert ps.reserved_usdt == Decimal("0")   # FAILED never reserved capital
    assert ps.last_event_seq > 0              # high-water mark still advances
