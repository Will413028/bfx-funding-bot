"""A restart must not cost an interval of lending.

`StandingQuoteStore` is in-memory and the scheduler arms the NEXT boundary, so
before this a process that came up at 07:01 deployed nothing until 08:00. That
also made a deployment and its release ceremony mutually exclusive: the DR
receipt the ceremony needs lives 900 seconds, and refreshing it costs a restart
that costs up to an hour of quote. Five ceremony windows were opened on
2026-09-20 and four expired; that is the same clock.

What the boot replay must get right is *which* boundary and *what age*: the one
the scheduler skips, dated to the boundary it speaks for rather than to boot.
"""
from unittest.mock import AsyncMock, MagicMock

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.marketfeed.scheduler import (
    _TIMEFRAME_MS,
    last_candle_close_mts,
    next_candle_close_mts,
)
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    StrategyRegistry,
    build_strategy,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome
from tests.modules.marketfeed.test_signal_engine import _cell, _history

_HOUR = _TIMEFRAME_MS["1h"]


def test_last_close_is_the_boundary_the_scheduler_skips() -> None:
    """register_from_now arms the next boundary; this names the one it leaves out."""
    now = 1_790_060_400_000 + 6 * 60_000  # 2026-09-22 07:06:00 UTC
    armed = next_candle_close_mts(timeframe="1h", now_ms=now)
    skipped = last_candle_close_mts(timeframe="1h", now_ms=now)
    assert skipped < now < armed
    assert armed - skipped == _HOUR


def test_last_close_on_an_exact_boundary_is_the_previous_one() -> None:
    """At 07:00:00 the scheduler will still fire 08:00, so 07:00 is ours."""
    now = 1_790_060_400_000
    assert now % _HOUR == 0
    assert last_candle_close_mts(timeframe="1h", now_ms=now) == now
    assert next_candle_close_mts(timeframe="1h", now_ms=now) == now + _HOUR


def test_every_timeframe_is_covered() -> None:
    """A cell on any configured timeframe must be replayable, not just 1h."""
    now = 1_790_060_461_000
    for timeframe, step in _TIMEFRAME_MS.items():
        skipped = last_candle_close_mts(timeframe=timeframe, now_ms=now)
        assert skipped <= now < skipped + step
        assert skipped % step == 0


def _engine(store: StandingQuoteStore, *, clock_ms: int) -> SignalEngine:
    axiom = MagicMock()
    axiom.emit = AsyncMock()
    diagnostics = MagicMock()
    diagnostics.emit = AsyncMock()
    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))
    return SignalEngine(
        phase=Phase.PAPER, event_sink=axiom, diagnostics=diagnostics,
        candles_repo=candles_repo, quote_store=store, clock=lambda: clock_ms,
    )


def _warmed_registry(cell) -> StrategyRegistry:
    reg = StrategyRegistry()
    strategy = build_strategy(cell)
    for candle in _history(7):
        strategy.observe(candle)
    reg.put(cell, strategy)
    return reg


async def test_replayed_quote_is_dated_to_its_boundary_not_to_boot() -> None:
    """A quote replayed at 07:59 speaks for 07:00 and must expire on 07:00's clock.

    Dating it "now" would hand a boundary that had already spent 59 minutes of
    its TTL a fresh one, and the reconciler would deploy against an hour-old
    signal believing it was current.
    """
    boundary = 1_790_060_400_000
    booted_at = boundary + 59 * 60_000
    store = StandingQuoteStore(ttl_ms=3_900_000)
    cell = _cell()

    await _engine(store, clock_ms=booted_at).process_candle(
        cell=cell, candle=_history(8)[-1], registry=_warmed_registry(cell),
        quote_created_at_ms=boundary,
    )

    quote = store.get_active(cell.cell_id, now_ms=booted_at)
    assert quote is not None and quote.outcome == DecisionOutcome.POST
    assert quote.created_at_ms == boundary
    # Honest age: expires 65 minutes after the boundary, not after boot.
    assert store.get_active(cell.cell_id, now_ms=boundary + 3_900_000) is not None
    assert store.get_active(cell.cell_id, now_ms=boundary + 3_900_001) is None


async def test_a_live_tick_still_dates_the_quote_now() -> None:
    """The default is unchanged; only the replay path passes a boundary."""
    store = StandingQuoteStore(ttl_ms=3_900_000)
    cell = _cell()
    await _engine(store, clock_ms=5_000).process_candle(
        cell=cell, candle=_history(8)[-1], registry=_warmed_registry(cell),
    )
    quote = store.get_active(cell.cell_id, now_ms=5_000)
    assert quote is not None and quote.created_at_ms == 5_000
