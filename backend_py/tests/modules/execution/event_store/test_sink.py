from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.sink import PostgresEventSink
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.events import OrderFilled, ReservationClaimed

_SCID = UUID("11111111-1111-1111-1111-111111111111")


async def _factory() -> async_sessionmaker[AsyncSession]:
    # Use StaticPool so all sessions share the same in-memory SQLite connection,
    # preventing "no such table" errors when separate sessions open fresh connections.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_sink_persists_claim_and_fill() -> None:
    factory = await _factory()
    store = PostgresEventStore(deployment_environment="ci")
    sink = PostgresEventSink(store=store, session_factory=factory)

    await sink.on_reservation_claimed(ReservationClaimed(
        cid=1, venue_offer_id="v1", size_usdt=Decimal("10"), signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, venue_seq=1, occurred_at_ms=1000))
    await sink.on_order_filled(OrderFilled(
        cid=1, venue_offer_id="v1", credit_id="c1", size_usdt=Decimal("4"), fill_rate=0.0,
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=2, occurred_at_ms=2000))

    async with factory() as s:
        rows = (await s.execute(select(EventLogRow))).scalars().all()
        assert len(rows) == 2
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "acct"))).scalar_one()
        assert ps.reserved_usdt == Decimal("6")
        assert ps.realized_usdt == Decimal("4")
