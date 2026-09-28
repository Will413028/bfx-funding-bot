"""Signal engine: observe -> emit signal -> divergence check -> emit decision.

設計依據: phase 4.1 paper/shadow infra design Section "Data Flow" Flow 2

Decoupling (2026-05-29): SignalEngine only records strategy *intent* as a
StandingQuote. Safety evaluation and venue submission are handled exclusively
by the DeploymentReconciler (single-writer pattern).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from bfx_funding_bot.core.telemetry import EventType, HealthStatus, HealthTarget, Level, Phase
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.execution.deployment.standing_quote import (
    StandingQuote,
    StandingQuoteStore,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.divergence_reporter import (
    DivergenceReporter,
    ExtractedSignal,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
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


class SignalEngine:
    def __init__(
        self,
        *,
        phase: Phase,
        event_sink: _EventSink,
        diagnostics: _DiagnosticsProtocol,
        candles_repo: _CandlesRepoProtocol,
        reporter: DivergenceReporter | None = None,
        quote_store: StandingQuoteStore | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.phase = phase
        self._events = event_sink
        self.diagnostics = diagnostics
        self.candles_repo = candles_repo
        self.reporter = reporter or DivergenceReporter()
        self.quote_store = quote_store
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def process_candle(
        self,
        *,
        cell: CellConfig,
        candle: FundingCandle,
        registry: StrategyRegistry,
        is_stale: bool = False,
        stale_seconds: int = 0,
        quote_created_at_ms: int | None = None,
    ) -> None:
        # quote_created_at_ms: the quote's age is measured from the boundary it
        # speaks for, not from when this ran. Identical at a live tick, where the
        # two are seconds apart; it matters when a boundary is replayed at boot,
        # because dating that quote "now" would silently extend a TTL the
        # boundary had already spent.
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

        # Signal layer: build strategy decision and record intent as a StandingQuote.
        # Safety evaluation and venue submission are handled by the
        # DeploymentReconciler (single-writer pattern) — not here.
        decision = self._build_tentative_decision(
            correlation_id, cell, live_signal,
            is_stale=is_stale, stale_seconds=stale_seconds,
        )

        await self._emit_decision_final(correlation_id, cell, decision)

        if self.quote_store is not None:
            self.quote_store.update(StandingQuote(
                cell_id=cell.cell_id,
                outcome=decision.decision_outcome,
                rate=decision.offer_rate,
                period_days=decision.offer_duration_days,
                signal_correlation_id=correlation_id,
                created_at_ms=self._clock() if quote_created_at_ms is None else quote_created_at_ms,
            ))

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
        *, is_stale: bool = False, stale_seconds: int = 0,
    ) -> DecisionPayload:
        """Pre-safety DecisionPayload built from strategy output.

        The strategy's Decimal rate is carried as-is (no float on the money
        path). The amount is the cell's config reference, never submitted: the
        deployment reconciler sizes the real offer.

        Staleness (is_stale/stale_seconds + cell budget) is stamped here so it
        rides the durable DECISION record, not just the ephemeral SIGNAL.
        """
        budget_seconds = _resolve_budget_seconds(cell)
        if sig.signal_direction == SignalDirection.POST and sig.lend_decision is not None:
            return DecisionPayload(
                decision_outcome=DecisionOutcome.POST,
                signal_correlation_id=correlation_id,
                symbol=cell.symbol,
                offer_rate=sig.lend_decision.rate,
                offer_amount_usdt=Decimal(str(cell.reference_amount_usdt)),
                offer_duration_days=int(sig.lend_decision.period_days),
                is_stale=is_stale, stale_seconds=stale_seconds, budget_seconds=budget_seconds,
            )
        return DecisionPayload(
            decision_outcome=DecisionOutcome.SKIP,
            signal_correlation_id=correlation_id,
            symbol=cell.symbol,
            skip_reason=SkipReason.BELOW_THRESHOLD,
            is_stale=is_stale, stale_seconds=stale_seconds, budget_seconds=budget_seconds,
        )

    async def _emit_decision_final(
        self, correlation_id: UUID, cell: CellConfig, decision: DecisionPayload,
    ) -> None:
        """Single immutable DECISION emit per cycle (event-sourcing best practice).

        The diagnostics sink injects the daemon's account scope.  The signal
        layer deliberately does not invent a realm or use a ``default``
        fallback; account binding is explicit at daemon bootstrap.
        """
        await self.diagnostics.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self.phase.value,
            "strategy": cell.strategy.value,
            "cell": cell.cell_id,
            "event_type": EventType.DECISION.value,
            "correlation_id": str(correlation_id),
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
