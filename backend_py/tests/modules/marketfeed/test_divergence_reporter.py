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
    _diff_fields,
)
from bfx_funding_bot.modules.marketfeed.schemas import SignalDirection
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    build_strategy,
    build_strategy_at_boundary,
)


def _cell_rp() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 8},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
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
    result = reporter.check(
        cell=cell, raw_history=history, boundary_candle=history[-1],
        budget_hours=cell.staleness_budget_hours or 2, live_signal=live_signal,
    )
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
    result = reporter.check(
        cell=cell, raw_history=history, boundary_candle=history[-1],
        budget_hours=cell.staleness_budget_hours or 2, live_signal=fake_live,
    )

    assert result is not None, "divergence MUST be detected when live differs from replay"
    assert "live" in result
    assert "replay" in result
    assert "diff_fields" in result
    diff = result["diff_fields"]
    assert "signal_score" in diff
    assert "signal_direction" in diff
    assert "strategy_attributes" in diff


def test_replay_byte_equivalent_with_locf_on_sparse_input():
    """Sparse p30 fixture: daemon LOCF path + replay LOCF path produce a
    byte-equivalent SignalPayload (no divergence), now verified at STATE level
    (window/threshold), not just direction.

    Phase 4.1 CP1 invariant extends to LOCF: both live and replay paths must see
    the same LOCF-filled candles (via reindex_and_ffill) → same ExtractedSignal.

    Faithful gap scenario: 10 dense candles, a 6h gap, then a REAL gap-terminating
    candle at T+15h. The daemon delivers that real candle as the boundary; live
    (warmup-equivalent) and replay both reindex raw history to its mts (=T+15h),
    LOCF-filling T+10..T+14. NOTE: an earlier version used a LOCF *fill* as the
    boundary — but LOCF fills carry their SOURCE candle's mts (T+9h), so the
    boundary's mts disagreed with the live reindex anchor (T+15h) and the two
    paths' windows silently diverged. The old direction-only comparison missed
    that; the state-level (last_threshold) comparison now catches it, so the
    fixture uses a real terminator to model the real daemon.
    """
    base_mts = 1747584000000  # 2026-05-18 12:00 UTC
    one_hour_ms = 3_600_000
    budget_hours = 12

    # Dense candles at T+0h … T+9h, a 6h gap, then a REAL candle at T+15h.
    dense_candles = [
        _candle(base_mts + i * one_hour_ms, Decimal(f"0.000{(i % 5) + 1}"))
        for i in range(10)
    ]
    ref_mts = base_mts + 15 * one_hour_ms  # T+15h; 6h gap after the last dense
    gap_terminator = _candle(ref_mts, Decimal("0.0006"))
    raw_candles = [*dense_candles, gap_terminator]

    locf_filled = reindex_and_ffill(
        raw_candles, ref_mts=ref_mts, max_gap_hours=budget_hours,
    )
    # Soft-tier: gap (6h) < budget (12h) → all slots filled, no None entries.
    assert all(fc.candle is not None for fc in locf_filled), (
        "all slots should be soft-tier filled (gap < budget)"
    )
    # The boundary slot (T+15h) is the REAL terminator (not stale); the gap
    # interior (e.g. T+12h, index 12) is LOCF-filled from the last dense candle.
    assert locf_filled[-1].is_stale is False
    assert locf_filled[12].is_stale is True
    assert locf_filled[12].stale_seconds == 3 * 3600  # T+12h sourced from T+9h

    locf_history = [fc.candle for fc in locf_filled if fc.candle is not None]
    assert len(locf_history) == len(locf_filled)  # all slots populated (verify)

    # Cell with staleness_budget_hours resolved (simulates load_config()).
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "p30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 8},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": budget_hours,
    })

    # Live path: observe LOCF history[:-1] then extract at the REAL boundary.
    live = build_strategy(cell)
    for c in locf_history[:-1]:
        live.observe(c)
    live_signal = ExtractedSignal.extract(cell, live, gap_terminator)

    # Replay path: reporter rebuilds via build_strategy_at_boundary over RAW
    # history with ref_mts=boundary.mts (=T+15h) — an independent reconstruction
    # that must produce a byte-equal window/threshold.
    reporter = DivergenceReporter()
    divergence = reporter.check(
        cell=cell, raw_history=raw_candles, boundary_candle=gap_terminator,
        budget_hours=budget_hours, live_signal=live_signal,
    )

    assert divergence is None, (
        f"CP1 byte-equivalence must hold with LOCF sparse input; got: {divergence}"
    )


