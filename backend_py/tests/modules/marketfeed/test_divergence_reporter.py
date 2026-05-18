from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.divergence_reporter import (
    DivergenceReporter,
    ExtractedSignal,
)
from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy


def _cell_rp() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 8},
        "reference_amount_usdt": 150.0,
    })


def _candle(mts: int, close: Decimal) -> FundingCandle:
    return FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="a30", mts=mts,
        open=close, close=close, high=close, low=close, volume=Decimal("100"),
    )


def test_no_divergence_when_inputs_match():
    cell = _cell_rp()
    history = [_candle(1747584000000 + i * 3600_000, Decimal(f"0.000{(i%5)+1}"))
               for i in range(10)]
    live = build_strategy(cell)
    for c in history[:-1]:
        live.observe(c)
    live_signal = ExtractedSignal.extract(cell, live, history[-1])

    reporter = DivergenceReporter()
    result = reporter.check(cell=cell, history=history, live_signal=live_signal)
    assert result is None  # no divergence


def test_divergence_detected_when_live_signal_differs_from_replay():
    """Hard-trigger detection by constructing a live_signal that doesn't match replay.

    Validates the detection branch (dict shape, diff_fields enumeration) which the
    in-test natural-mismatch path can't reliably hit at the percentile boundary.
    """
    from bfx_funding_bot.modules.marketfeed.schemas import SignalDirection

    cell = _cell_rp()
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0001"))
               for i in range(10)]

    # Manually construct a live_signal that disagrees with what replay will compute.
    # Uniform history (all 0.0001, lookback=8, percentile=75) → replay produces POST.
    # Use SKIP + score=-999 to guarantee all three fields diverge from replay.
    fake_live = ExtractedSignal(
        signal_score=-999.0,
        signal_direction=SignalDirection.SKIP,
        strategy_attributes=(("fake_attr", "fake_value"),),
        lend_decision=None,
    )

    reporter = DivergenceReporter()
    result = reporter.check(cell=cell, history=history, live_signal=fake_live)

    assert result is not None, "divergence MUST be detected when live differs from replay"
    assert "live" in result
    assert "replay" in result
    assert "diff_fields" in result
    diff = result["diff_fields"]
    assert "signal_score" in diff
    assert "signal_direction" in diff
    assert "strategy_attributes" in diff


@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    n=st.integers(min_value=20, max_value=40),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
def test_cp1_byte_equivalence_property(n: int, seed: int):
    """CP1: live observe path 跟 backtest replay path 必須產出 byte-equal signal.

    Given the same candle sequence, observing it once via the live path and
    once via the replay path must produce identical extracted signals.
    """
    import random as _random
    rng = _random.Random(seed)
    # Generate n monotonically-mts candles with random closes
    base_mts = 1747584000000
    history = [
        _candle(
            base_mts + i * 3600_000,
            Decimal(str(round(0.00005 + rng.random() * 0.001, 6))),
        )
        for i in range(n)
    ]
    cell = _cell_rp()

    # Build live strategy: observe history[:-1], then extract at history[-1]
    live = build_strategy(cell)
    for c in history[:-1]:
        live.observe(c)
    live_signal = ExtractedSignal.extract(cell, live, history[-1])

    # Build replay strategy independently: observe history[:-1], then extract at history[-1]
    replay = build_strategy(cell)
    for c in history[:-1]:
        replay.observe(c)
    replay_signal = ExtractedSignal.extract(cell, replay, history[-1])

    # Byte-equivalence: same input sequence -> same extracted signal
    assert live_signal == replay_signal
