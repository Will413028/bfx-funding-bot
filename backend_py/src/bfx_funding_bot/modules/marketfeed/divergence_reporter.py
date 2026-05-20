"""In-process dual-compute divergence reporter.

設計依據: phase4.1-paper-shadow-infra-design.md Q4 — replay rebuilds strategy
from scratch on the same DB candle source; live != replay 即觸發 divergence_detail
warn event.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
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
    """Per-strategy attribute extraction.

    TODO(phase-4.3): Current placeholder reads only static inputs (config + candle.close)
    — cannot detect strategy *internal state* divergence (e.g., RP deque drift,
    MR EMA accumulator off-by-one). For true silent-divergence detection, expose
    strategy state via @property accessors on RatePercentileStrategy /
    MeanReversionStrategy (e.g., `last_threshold`, `last_ema`) and include here.
    This limits 4.1's divergence reporter to direction-flip detection only.
    """
    if cell.strategy == StrategyName.RATE_PERCENTILE:
        return {
            "percentile": float(cell.params["percentile"]),
            "last_close": float(candle.close) if candle.close is not None else 0.0,
        }
    if cell.strategy == StrategyName.MEAN_REVERSION:
        return {
            "rate": float(candle.close) if candle.close is not None else 0.0,
            "threshold_sigma": float(cell.params["threshold_sigma"]),
        }
    return {}


def _normalize_signal_score(
    cell: CellConfig, strategy: Any, candle: FundingCandle, ld: LendDecision | None,
) -> float:
    """Cross-strategy comparable normalized score.

    TODO(phase-4.3): G2 calibration formula. Current placeholder is direction-only.
    Future: read strategy internals (EMA distance, percentile rank).
    """
    del strategy, candle  # placeholder intentionally ignores state
    return 1.0 if ld is not None else -1.0


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

        Returns None if byte-equal; dict with diff detail otherwise.

        Phase 4.3 LOCF symmetry: this function and warmup.warmup_cell BOTH
        call build_strategy_at_boundary with their respective (history,
        ref_mts, budget_hours). That single function is the source of
        truth — given the same inputs it produces deterministically equal
        Strategy state, so live (warmup-derived) and replay (this
        function) cannot drift by construction.
        """
        if len(raw_history) < 2:
            return None
        result = build_strategy_at_boundary(
            cell=cell, history=raw_history,
            ref_mts=boundary_candle.mts, budget_hours=budget_hours,
        )
        replay_signal = ExtractedSignal.extract(cell, result.strategy, boundary_candle)

        if replay_signal == live_signal:
            return None
        log.warning(
            "divergence_detected cell=%s live=%r replay=%r",
            cell.pair_id, live_signal, replay_signal,
        )
        return {
            "live": _to_dict(live_signal),
            "replay": _to_dict(replay_signal),
            "diff_fields": _diff_fields(live_signal, replay_signal),
        }


def _to_dict(s: ExtractedSignal) -> dict[str, Any]:
    return {
        "signal_score": s.signal_score,
        "signal_direction": s.signal_direction.value,
        "strategy_attributes": dict(s.strategy_attributes),
    }


def _diff_fields(a: ExtractedSignal, b: ExtractedSignal) -> list[str]:
    diffs: list[str] = []
    if a.signal_score != b.signal_score:
        diffs.append("signal_score")
    if a.signal_direction != b.signal_direction:
        diffs.append("signal_direction")
    if a.strategy_attributes != b.strategy_attributes:
        diffs.append("strategy_attributes")
    return diffs
