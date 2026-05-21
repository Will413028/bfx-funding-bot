"""build_executor: env validation, invariant check (paper+fill_tracker mismatch)."""
from __future__ import annotations

import pytest

from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.registry import (
    ExecutorConfigError,
    build_executor,
)
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName


class _NullAxiom:
    async def emit(self, event: dict) -> None: ...


def test_default_paper_executor_no_fill_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)
    spec = build_executor(
        axiom=_NullAxiom(), phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
    )
    assert isinstance(spec.executor, EchoPaperExecutor)
    assert spec.fill_tracker_enabled is False


def test_paper_executor_with_fill_tracker_enabled_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "paper")
    monkeypatch.setenv("BFX_FILL_TRACKER_ENABLED", "true")
    with pytest.raises(ExecutorConfigError, match="paper"):
        build_executor(
            axiom=_NullAxiom(), phase=Phase.PAPER,
            strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        )


def test_bitfinex_live_executor_not_yet_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    with pytest.raises(ExecutorConfigError, match=r"4\.4"):
        build_executor(
            axiom=_NullAxiom(), phase=Phase.PAPER,
            strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        )
