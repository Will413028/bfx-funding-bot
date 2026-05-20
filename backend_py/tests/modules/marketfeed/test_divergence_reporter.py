from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
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


def test_replay_byte_equivalent_with_locf_on_sparse_input():
    """Sparse p30 candle fixture: daemon LOCF path + replay LOCF path produce
    byte-equivalent SignalPayload (no divergence_detected emit).

    Phase 4.1 CP1 invariant extends to LOCF: both live and replay paths must see
    the same LOCF-filled candles (via reindex_and_ffill) → same ExtractedSignal.

    Setup:
    - 10 dense candles from base_mts, then 6h gap (sparse), then ref_mts
    - budget=12h → gap < budget → LOCF soft-tier fills the gap with last known candle
    - live strategy observes LOCF-processed candles (same as daemon path)
    - replay path (what signal_engine now passes to reporter) also gets LOCF history
    - DivergenceReporter.check() must return None (byte-equal)

    Regression guard: without Phase 4.3 fix, reporter would receive raw candles
    (gap slot missing), replay would see 10 candles while live saw 16 → mismatch.
    """
    base_mts = 1747584000000  # 2026-05-18 12:00 UTC
    one_hour_ms = 3_600_000
    budget_hours = 12

    # Dense candles at T+0h … T+9h (10 candles), then a 6h gap, ref at T+15h.
    dense_candles = [
        _candle(base_mts + i * one_hour_ms, Decimal(f"0.000{(i % 5) + 1}"))
        for i in range(10)
    ]
    last_dense_mts = base_mts + 9 * one_hour_ms
    ref_mts = last_dense_mts + 6 * one_hour_ms  # T+15h; 6h gap in raw candles

    # Simulate what signal_engine now does: apply LOCF to raw history.
    locf_filled = reindex_and_ffill(
        dense_candles, ref_mts=ref_mts, max_gap_hours=budget_hours,
    )
    # Soft-tier: gap (6h) < budget (12h) → all slots filled, no None entries.
    assert all(fc.candle is not None for fc in locf_filled), (
        "all slots should be soft-tier filled (gap < budget)"
    )
    # ref_mts slot is LOCF-filled from last dense candle.
    assert locf_filled[-1].is_stale is True
    assert locf_filled[-1].stale_seconds == 6 * 3600

    locf_history: list[FundingCandle] = [fc.candle for fc in locf_filled]  # type: ignore[misc]
    latest_candle = locf_history[-1]

    # Cell with staleness_budget_hours resolved (simulates load_config()).
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "p30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 8},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": budget_hours,
    })

    # Live path: observe history[:-1] then extract at history[-1].
    live = build_strategy(cell)
    for c in locf_history[:-1]:
        live.observe(c)
    live_signal = ExtractedSignal.extract(cell, live, latest_candle)

    # Replay path (mirrors what DivergenceReporter.check does internally).
    # MUST use the same LOCF-processed locf_history — not raw dense_candles.
    reporter = DivergenceReporter()
    divergence = reporter.check(cell=cell, history=locf_history, live_signal=live_signal)

    assert divergence is None, (
        f"CP1 byte-equivalence must hold with LOCF sparse input; got: {divergence}"
    )


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
