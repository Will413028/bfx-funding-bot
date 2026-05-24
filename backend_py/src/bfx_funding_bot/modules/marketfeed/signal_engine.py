"""Signal engine: observe -> emit signal -> divergence check -> emit decision.

設計依據: phase4.1-paper-shadow-infra-design.md Section "Data Flow" Flow 2
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    GuardResult,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.divergence_reporter import (
    DivergenceReporter,
    ExtractedSignal,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
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


class _EventSink(Protocol):
    """Operational telemetry port (→ structured-stdout StdoutEventSink)."""

    async def emit(self, event: dict[str, Any]) -> None: ...


class _DiagnosticsProtocol(Protocol):
    """Forensic diagnostics port (→ PG DiagnosticsSink)."""

    async def emit(self, event: dict[str, Any]) -> None: ...


class _CandlesRepoProtocol(Protocol):
    async def get_up_to(
        self, *, symbol: str, timeframe: str, period_agg: str,
        mts_inclusive: int, lookback: int,
    ) -> list[FundingCandle]: ...


class _SafetyChainProtocol(Protocol):
    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult: ...


class SignalEngine:
    def __init__(
        self,
        *,
        phase: Phase,
        event_sink: _EventSink,
        diagnostics: _DiagnosticsProtocol,
        candles_repo: _CandlesRepoProtocol,
        reporter: DivergenceReporter | None = None,
        safety_chain: _SafetyChainProtocol | None = None,
        executor: ExecutorPort | None = None,
        account_ctx: AccountContext | None = None,
    ) -> None:
        self.phase = phase
        self._events = event_sink
        self.diagnostics = diagnostics
        self.candles_repo = candles_repo
        self.reporter = reporter or DivergenceReporter()
        self.safety_chain = safety_chain
        self.executor = executor
        self.account_ctx = account_ctx

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
            raw_history = await self.candles_repo.get_up_to(
                symbol=cell.symbol, timeframe=cell.timeframe, period_agg=cell.period_agg,
                mts_inclusive=candle.mts, lookback=_lookback(cell) + 1,
            )
            # Phase 4.3 LOCF symmetry: DivergenceReporter.check internally calls
            # the shared build_strategy_at_boundary primitive (same one warmup
            # uses) — passing raw_history + boundary_candle + budget_hours is
            # sufficient. No LOCF / filter logic here.
            budget_hours = _resolve_staleness_budget_hours(cell)
            divergence = self.reporter.check(
                cell=cell, raw_history=raw_history, boundary_candle=candle,
                budget_hours=budget_hours, live_signal=live_signal,
            )
            if divergence is not None:
                await self._emit_signal_divergence_warn(
                    correlation_id, cell, live_signal, divergence,
                    is_stale=is_stale, stale_seconds=stale_seconds,
                )
        except Exception:
            log.exception("divergence_check_exception cell=%s", cell.pair_id)

        # Phase 4.2 Task 19: defer DECISION emit until post-safety-eval.
        # Build tentative decision (post-strategy, pre-safety).
        tentative = self._build_tentative_decision(correlation_id, cell, live_signal)

        final = await self._apply_safety_eval(tentative)

        await self._emit_decision_final(correlation_id, cell, final)

        if (
            final.decision_outcome == DecisionOutcome.POST
            and self.executor is not None
            and self.account_ctx is not None
        ):
            try:
                await self.executor.submit(final, self.account_ctx)
            except Exception:
                log.exception("executor_submit_exception cell=%s", cell.pair_id)
                raise

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
        await self._events.emit({
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
        # event_type=signal_divergence (not signal) so queries that count
        # strategy emissions by event_type='signal' don't over-count this
        # quality-check side event. Shares correlation_id with the base
        # signal emit (same cycle / trace).
        budget_seconds = _resolve_budget_seconds(cell)
        await self._events.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.WARN.value,
            "phase": self.phase.value,
            "strategy": cell.strategy.value,
            "cell": cell.cell_id,
            "event_type": EventType.SIGNAL_DIVERGENCE.value,
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

    def _build_tentative_decision(
        self, correlation_id: UUID, cell: CellConfig, sig: ExtractedSignal,
    ) -> DecisionPayload:
        """Pre-safety DecisionPayload built from strategy output.

        Decimal → float: DecisionPayload schema declares offer_rate: float | None.
        Funding rate precision (~6 dp) is well within double-precision range.
        """
        if sig.signal_direction == SignalDirection.POST and sig.lend_decision is not None:
            return DecisionPayload(
                decision_outcome=DecisionOutcome.POST,
                signal_correlation_id=correlation_id,
                offer_rate=float(sig.lend_decision.rate),
                offer_amount_usdt=cell.reference_amount_usdt,
                offer_duration_days=int(sig.lend_decision.period_days),
            )
        return DecisionPayload(
            decision_outcome=DecisionOutcome.SKIP,
            signal_correlation_id=correlation_id,
            skip_reason=SkipReason.BELOW_THRESHOLD,
        )

    async def _apply_safety_eval(self, tentative: DecisionPayload) -> DecisionPayload:
        """Run safety_chain.evaluate; downgrade POST→SKIP/safety_block if denied.

        No-op (returns tentative) when:
        - safety_chain or account_ctx is not wired (4.1 fallback / fixture-less tests)
        - tentative is already SKIP (strategy didn't post — nothing to block)
        """
        if self.safety_chain is None or self.account_ctx is None:
            return tentative
        if tentative.decision_outcome != DecisionOutcome.POST:
            return tentative
        result = await self.safety_chain.evaluate(tentative, self.account_ctx)
        if result.allowed:
            return tentative
        return DecisionPayload(
            decision_outcome=DecisionOutcome.SKIP,
            signal_correlation_id=tentative.signal_correlation_id,
            skip_reason=SkipReason.SAFETY_BLOCK,
            skip_reason_detail=result.reason,
        )

    async def _emit_decision_final(
        self, correlation_id: UUID, cell: CellConfig, decision: DecisionPayload,
    ) -> None:
        """Single immutable DECISION emit per cycle (event-sourcing best practice).

        Envelope: standard event fields (timestamp/level/phase/strategy/cell/
        event_type/correlation_id) + account_id (Task 1 schema addition,
        sourced from account_ctx or 'default' fallback) + payload from the
        post-safety-eval DecisionPayload model_dump.
        """
        await self.diagnostics.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self.phase.value,
            "strategy": cell.strategy.value,
            "cell": cell.cell_id,
            "event_type": EventType.DECISION.value,
            "correlation_id": str(correlation_id),
            "account_id": getattr(self.account_ctx, "account_id", "default"),
            "payload": decision.model_dump(mode="json"),
        })

    async def _emit_health(
        self, correlation_id: UUID, status: HealthStatus, *,
        target: HealthTarget, error_message: str,
    ) -> None:
        await self._events.emit({
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