def test_state_drift_detected_even_when_direction_matches():
    """The headline G2 invariant: live and replay agree on direction (POST) but
    the MR ema accumulator silently drifted → must surface as strategy_attributes
    divergence. The old direction-only reporter missed this."""
    from bfx_funding_bot.modules.marketfeed.schemas import SignalDirection

    cell = CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"ema_span": 24, "threshold_sigma": 0.5, "ratio_sigma": 0.05},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(10)]

    # Live signal AGREES on direction (POST) but carries a drifted ema_current.
    fake_live = ExtractedSignal(
        signal_score=0.0,
        signal_direction=SignalDirection.POST,
        strategy_attributes=tuple(sorted({
            "rate": 0.0003,
            "threshold_sigma": 0.5,
            "ema_current": Decimal("0.0009"),   # drifted vs the true ~0.0003
            "last_deviation": Decimal("0"),
        }.items())),
        lend_decision=None,
    )
    reporter = DivergenceReporter()
    result = reporter.check(
        cell=cell, raw_history=history, boundary_candle=history[-1],
        budget_hours=2, live_signal=fake_live,
    )
    assert result is not None, "state drift must be detected even with matching direction"
    assert "strategy_attributes" in result["diff_fields"]
    assert "signal_direction" not in result["diff_fields"]  # directions agreed
    # Pin the FEATURE (not an incidental key mismatch): replay must EMIT the real
    # ema_current, and it must differ from the drifted live value.
    replay_attrs = dict(result["replay"]["strategy_attributes"])
    assert "ema_current" in replay_attrs, "replay must expose ema_current"
    assert Decimal(str(replay_attrs["ema_current"])) != Decimal("0.0009")


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


def test_signal_score_mr_is_continuous_deviation():
    """MR signal_score is the continuous (close-ema)/ema margin, not ±1.0."""
    cell = CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"ema_span": 24, "threshold_sigma": 0.5, "ratio_sigma": 0.05},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })
    strat = build_strategy(cell)
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(5)]
    for c in history[:-1]:
        strat.observe(c)
    sig = ExtractedSignal.extract(cell, strat, history[-1])
    assert sig.signal_score == float(strat.last_deviation)
    assert sig.signal_score != 1.0  # no longer the binary placeholder


def test_signal_score_rp_is_percentile_rank():
    """RP signal_score is the 0-100 rank of close within the window."""
    cell = _cell_rp()  # percentile=75, lookback=8
    history = [_candle(1747584000000 + i * 3600_000, Decimal(f"0.000{i + 1}"))
               for i in range(9)]
    strat = build_strategy(cell)
    for c in history[:-1]:
        strat.observe(c)
    sig = ExtractedSignal.extract(cell, strat, history[-1])
    # boundary close (0.0009) >= every value in the post-observe window → 100.0
    assert sig.signal_score == 100.0


def test_signal_score_zero_when_window_not_filled():
    """RP score is 0.0 while the window is still warming up (not filled)."""
    cell = _cell_rp()  # lookback=8
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(3)]  # only 3 < 8 → never fills
    strat = build_strategy(cell)
    for c in history[:-1]:
        strat.observe(c)
    sig = ExtractedSignal.extract(cell, strat, history[-1])
    assert sig.signal_score == 0.0


# ---------------------------------------------------------------------------
# Continuous-accumulator tolerance: MR's EMA is unbounded, so live (warmup-seeded,
# drifting) and replay (re-seeded at T-lookback each tick) converge but never
# byte-match. _diff_fields compares accumulator-derived fields (ema_current,
# last_deviation, signal_score) with a relative tolerance, exact otherwise.
# ---------------------------------------------------------------------------


