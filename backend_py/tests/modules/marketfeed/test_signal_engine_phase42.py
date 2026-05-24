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
