"""SignalEngine: strategy decision + standing quote write (post-refactor).

Phase 4.2 / Task 5 refactor — verifies the decoupled signal layer:
1. Single DECISION event per cycle (not 2 — event-sourcing best practice)
2. POST path: standing quote written with correct rate/period/created_at_ms
3. SKIP path: no active quote (store returns None for below_threshold)
4. Staleness metadata survives on the DECISION record (POST path)

Safety evaluation and executor.submit now live in DeploymentReconciler;
those behaviors are tested in the deployment reconciler test suite.
"""
from __future__ import annotations

from typing import Any

import pytest

from bfx_funding_bot.core.telemetry import EventType
from bfx_funding_bot.modules.strategy import DecisionOutcome, SkipReason


@pytest.mark.asyncio
async def test_decision_emitted_once_post_and_quote_written(
    capture_engine: Any,
) -> None:
    """Single DECISION event per cycle; standing quote written on POST."""
    engine, _axiom, diagnostics, store, cell, candle, registry = capture_engine
    await engine.process_candle(cell=cell, candle=candle, registry=registry)

    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision_outcome"] == DecisionOutcome.POST.value

    # Standing quote must be written and active
    quote = store.get_active(cell.cell_id, now_ms=5_000)
    assert quote is not None
    assert quote.outcome == DecisionOutcome.POST
    assert quote.rate is not None
    assert quote.period_days is not None
    assert quote.created_at_ms == 5_000


@pytest.mark.asyncio
async def test_decision_emitted_once_skip_no_active_quote(
    capture_engine_blocked: Any,
) -> None:
    """Single DECISION event per cycle; no active quote on SKIP/below_threshold."""
    engine, _axiom, diagnostics, store, cell, candle, registry = capture_engine_blocked
    await engine.process_candle(cell=cell, candle=candle, registry=registry)

    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision_outcome"] == DecisionOutcome.SKIP.value
    assert decisions[0]["payload"]["skip_reason"] == SkipReason.BELOW_THRESHOLD.value

    # SKIP → no active quote (outcome != POST so get_active returns None)
    assert store.get_active(cell.cell_id, now_ms=5_000) is None


@pytest.mark.asyncio
async def test_decision_carries_staleness_metadata_on_post(
    capture_engine: Any,
) -> None:
    """Durable DECISION (→ PG diagnostics) must carry the staleness dimension so
    canary outcomes can be sliced stale-vs-fresh from the SoT (not just the
    ephemeral SIGNAL on stdout). cell.staleness_budget_hours=2 → 7200s."""
    engine, _axiom, diagnostics, _store, cell, candle, registry = capture_engine
    await engine.process_candle(
        cell=cell, candle=candle, registry=registry,
        is_stale=True, stale_seconds=900,
    )
    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    payload = decisions[0]["payload"]
    assert payload["decision_outcome"] == DecisionOutcome.POST.value
    assert payload["is_stale"] is True
    assert payload["stale_seconds"] == 900
    assert payload["budget_seconds"] == 7200


@pytest.mark.asyncio
async def test_decision_preserves_staleness_metadata_on_skip(
    capture_engine_blocked: Any,
) -> None:
    """Staleness dimension must ride the DECISION record regardless of outcome."""
    engine, _axiom, diagnostics, _store, cell, candle, registry = capture_engine_blocked
    await engine.process_candle(
        cell=cell, candle=candle, registry=registry,
        is_stale=True, stale_seconds=1234,
    )
    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    payload = decisions[0]["payload"]
    assert payload["decision_outcome"] == DecisionOutcome.SKIP.value
    assert payload["skip_reason"] == SkipReason.BELOW_THRESHOLD.value
    assert payload["is_stale"] is True
    assert payload["stale_seconds"] == 1234
    assert payload["budget_seconds"] == 7200