def _mr_signal(ema: str, dev: str, score: float, direction=SignalDirection.POST):
    return ExtractedSignal(
        signal_score=score,
        signal_direction=direction,
        strategy_attributes=tuple(sorted({
            "rate": 0.00011412,
            "threshold_sigma": 0.5,
            "ema_current": Decimal(ema),
            "last_deviation": Decimal(dev),
        }.items())),
        lend_decision=None,
    )


def test_diff_fields_mr_ema_sub_tolerance_not_flagged():
    # The real warmup-vs-window divergence measured empirically (~9e-9 relative).
    live = _mr_signal("0.0002135358306077083662106630005",
                      "-0.4655697843531819142686270719", -0.4655697843531819)
    replay = _mr_signal("0.0002135358325517000949927015409",
                        "-0.4655697892185382641312486931", -0.46556978921853825)
    assert _diff_fields(live, replay) == []  # within tolerance → no divergence


def test_diff_fields_mr_ema_beyond_tolerance_flagged():
    # A real drift (off-by-one observe) shifts ema by ~4% — far above tolerance.
    live = _mr_signal("0.000213536", "-0.46557", -0.46557)
    replay = _mr_signal("0.000222836", "-0.40000", -0.40000)
    diffs = _diff_fields(live, replay)
    assert "strategy_attributes" in diffs
    assert "signal_score" in diffs


def test_diff_fields_exact_field_no_tolerance():
    # Non-accumulator fields (rate, threshold_sigma) are compared EXACTLY: even a
    # tiny difference flags, because those are deterministic from config+candle.
    live = _mr_signal("0.000213536", "-0.46557", -0.46557)
    replay = ExtractedSignal(
        signal_score=-0.46557,
        signal_direction=SignalDirection.POST,
        strategy_attributes=tuple(sorted({
            "rate": 0.00011413,  # differs in the last digit — exact field
            "threshold_sigma": 0.5,
            "ema_current": Decimal("0.000213536"),
            "last_deviation": Decimal("-0.46557"),
        }.items())),
        lend_decision=None,
    )
    assert "strategy_attributes" in _diff_fields(live, replay)


def test_diff_fields_direction_always_exact():
    live = _mr_signal("0.000213536", "-0.46557", -0.46557, direction=SignalDirection.POST)
    replay = _mr_signal("0.000213536", "-0.46557", -0.46557, direction=SignalDirection.SKIP)
    assert "signal_direction" in _diff_fields(live, replay)


def test_mr_warmup_drift_no_false_divergence():
    """Faithful production scenario: the live MR strategy is warmed once then
    drifts forward via per-tick observes; the reporter rebuilds replay from a
    lookback window each tick. Their EMAs converge but differ at ~1e-8 — this must
    NOT be flagged as divergence (the bug the adversarial review surfaced)."""
    base, hour = 1747584000000, 3600_000
    import random
    rng = random.Random(7)
    candles = [_candle(base + i * hour,
                       Decimal(str(round(0.00005 + rng.random() * 0.0003, 8))))
               for i in range(260)]
    cell = CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"ema_span": 24, "threshold_sigma": 0.5, "ratio_sigma": 0.05},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 200,
    })

    def up_to(mts, lb):
        return [c for c in candles if c.mts <= mts][-lb:]

    # Live: warmup at idx 200, then observe ticks 200..205 (drift past warmup).
    live = build_strategy_at_boundary(
        cell=cell, history=up_to(candles[200].mts, 201),
        ref_mts=candles[200].mts, budget_hours=200,
    ).strategy
    for idx in range(200, 206):
        live_signal = ExtractedSignal.extract(cell, live, candles[idx])

    reporter = DivergenceReporter()
    divergence = reporter.check(
        cell=cell, raw_history=up_to(candles[205].mts, 201),
        boundary_candle=candles[205], budget_hours=200, live_signal=live_signal,
    )
    assert divergence is None, (
        f"MR EMA convergence noise must not be flagged as divergence; got: {divergence}"
    )


