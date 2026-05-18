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
from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy

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

    NOTE: 4.1 spec writer must adjust this to read the actual strategy
    internals exposed by RatePercentileStrategy / MeanReversionStrategy.
    For now, we read minimal informational attributes:
    - rate_percentile: {percentile from config, last_close = candle.close}
    - mean_reversion: {rate = candle.close, threshold_sigma from config}
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

    NOTE: 4.3 G2 calibration may revise this formula for true cross-strategy
    comparability. For 4.1 we use a placeholder:
    - signal_direction=POST → +1.0, SKIP → -1.0
    Future: read strategy internals (EMA distance, percentile rank).
    """
    return 1.0 if ld is not None else -1.0


class DivergenceReporter:
    def check(
        self,
        *,
        cell: CellConfig,
        history: list[FundingCandle],
        live_signal: ExtractedSignal,
    ) -> dict[str, Any] | None:
        """Re-run backtest replay over the same history; compare to live_signal.

        Returns None if byte-equal; dict with diff detail otherwise.
        """
        if len(history) < 2:
            return None
        replay = build_strategy(cell)
        for c in history[:-1]:
            replay.observe(c)
        replay_signal = ExtractedSignal.extract(cell, replay, history[-1])

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
