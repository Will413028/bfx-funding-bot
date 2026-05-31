"""from_snapshot rebuilds multiple per-symbol buckets (Phase 1)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


@pytest.fixture
async def session() -> AsyncSession:  # type: ignore[misc]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_from_snapshot_loads_multiple_symbol_rows(session: AsyncSession) -> None:
    session.add_all([
        PositionStateRow(
            account_id="default", deployment_environment="prod", symbol="fUST",
            reserved=Decimal("10"), realized=Decimal("440"),
            last_updated_ms=1, last_event_seq=1,
        ),
        PositionStateRow(
            account_id="default", deployment_environment="prod", symbol="fUSD",
            reserved=Decimal("3"), realized=Decimal("90"),
            last_updated_ms=1, last_event_seq=1,
        ),
        # different env — must be ignored
        PositionStateRow(
            account_id="default", deployment_environment="shadow", symbol="fUST",
            reserved=Decimal("999"), realized=Decimal("999"),
            last_updated_ms=1, last_event_seq=1,
        ),
    ])
    await session.commit()

    led = await PaperPositionLedger.from_snapshot(
        session, account_id="default", deployment_environment="prod",
    )
    assert led.reserved_exposure("fUST") == Decimal("10")
    assert led.realized_exposure("fUST") == Decimal("440")
    assert led.reserved_exposure("fUSD") == Decimal("3")
    assert led.realized_exposure("fUSD") == Decimal("90")
    # available is never persisted — 0 until first reconcile
    assert led.available_balance("fUST") == Decimal("0")


async def test_from_snapshot_empty_is_all_zero(session: AsyncSession) -> None:
    led = await PaperPositionLedger.from_snapshot(
        session, account_id="default", deployment_environment="prod",
    )
    assert led.reserved_exposure("fUST") == Decimal("0")
    assert led.realized_exposure("fUST") == Decimal("0")