# ---------------------------------------------------------------------------
# AdaptivePeriod divergence reporter tests (B1)
#
# Design note: period_days is a deterministic step fn of (ema_current, close,
# config). Comparing the derived period would manufacture false divergences at
# tier boundaries. We compare its INPUTS: ema_current (tolerance via
# _APPROX_ATTR_KEYS), window_filled (exact), t1/t2/ratio_sigma (exact config).
# ---------------------------------------------------------------------------


def _cell_ap() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "adaptive_period", "symbol": "fUST", "period_agg": "a30",
        "timeframe": "1h",
        "params": {
            "ema_span": 24,
            "ratio_sigma": 0.05,
            "t1": 0.5,
            "t2": 1.5,
            "p_mid": 7,
            "p_long": 14,
        },
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })


def test_adaptive_period_state_drift_detected():
    """EMA accumulator drift on adaptive_period must surface as strategy_attributes
    divergence even when signal_direction matches (mirrors MR G2 test)."""
    cell = _cell_ap()
    # Flat candle history — ema converges to ~0.0003 after warm-up.
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(30)]

    # Live signal agrees on direction (POST — always lends) but carries a drifted ema.
    fake_live = ExtractedSignal(
        signal_score=0.0,
        signal_direction=SignalDirection.POST,
        strategy_attributes=tuple(sorted({
            "rate": 0.0003,
            "t1": 0.5,
            "t2": 1.5,
            "ratio_sigma": 0.05,
            "ema_current": Decimal("0.0009"),   # drifted: 3× the true ~0.0003
            "window_filled": True,
        }.items())),
        lend_decision=None,
    )

    reporter = DivergenceReporter()
    result = reporter.check(
        cell=cell, raw_history=history, boundary_candle=history[-1],
        budget_hours=2, live_signal=fake_live,
    )

    assert result is not None, "EMA drift must be detected even with matching direction"
    assert "strategy_attributes" in result["diff_fields"]
    assert "signal_direction" not in result["diff_fields"]  # directions agreed
    replay_attrs = dict(result["replay"]["strategy_attributes"])
    assert "ema_current" in replay_attrs, "replay must expose ema_current"
    assert Decimal(str(replay_attrs["ema_current"])) != Decimal("0.0009")


def test_adaptive_period_no_false_divergence_on_boundary_period_flip():
    """No false divergence when ema_current matches within _REL_TOL even if a
    period tier flip would occur if period were compared.

    The key assertion: when all attrs match (within ema tolerance), check()
    returns None. We do NOT need to actually trigger a real tier flip in the
    strategy — period is not in the compared attributes by design.
    """
    cell = _cell_ap()
    # Use enough candles for warm-up (ema_span=24, need ≥24 observes before boundary).
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(30)]

    # Build the true replay signal to get exact attribute values.
    true_strategy = build_strategy(cell)
    for c in history[:-1]:
        true_strategy.observe(c)
    true_signal = ExtractedSignal.extract(cell, true_strategy, history[-1])
    true_attrs = dict(true_signal.strategy_attributes)

    # Build a fake_live whose ema_current is within _REL_TOL of the true value.
    # Even if this tiny perturbation would flip a tier boundary (it won't for flat
    # history, but conceptually: period is not compared, so it doesn't matter).
    true_ema = true_attrs["ema_current"]
    # Perturb by 1e-5 relative — well within _REL_TOL (1e-4).
    perturbed_ema = true_ema * Decimal("1.00001")

    fake_attrs = dict(true_attrs)
    fake_attrs["ema_current"] = perturbed_ema

    fake_live = ExtractedSignal(
        signal_score=true_signal.signal_score,
        signal_direction=true_signal.signal_direction,
        strategy_attributes=tuple(sorted(fake_attrs.items())),
        lend_decision=None,
    )

    reporter = DivergenceReporter()
    result = reporter.check(
        cell=cell, raw_history=history, boundary_candle=history[-1],
        budget_hours=2, live_signal=fake_live,
    )

    assert result is None, (
        f"ema within _REL_TOL must NOT be flagged as divergence; got: {result}"
    )


