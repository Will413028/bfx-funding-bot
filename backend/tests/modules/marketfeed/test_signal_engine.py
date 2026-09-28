from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.marketfeed.divergence_reporter import DivergenceReporter
from bfx_funding_bot.modules.marketfeed.schemas import Envelope
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.strategy import CellConfig, DecisionOutcome
from bfx_funding_bot.modules.strategy.wiring import build_strategy, build_strategy_at_boundary


def _cell() -> CellConfig:
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
    })
    cell.staleness_budget_hours = 2  # simulate load_config() resolution
    return cell


def _history(n: int) -> list[FundingCandle]:
    return [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=1747584000000 + i * 3600_000,
            open=Decimal(f"0.000{i+1}"), close=Decimal(f"0.000{i+1}"),
            high=Decimal(f"0.000{i+1}"), low=Decimal(f"0.000{i+1}"),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


async def test_signal_engine_emits_signal_and_decision():
    captured: list[dict] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    decisions: list[dict] = []
    diagnostics = MagicMock()
    diagnostics.emit = AsyncMock(side_effect=lambda e: decisions.append(e))

    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))

    engine = SignalEngine(
        phase=Phase.PAPER, event_sink=axiom, diagnostics=diagnostics,
        candles_repo=candles_repo,
        reporter=DivergenceReporter(build_strategy_at_boundary),
    )
    cell = _cell()
    reg = StrategyRegistry(build_strategy)
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)

    await engine.process_candle(cell=cell, candle=_history(8)[-1], registry=reg)

    types = [e["event_type"] for e in captured]
    assert "signal" in types
    decision_types = [e["event_type"] for e in decisions]
    assert "decision" in decision_types
    assert "decision" not in types  # DECISION must not leak onto axiom channel
    assert "signal" not in decision_types  # SIGNAL must not leak onto diagnostics channel


async def test_cp3_every_emit_passes_schema_validation():
    """CP3: every emitted event must validate against Envelope + payload model."""
    captured: list[dict] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    decisions: list[dict] = []
    diagnostics = MagicMock()
    diagnostics.emit = AsyncMock(side_effect=lambda e: decisions.append(e))

    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))

    engine = SignalEngine(
        phase=Phase.PAPER, event_sink=axiom, diagnostics=diagnostics,
        candles_repo=candles_repo,
        reporter=DivergenceReporter(build_strategy_at_boundary),
    )
    cell = _cell()
    reg = StrategyRegistry(build_strategy)
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)

    await engine.process_candle(cell=cell, candle=_history(8)[-1], registry=reg)

    # Non-divergent path: 1 event on axiom (signal), 1 on diagnostics (decision)
    assert len(captured) == 1
    assert len(decisions) == 1
    for event in captured + decisions:
        Envelope.model_validate(event)  # raises if schema violation


async def test_cp3_divergence_path_also_passes_schema():
    """CP3 (divergence variant): when reporter detects drift, divergence emit
    uses distinct event_type='signal_divergence' (orthogonal to severity), and
    both the base signal + the divergence event must validate against schema.

    Why distinct event_type (not level=warn discrimination):
    - level (info/warn) = severity, event_type = semantic identity (orthogonal)
    - Avoids over-counting in queries that filter by event_type='signal' (C2 etc.)
    - Matches OTel / CloudEvents / DDD conventions
    """
    captured: list[dict] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    decisions: list[dict] = []
    diagnostics = MagicMock()
    diagnostics.emit = AsyncMock(side_effect=lambda e: decisions.append(e))

    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))

    # Mock reporter that always returns a divergence dict
    reporter = MagicMock()
    reporter.check = MagicMock(return_value={
        "live": {"signal_score": 1.0, "signal_direction": "post",
                 "strategy_attributes": {}},
        "replay": {"signal_score": -1.0, "signal_direction": "skip",
                   "strategy_attributes": {}},
        "diff_fields": ["signal_score", "signal_direction"],
    })

    engine = SignalEngine(
        phase=Phase.PAPER, event_sink=axiom, diagnostics=diagnostics,
        candles_repo=candles_repo, reporter=reporter,
    )
    cell = _cell()
    reg = StrategyRegistry(build_strategy)
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)

    await engine.process_candle(cell=cell, candle=_history(8)[-1], registry=reg)

    # Divergent path: 2 on axiom (signal + signal_divergence), 1 on diagnostics (decision)
    assert len(captured) == 2
    assert len(decisions) == 1
    types_and_levels = [(e["event_type"], e["level"]) for e in captured]
    assert ("signal", "info") in types_and_levels
    assert ("signal_divergence", "warn") in types_and_levels
    assert decisions[0]["event_type"] == "decision"
    assert decisions[0]["level"] == "info"

    # Exactly one real signal per cycle (not two) — fixes C2 over-count bug
    signal_events = [e for e in captured if e["event_type"] == "signal"]
    assert len(signal_events) == 1

    # All must validate
    for event in captured + decisions:
        Envelope.model_validate(event)

    # Divergence event must carry divergence_detail
    div_events = [e for e in captured if e["event_type"] == "signal_divergence"]
    assert len(div_events) == 1
    assert div_events[0]["payload"]["divergence_detail"] is not None
    # Shares correlation_id with the base signal (same trace / cycle)
    sig_corr = signal_events[0]["correlation_id"]
    assert div_events[0]["correlation_id"] == sig_corr


