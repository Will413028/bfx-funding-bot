"""build_executor: env validation, invariant check (paper+fill_tracker mismatch)."""
from __future__ import annotations

import pytest

from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.registry import (
    ExecutorConfigError,
    build_executor,
)
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName


class _EventCapture:
    async def emit(self, event: dict) -> None: ...


def test_default_paper_executor_no_fill_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)
    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.PAPER,
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
            event_sink=_EventCapture(), phase=Phase.PAPER,
            strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        )


def test_bitfinex_live_builder_requires_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    # Credentials are loaded by the account bootstrap, not this factory. The
    # factory still requires its runtime HTTP/event dependencies.
    with pytest.raises(ExecutorConfigError, match=r"http \+ bus"):
        build_executor(
            event_sink=_EventCapture(), phase=Phase.PAPER,
            strategy=StrategyName.MEAN_REVERSION,
            configured_symbols=frozenset({"fUST"}), cell="fUSD_a30",
        )


def test_unknown_executor_kind_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "foo")
    with pytest.raises(ExecutorConfigError, match="unknown"):
        build_executor(
            event_sink=_EventCapture(), phase=Phase.PAPER,
            strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        )