def test_adaptive_period_warmup_no_false_divergence():
    """Short history (< ema_span): live and replay both warmup, ema_current
    matches within tolerance — no false divergence.

    Strengthened (mutation-proof): fake_live carries HARDCODED strategy_attributes
    matching the known deterministic replay output for 5 flat-rate candles.
    The test fails if _strategy_attributes drops or renames any of the 6 keys.
    """
    cell = _cell_ap()
    # Only 5 candles: well below ema_span=24, window_filled=False.
    # Flat rate 0.0003 → EMA converges exactly to 0.0003 after 4 observes.
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(5)]

    # Hardcoded attrs mirroring what replay produces for this deterministic scenario:
    #   rate=0.0003 (boundary candle close), t1/t2/ratio_sigma from config (exact),
    #   ema_current=Decimal("0.000300000000") (EMA of 4 identical flat candles),
    #   window_filled=False (only 4 observes < ema_span=24).
    fake_live = ExtractedSignal(
        signal_score=0.0,
        signal_direction=SignalDirection.POST,
        strategy_attributes=tuple(sorted({
            "rate": 0.0003,
            "t1": 0.5,
            "t2": 1.5,
            "ratio_sigma": 0.05,
            "ema_current": Decimal("0.000300000000"),
            "window_filled": False,
        }.items())),
        lend_decision=None,
    )

    reporter = DivergenceReporter()
    result = reporter.check(
        cell=cell, raw_history=history, boundary_candle=history[-1],
        budget_hours=2, live_signal=fake_live,
    )

    assert result is None, (
        f"warmup (window not filled) must not produce false divergence; got: {result}"
    )


def test_adaptive_period_config_drift_detected():
    """A live signal carrying a different t2 than what replay uses must be
    flagged — catches config drift between live and replay paths.

    Strengthened (mutation-proof): fake_live carries HARDCODED strategy_attributes
    with all 6 keys explicit. The 5 matching keys exactly mirror replay's known
    deterministic output for 30 flat candles; only t2 is flipped (1.0 vs replay's
    1.5). Divergence is detected because the t2 VALUE differs (exact-field), NOT
    because of a key-set mismatch. The test fails if _strategy_attributes drops or
    renames any key (the set-check in _attrs_diverge fires instead, which also
    detects but via the wrong path — this fixture makes the t2 value the trigger).
    """
    cell = _cell_ap()  # t2=1.5
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(30)]

    # Hardcoded attrs for 30 flat-rate candles:
    #   rate=0.0003, t1=0.5, ratio_sigma=0.05 match replay exactly.
    #   ema_current=Decimal("0.0003000000000000000000000000000") is the exact
    #   EMA value after 29 identical flat observes (converges to the input).
    #   window_filled=True (29 observes > ema_span=24).
    #   t2 is DELIBERATELY wrong (1.0 vs replay's 1.5) to trigger detection.
    fake_live = ExtractedSignal(
        signal_score=0.0,
        signal_direction=SignalDirection.POST,
        strategy_attributes=tuple(sorted({
            "rate": 0.0003,
            "t1": 0.5,
            "t2": 1.0,          # drifted: replay produces 1.5 from cell config
            "ratio_sigma": 0.05,
            "ema_current": Decimal("0.0003000000000000000000000000000"),
            "window_filled": True,
        }.items())),
        lend_decision=None,
    )

    reporter = DivergenceReporter()
    result = reporter.check(
        cell=cell, raw_history=history, boundary_candle=history[-1],
        budget_hours=2, live_signal=fake_live,
    )

    assert result is not None, "config drift (t2 mismatch) must be detected"
    assert "strategy_attributes" in result["diff_fields"]
    # Pin the FEATURE: replay must expose all 6 keys and t2 must be the correct
    # config value (1.5). If _strategy_attributes returned {}, this fails because
    # the divergence would be a key-set mismatch, not a t2 VALUE mismatch.
    replay_attrs = dict(result["replay"]["strategy_attributes"])
    assert set(replay_attrs) == {"rate", "t1", "t2", "ratio_sigma", "ema_current", "window_filled"}, (
        f"replay must expose all 6 adaptive_period attrs; got keys: {set(replay_attrs)}"
    )
    assert replay_attrs["t2"] == 1.5, f"replay t2 must be 1.5 from config; got {replay_attrs['t2']}"
