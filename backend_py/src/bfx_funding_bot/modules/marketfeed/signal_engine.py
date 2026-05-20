"""Signal engine: observe -> emit signal -> divergence check -> emit decision.

設計依據: phase4.1-paper-shadow-infra-design.md Section "Data Flow" Flow 2
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.divergence_reporter import (
    DivergenceReporter,
    ExtractedSignal,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
    Phase,
    SignalDirection,
    SkipReason,
)
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry

log = logging.getLogger(__name__)

def _resolve_budget_seconds(cell: CellConfig) -> int:
    """Convert resolved per-cell staleness_budget_hours to seconds.

    Invariant: load_config() must have populated cell.staleness_budget_hours
    from MarketfeedConfig.staleness_budget_hours_default before emit time.
    None here = loader invariant violation = crash loud.
    """
    if cell.staleness_budget_hours is None:
        raise AssertionError(
            f"cell {cell.symbol}_{cell.period_agg}_{cell.strategy} "
            "staleness_budget_hours not resolved; was load_config() called?"
        )
    return cell.staleness_budget_hours * 3600


def _resolve_staleness_budget_hours(cell: CellConfig) -> int:
    """Resolve per-cell staleness_budget_hours assuming load_config() resolution.

    Invariant: load_config() populates cell.staleness_budget_hours from
    MarketfeedConfig.staleness_budget_hours_default before any signal processing.
    None here = loader invariant violation = crash loud.

    Parallel to _resolve_budget_seconds (which converts to seconds);
    this returns raw hours for use as reindex_and_ffill max_gap_hours.
    """
    if cell.staleness_budget_hours is None:
        raise AssertionError(
            f"cell {cell.symbol}_{cell.period_agg}_{cell.strategy} "
            "staleness_budget_hours not resolved; was load_config() called?"
        )
    return cell.staleness_budget_hours


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class _CandlesRepoProtocol(Protocol):
    async def get_up_to(
        self, *, symbol: str, timeframe: str, period_agg: str,
        mts_inclusive: int, lookback: int,
    ) -> list[FundingCandle]: ...


class SignalEngine:
    def __init__(
        self,
        *,
        phase: Phase,
        axiom: _AxiomProtocol,
        candles_repo: _CandlesRepoProtocol,
        reporter: DivergenceReporter | None = None,
    ) -> None:
        self.phase = phase
        self.axiom = axiom
        self.candles_repo = candles_repo
        self.reporter = reporter or DivergenceReporter()

    async def process_candle(
        self,
        *,
        cell: CellConfig,
        candle: FundingCandle,
        registry: StrategyRegistry,
        is_stale: bool = False,
        stale_seconds: int = 0,
    ) -> None:
        correlation_id = uuid4()
        strategy = registry.get(cell)

        try:
            live_signal = ExtractedSignal.extract(cell, strategy, candle)
        except Exception as exc:
            log.exception("strategy_exception cell=%s mts=%d", cell.pair_id, candle.mts)
            await self._emit_health(
                correlation_id, HealthStatus.DEGRADED,
                target=HealthTarget.BITFINEX_WS,
                error_message=f"strategy_exception: {exc!r}",
            )
            return

        await self._emit_signal(
            correlation_id, cell, live_signal,
            is_stale=is_stale, stale_seconds=stale_seconds,
        )

        try:
            history = await self.candles_repo.get_up_to(
                symbol=cell.symbol, timeframe=cell.timeframe, period_agg=cell.period_agg,
                mts_inclusive=candle.mts, lookback=_lookback(cell) + 1,
            )
            # Phase 4.3 LOCF: replay path must see the same LOCF-processed candles
            # as the live path (daemon applies reindex_and_ffill before calling here).
            # Apply LOCF to raw history so DivergenceReporter.check() rebuilds the
            # replay strategy on identical inputs → CP1 byte-equivalence holds on
            # sparse input.
            budget_hours = _resolve_staleness_budget_hours(cell)
            # LOCF window is strategy-lookback-sized (not budget-sized) because replay only
            # needs enough candles to rebuild strategy state. Daemon uses budget-sized
            # window for staleness tier determination — different purpose, different size.
            filled = reindex_and_ffill(history, ref_mts=candle.mts, max_gap_hours=budget_hours)
            locf_history = [fc.candle for fc in filled if fc.candle is not None]
            divergence = self.reporter.check(cell=cell, history=locf_history, live_signal=live_signal)
            if divergence is not None:
                await self._emit_signal_divergence_warn(
                    correlation_id, cell, live_signal, divergence,
                    is_stale=is_stale, stale_seconds=stale_seconds,
                )
        except Exception:
            log.exception("divergence_check_exception cell=%s", cell.pair_id)

        await self._emit_decision(correlation_id, cell, live_signal)

    async def _emit_signal(
        self,
        correlation_id: UUID,
        cell: CellConfig,
        sig: ExtractedSignal,
        *,
        is_stale: bool = False,
        stale_seconds: int = 0,
    ) -> None:
        budget_seconds = _resolve_budget_seconds(cell)
        await self.axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self.phase.value,
            "strategy": cell.strategy.value,
            "cell": cell.cell_id,
            "event_type": EventType.SIGNAL.value,
            "correlation_id": str(correlation_id),
            "payload": {
                "signal_score": sig.signal_score,
                "signal_direction": sig.signal_direction.value,
                "strategy_attributes": dict(sig.strategy_attributes),
                "is_stale": is_stale,
                "stale_seconds": stale_seconds,
                "budget_seconds": budget_seconds,
            },
        })

    async def _emit_signal_divergence_warn(
        self,
        correlation_id: UUID,
        cell: CellConfig,
        sig: ExtractedSignal,
        divergence: dict[str, Any],
        *,
        is_stale: bool = False,
        stale_seconds: int = 0,
    ) -> None:
        budget_seconds = _resolve_budget_seconds(cell)
        await self.axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.WARN.value,
            "phase": self.phase.value,
            "strategy": cell.strategy.value,
            "cell": cell.cell_id,
            "event_type": EventType.SIGNAL.value,
            "correlation_id": str(correlation_id),
            "payload": {
                "signal_score": sig.signal_score,
                "signal_direction": sig.signal_direction.value,
                "strategy_attributes": dict(sig.strategy_attributes),
                "divergence_detail": divergence,
                "is_stale": is_stale,
                "stale_seconds": stale_seconds,
                "budget_seconds": budget_seconds,
            },
        })

    async def _emit_decision(
        self, correlation_id: UUID, cell: CellConfig, sig: ExtractedSignal,
    ) -> None:
        if sig.signal_direction == SignalDirection.POST and sig.lend_decision is not None:
            # Decimal → float: DecisionPayload schema declares offer_rate: float | None.
            # Funding rate precision (~6 dp) is well within double-precision range.
            payload: dict[str, Any] = {
                "decision_outcome": DecisionOutcome.POST.value,
                "signal_correlation_id": str(correlation_id),
                "offer_rate": float(sig.lend_decision.rate),
                "offer_amount_usdt": cell.reference_amount_usdt,
                "offer_duration_days": int(sig.lend_decision.period_days),
            }
        else:
            payload = {
                "decision_outcome": DecisionOutcome.SKIP.value,
                "signal_correlation_id": str(correlation_id),
                "skip_reason": SkipReason.BELOW_THRESHOLD.value,
            }
        await self.axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self.phase.value,
            "strategy": cell.strategy.value,
            "cell": cell.cell_id,
            "event_type": EventType.DECISION.value,
            "correlation_id": str(correlation_id),
            "payload": payload,
        })

    async def _emit_health(
        self, correlation_id: UUID, status: HealthStatus, *,
        target: HealthTarget, error_message: str,
    ) -> None:
        await self.axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.WARN.value if status == HealthStatus.DEGRADED else Level.ERROR.value,
            "phase": self.phase.value,
            "strategy": None,
            "cell": None,
            "event_type": EventType.HEALTH_CHECK.value,
            "correlation_id": str(correlation_id),
            "payload": {
                "check_target": target.value, "status": status.value,
                "last_msg_age_ms": 0, "reconnect_count_last_hour": 0,
                "error_message": error_message,
            },
        })


def _lookback(cell: CellConfig) -> int:
    from bfx_funding_bot.modules.marketfeed.warmup import _lookback_for
    return _lookback_for(cell)
