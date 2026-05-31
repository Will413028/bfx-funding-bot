"""In-process dual-compute divergence reporter.

設計依據: phase4.1-paper-shadow-infra-design.md Q4 — replay rebuilds strategy
from scratch on the same DB candle source; live != replay 即觸發 divergence_detail
warn event.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import (
    SignalDirection,
    StrategyName,
)
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    build_strategy_at_boundary,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractedSignal:
    """4.1 signal payload pre-emit representation; used for byte-equivalence diffing."""

    signal_score: float
    signal_direction: SignalDirection
    strategy_attributes: tuple[tuple[str, Any], ...]
    lend_decision: LendDecision | None = None

    @classmethod
    def extract(
        cls, cell: CellConfig, strategy: Any, candle: FundingCandle
    ) -> ExtractedSignal:
        """Observe + decide on candle, returning normalized signal.

        SIDE EFFECT: calls `strategy.observe(candle)` then `strategy.decide(candle)`.
        Caller MUST NOT have already observed `candle` on this strategy; doing so
        would double-count it (rate_percentile deque corruption, MeanReversion
        EMA over-update). The reporter's check() observes history[:-1] only and
        relies on extract() to handle history[-1] exactly once.
        """
        strategy.observe(candle)
        ld = strategy.decide(candle)
        direction = SignalDirection.POST if ld is not None else SignalDirection.SKIP
        attrs = _strategy_attributes(cell, strategy, candle, ld)
        score = _normalize_signal_score(cell, strategy, candle, ld)
        return cls(
            signal_score=score,
            signal_direction=direction,
            strategy_attributes=tuple(sorted(attrs.items())),
            lend_decision=ld,
        )


def _strategy_attributes(
    cell: CellConfig, strategy: Any, candle: FundingCandle, ld: LendDecision | None,
) -> dict[str, Any]:
    """Per-strategy attribute extraction for divergence comparison.

    Includes decision-determining internal state (MR ema/deviation, RP
    threshold/window-filled) at Decimal precision so silent accumulator drift is
    detectable even when the resulting direction matches (G2 state-parity).
    Reads strategy state AFTER ExtractedSignal.extract has called observe+decide,
    so the cached deviation/threshold reflect this boundary candle. Decimal values
    are kept un-cast — a float cast would mask sub-Decimal drift.
    """
    if cell.strategy == StrategyName.RATE_PERCENTILE:
        return {
            "percentile": float(cell.params["percentile"]),
            "last_close": float(candle.close) if candle.close is not None else 0.0,
            "last_threshold": strategy.last_threshold,
            "window_filled": strategy.window_filled,
        }
    if cell.strategy == StrategyName.MEAN_REVERSION:
        return {
            "rate": float(candle.close) if candle.close is not None else 0.0,
            "threshold_sigma": float(cell.params["threshold_sigma"]),
            "ema_current": strategy.ema_current,
            "last_deviation": strategy.last_deviation,
        }
    return {}


def _normalize_signal_score(
    cell: CellConfig, strategy: Any, candle: FundingCandle, ld: LendDecision | None,
) -> float:
    """Continuous, strategy-specific signal-strength score (replaces the old
    direction-only ±1.0 placeholder). Same-strategy live vs replay must be equal.

    - MR: the (close-ema)/ema deviation margin (negative = skip region).
    - RP: the 0-100 percentile rank of close within the current window.
    0.0 when the underlying state is unavailable (warmup / no ema). Reads the
    cached state populated by ExtractedSignal.extract's observe+decide.
    Cross-strategy normalization is intentionally deferred to the G2 audit (D).
    """
    del ld  # score derives from cached state, not the decision object
    if cell.strategy == StrategyName.MEAN_REVERSION:
        dev = strategy.last_deviation
        return float(dev) if dev is not None else 0.0
    if cell.strategy == StrategyName.RATE_PERCENTILE:
        if candle.close is None or not strategy.window_filled:
            return 0.0
        window = strategy.window_values  # read-only snapshot, post-observe
        if not window:
            return 0.0
        close = candle.close
        return 100.0 * sum(1 for w in window if w <= close) / len(window)
    return 0.0


class DivergenceReporter:
    def check(
        self,
        *,
        cell: CellConfig,
        raw_history: list[FundingCandle],
        boundary_candle: FundingCandle,
        budget_hours: int,
        live_signal: ExtractedSignal,
    ) -> dict[str, Any] | None:
        """Rebuild the reference strategy state via the shared
        build_strategy_at_boundary primitive, extract on boundary_candle,
        compare to live_signal.

        Returns None if equal (within tolerance); dict with diff detail otherwise.

        Phase 4.3 LOCF symmetry: this function and warmup.warmup_cell BOTH call
        build_strategy_at_boundary. For BOUNDED state (RP's maxlen deque) live
        (warmup-derived, incrementally observed) and replay (this function) are
        byte-equal — the bounded window forgets old data exactly. For UNBOUNDED
        accumulators (MR's EMA) they only CONVERGE: live is seeded once at warmup
        and drifts forward while replay re-seeds at ref_mts-lookback each tick, so
        the seed's exponential tail (~(1-alpha)^lookback) differs at a noise-floor
        level that never reaches 0 in Decimal. _diff_fields compares those
        accumulator-derived fields with a relative tolerance, exact otherwise.
        """
        if len(raw_history) < 2:
            return None
        result = build_strategy_at_boundary(
            cell=cell, history=raw_history,
            ref_mts=boundary_candle.mts, budget_hours=budget_hours,
        )
        replay_signal = ExtractedSignal.extract(cell, result.strategy, boundary_candle)

        diff_fields = _diff_fields(live_signal, replay_signal)
        if not diff_fields:
            return None
        log.warning(
            "divergence_detected cell=%s live=%r replay=%r",
            cell.pair_id, live_signal, replay_signal,
        )
        return {
            "live": _to_dict(live_signal),
            "replay": _to_dict(replay_signal),
            "diff_fields": diff_fields,
        }


def _to_dict(s: ExtractedSignal) -> dict[str, Any]:
    return {
        "signal_score": s.signal_score,
        "signal_direction": s.signal_direction.value,
        "strategy_attributes": dict(s.strategy_attributes),
    }


# Accumulator-derived attributes compared with relative tolerance (vs exact).
# MR's EMA is an unbounded accumulator: warmup-seeded live and window-seeded
# replay converge but never byte-match (the seed's exponential tail never reaches
# 0 in Decimal). Bounded/discrete fields (RP deque-derived, config, direction)
# stay exact. The tolerance sits ~140x above the span=24 noise floor (~7e-7
# steady-state, lookback=200) and ~100x below real-drift magnitude (~1e-2). NOTE:
# span=168 cells do NOT converge within lookback=200 (~9% floor) — they need a
# larger lookback to be tolerance-comparable; not deployed (canary is span=24),
# deferred.
_APPROX_ATTR_KEYS = frozenset({"ema_current", "last_deviation"})
_REL_TOL = Decimal("1e-4")


def _approx_equal(a: Any, b: Any) -> bool:
    """Relative-tolerance equality for continuous accumulator values.

    Exact match short-circuits True. None matches only None. Otherwise compares
    |a-b| / max(|a|, |b|) <= _REL_TOL (both ~0 → equal).
    """
    if a == b:
        return True
    if a is None or b is None:
        return False
    da, db = Decimal(str(a)), Decimal(str(b))
    scale = max(abs(da), abs(db))
    if scale == 0:
        return True
    return abs(da - db) / scale <= _REL_TOL


def _attrs_diverge(a_attrs: Any, b_attrs: Any) -> bool:
    """Per-key attribute comparison: tolerance on accumulator keys, exact else."""
    da, db = dict(a_attrs), dict(b_attrs)
    if set(da) != set(db):
        return True
    for k in da:
        if k in _APPROX_ATTR_KEYS:
            if not _approx_equal(da[k], db[k]):
                return True
        elif da[k] != db[k]:
            return True
    return False


def _diff_fields(a: ExtractedSignal, b: ExtractedSignal) -> list[str]:
    """Field-level divergence between two signals on the SAME boundary candle.

    `lend_decision` is intentionally NOT compared: every strategy builds it as
    LendDecision(mts=candle.mts, rate=candle.close, period_days=2) from the shared
    boundary candle, so it is byte-identical whenever signal_direction agrees (and
    its None-ness IS signal_direction). Comparing it would be redundant.

    signal_score is always tolerance-compared: MR's score = float(deviation) needs
    it (continuous accumulator); RP's score is a bounded-window percentile rank
    that is byte-equal live-vs-replay, so the tolerance is a conservative no-op
    there (a real RP drift shifts the rank by >= 100/lookback >> tol and is also
    caught by the exact last_threshold/window_filled fields).
    """
    diffs: list[str] = []
    if not _approx_equal(a.signal_score, b.signal_score):
        diffs.append("signal_score")
    if a.signal_direction != b.signal_direction:
        diffs.append("signal_direction")
    if _attrs_diverge(a.strategy_attributes, b.strategy_attributes):
        diffs.append("strategy_attributes")
    return diffs
