from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.marketfeed.warmup import warmup_cell, warmup_ref_mts
from bfx_funding_bot.modules.strategy import CellConfig
from bfx_funding_bot.modules.strategy.wiring import build_strategy, build_strategy_at_boundary


def _cell() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })


async def test_warmup_feeds_strategy_with_db_candles(sqlite_session: AsyncSession):
    async with sqlite_session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    candles = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=1747584000000 + i * 3600_000,
            open=Decimal("0.0001"),
            close=Decimal(f"0.0001{i}").quantize(Decimal("0.00001")),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for i in range(5)
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    bfx = AsyncMock()
    bfx.get_funding_candles = AsyncMock(return_value=[])
    reg = StrategyRegistry(build_strategy)
    cell = _cell()

    result = await warmup_cell(
        cell=cell, registry=reg, boundary_builder=build_strategy_at_boundary,
        bitfinex=bfx, session=sqlite_session,
        now_mts=1747584000000 + 5 * 3600_000,
    )

    # Slot 4 just closed: the boot rehydrate tick observes it, so warmup stops at 3.
    assert result.observed_count == 4
    assert reg.get(cell) is not None


async def test_warmup_locf_symmetry_for_sparse_cell(
    sqlite_session: AsyncSession,
) -> None:
    """Phase 4.3 LOCF symmetry: warmup must apply reindex_and_ffill so the live
    strategy state at first post-deploy tick matches what replay (divergence
    reporter) computes. Without this, sparse cells (e.g. fUSD_p30 ~1/3 density)
    have CP1 divergence on first tick — daemon state (raw history) vs replay
    state (LOCF history) diverge, self-heals only after multiple LOCF
    observations accumulate.

    Regression for 5/20 prod deploy 003c9502 10:00 UTC fUSD_p30 MR divergence.
    """
    async with sqlite_session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    base_mts = 1747584000000
    # Sparse fixture: 10 hourly slots, candles only at slots 0/3/6/9 (1/3 density,
    # mimicking p30 cell sparsity on Bitfinex).
    sparse_candles = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="p30",
            mts=base_mts + i * 3600_000,
            open=Decimal(f"0.001{i}"), close=Decimal(f"0.001{i}"),
            high=Decimal(f"0.001{i}"), low=Decimal(f"0.001{i}"),
            volume=Decimal("100"),
        )
        for i in (0, 3, 6, 9)
    ]
    await upsert_candles(sqlite_session, sparse_candles)
    await sqlite_session.commit()

    bfx = AsyncMock()
    bfx.get_funding_candles = AsyncMock(return_value=[])
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "p30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 10},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 12,
    })

    reg = StrategyRegistry(build_strategy)
    now_mts = base_mts + 10 * 3600_000  # slot 10 boundary (no candle here yet)
    await warmup_cell(
        cell=cell, registry=reg, boundary_builder=build_strategy_at_boundary, bitfinex=bfx,
        session=sqlite_session, now_mts=now_mts,
    )

    # Construct the reference state replay would build at the first tick, the
    # boot rehydrate of the candle that just closed. Apply LOCF with the SAME
    # parameters warmup must use, then observe filled[:-1] — the last slot is
    # the candle that tick delivers to signal_engine.extract().
    filled = reindex_and_ffill(
        sparse_candles, ref_mts=warmup_ref_mts(cell, now_mts=now_mts), max_gap_hours=12,
    )
    replay = build_strategy(cell)
    for fc in filled[:-1]:
        if fc.candle is not None:
            replay.observe(fc.candle)

    warmup_strategy = reg.get(cell)
    assert warmup_strategy is not None
    # CP1 byte-equivalence proxy: same _window deque content (closes only).
    assert list(warmup_strategy._window) == list(replay._window), (  # type: ignore[attr-defined]
        f"warmup state diverged from LOCF-replay state: "
        f"warmup={list(warmup_strategy._window)} replay={list(replay._window)}"  # type: ignore[attr-defined]
    )


async def test_warmup_locf_dense_cell_no_change(
    sqlite_session: AsyncSession,
) -> None:
    """Regression: LOCF on fully dense data is identity (1-to-1 wrap, no fill
    applied per reindex_and_ffill invariant). Warmup observed_count for dense
    p2/a30 cells must remain identical to pre-Phase-4.3 raw-observation
    behavior — only the slot of the candle that just closed is dropped (the
    boot rehydrate tick observes it), not any other historical slot.
    """
    async with sqlite_session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    base_mts = 1747584000000
    dense_candles = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=base_mts + i * 3600_000,
            open=Decimal(f"0.000{i+1}"), close=Decimal(f"0.000{i+1}"),
            high=Decimal(f"0.000{i+1}"), low=Decimal(f"0.000{i+1}"),
            volume=Decimal("100"),
        )
        for i in range(5)
    ]
    await upsert_candles(sqlite_session, dense_candles)
    await sqlite_session.commit()

    bfx = AsyncMock()
    bfx.get_funding_candles = AsyncMock(return_value=[])
    reg = StrategyRegistry(build_strategy)
    cell = _cell()  # dense a30 cell, budget=2h

    # now_mts at slot 5 (no candle yet at slot 5 — matches realistic warmup
    # timing where boundary candle is the upcoming first-tick target).
    result = await warmup_cell(
        cell=cell, registry=reg, boundary_builder=build_strategy_at_boundary, bitfinex=bfx,
        session=sqlite_session, now_mts=base_mts + 5 * 3600_000,
    )

    # LOCF over slots 0..4 is identity; filled[:-1] drops slot 4, the candle
    # that just closed and that the boot rehydrate tick observes.
    assert result.observed_count == 4