def test_resolve_budget_seconds_raises_if_staleness_not_resolved() -> None:
    """Loader invariant: cell.staleness_budget_hours must be set before emit.

    None at emit time = loader bypassed = AssertionError to surface the bug.
    """
    import pytest

    from bfx_funding_bot.modules.marketfeed.signal_engine import _resolve_budget_seconds

    cell = CellConfig(
        strategy="mean_reversion",
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        params={"threshold_sigma": 1.0, "ratio_sigma": 0.5, "ema_span": 168},
        reference_amount_usdt=150.0,
        staleness_budget_hours=None,  # simulate loader bypass
    )
    with pytest.raises(AssertionError, match="staleness_budget_hours not resolved"):
        _resolve_budget_seconds(cell)


def test_resolve_budget_seconds_correct_value() -> None:
    """Resolved cell returns hours * 3600."""
    from bfx_funding_bot.modules.marketfeed.signal_engine import _resolve_budget_seconds

    cell = CellConfig(
        strategy="rate_percentile",
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        params={"percentile": 75, "lookback_hours": 168},
        reference_amount_usdt=150.0,
        staleness_budget_hours=12,
    )
    assert _resolve_budget_seconds(cell) == 12 * 3600


def test_resolve_staleness_budget_hours_raises_if_not_resolved() -> None:
    """Loader invariant: cell.staleness_budget_hours must be set before processing.

    None at signal_engine.process_candle time = loader bypassed = AssertionError
    (parallel to test_resolve_budget_seconds_raises_if_staleness_not_resolved).
    """
    import pytest

    from bfx_funding_bot.modules.marketfeed.signal_engine import _resolve_staleness_budget_hours

    cell = CellConfig(
        strategy="mean_reversion",
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        params={"threshold_sigma": 1.0, "ratio_sigma": 0.5, "ema_span": 168},
        reference_amount_usdt=150.0,
        staleness_budget_hours=None,
    )
    with pytest.raises(AssertionError, match="staleness_budget_hours not resolved"):
        _resolve_staleness_budget_hours(cell)


def test_resolve_staleness_budget_hours_correct_value() -> None:
    """Resolved cell returns hours value as-is."""
    from bfx_funding_bot.modules.marketfeed.signal_engine import _resolve_staleness_budget_hours

    cell = CellConfig(
        strategy="rate_percentile",
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        params={"percentile": 75, "lookback_hours": 168},
        reference_amount_usdt=150.0,
        staleness_budget_hours=12,
    )
    assert _resolve_staleness_budget_hours(cell) == 12


async def test_process_candle_writes_standing_quote():
    """Signal layer writes a StandingQuote on POST decision.

    Decoupling: SignalEngine no longer submits to venue; it records pure
    strategy intent in the StandingQuoteStore. The DeploymentReconciler
    reads these quotes and handles safety + venue submit.
    """
    axiom = MagicMock()
    axiom.emit = AsyncMock()
    diagnostics = MagicMock()
    diagnostics.emit = AsyncMock()
    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))

    store = StandingQuoteStore(ttl_ms=3_900_000)
    cell = _cell()
    engine = SignalEngine(
        phase=Phase.PAPER, event_sink=axiom, diagnostics=diagnostics,
        candles_repo=candles_repo,
        reporter=DivergenceReporter(build_strategy_at_boundary),
        quote_store=store,
        clock=lambda: 5_000,
    )

    reg = StrategyRegistry(build_strategy)
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)

    await engine.process_candle(cell=cell, candle=_history(8)[-1], registry=reg)

    # Ascending-rate history → rate_percentile strategy emits POST
    quote = store.get_active(cell.cell_id, now_ms=5_000)
    assert quote is not None
    assert quote.outcome == DecisionOutcome.POST
    assert quote.rate is not None
    assert quote.period_days is not None
    assert quote.created_at_ms == 5_000
