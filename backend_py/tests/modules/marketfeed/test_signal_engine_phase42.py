"""SignalEngine: post-safety-eval decision emit + executor.submit on allowed.

Phase 4.2 Task 19 — verifies:
1. Single DECISION event per cycle (not 2 — fixes double-emit footgun)
2. Allowed path: outcome=POST + executor.submit called once
3. Blocked path: outcome=SKIP, skip_reason=safety_block, executor NOT called
"""
from __future__ import annotations

from typing import Any

import pytest

from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    EventType,
    SkipReason,
)


@pytest.mark.asyncio
async def test_decision_emitted_once_when_allowed_then_executor_called(
    capture_engine: Any,
) -> None:
    """Sanity: decision event count = 1 per cycle (not 2), executor called once."""
    engine, _axiom, diagnostics, executor, _chain, cell, candle, registry = capture_engine
    await engine.process_candle(cell=cell, candle=candle, registry=registry)
    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision_outcome"] == DecisionOutcome.POST.value
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_decision_emitted_once_when_blocked_executor_not_called(
    capture_engine_blocked: Any,
) -> None:
    engine, _axiom, diagnostics, executor, _chain, cell, candle, registry = capture_engine_blocked
    await engine.process_candle(cell=cell, candle=candle, registry=registry)
    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision_outcome"] == DecisionOutcome.SKIP.value
    assert decisions[0]["payload"]["skip_reason"] == SkipReason.SAFETY_BLOCK.value
    assert executor.calls == []


@pytest.mark.asyncio
async def test_decision_carries_staleness_metadata_on_post(
    capture_engine: Any,
) -> None:
    """Durable DECISION (→ PG diagnostics) must carry the staleness dimension so
    canary outcomes can be sliced stale-vs-fresh from the SoT (not just the
    ephemeral SIGNAL on stdout). cell.staleness_budget_hours=2 → 7200s."""
    engine, _axiom, diagnostics, _executor, _chain, cell, candle, registry = capture_engine
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
async def test_decision_preserves_staleness_metadata_when_blocked(
    capture_engine_blocked: Any,
) -> None:
    """Safety-block rebuilds the DecisionPayload (POST→SKIP); staleness dimension
    must survive the rebuild."""
    engine, _axiom, diagnostics, _executor, _chain, cell, candle, registry = capture_engine_blocked
    await engine.process_candle(
        cell=cell, candle=candle, registry=registry,
        is_stale=True, stale_seconds=1234,
    )
    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    payload = decisions[0]["payload"]
    assert payload["decision_outcome"] == DecisionOutcome.SKIP.value
    assert payload["skip_reason"] == SkipReason.SAFETY_BLOCK.value
    assert payload["is_stale"] is True
    assert payload["stale_seconds"] == 1234
    assert payload["budget_seconds"] == 7200
