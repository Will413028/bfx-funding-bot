"""build_executor factory: bitfinex_live build path + CC4 ws_client_enabled invariants."""
from typing import Any

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.registry import (
    ExecutorConfigError,
    build_executor,
)
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("BFX_EXECUTOR", "BFX_FILL_TRACKER_ENABLED", "BFX_WS_CLIENT_ENABLED",
              "BFX_API_KEY", "BFX_API_SECRET"):
        monkeypatch.delenv(k, raising=False)


def test_paper_default_no_ws_no_tracker() -> None:
    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="C-1",
    )
    assert spec.fill_tracker_enabled is False
    assert spec.ws_client_enabled is False


def test_paper_with_ws_client_enabled_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "paper")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    with pytest.raises(ExecutorConfigError, match="BFX_WS_CLIENT_ENABLED"):
        build_executor(
            event_sink=_EventCapture(), phase=Phase.PAPER,
            strategy=StrategyName.RATE_PERCENTILE, cell="C-1",
        )


def test_bitfinex_live_builder_does_not_require_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    from bfx_funding_bot.modules.execution.bus import DomainEventBus

    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
    )
    assert spec.ws_client_enabled is True


def test_bitfinex_live_without_ws_client_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    # ws_client_enabled defaults false → invalid
    with pytest.raises(ExecutorConfigError, match="BFX_WS_CLIENT_ENABLED"):
        build_executor(
            event_sink=_EventCapture(), phase=Phase.PAPER,
            strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="C-1",
        )


def test_bitfinex_live_without_http_or_bus_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    with pytest.raises(ExecutorConfigError, match="http"):
        build_executor(
            event_sink=_EventCapture(), phase=Phase.PAPER,
            strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="C-1",
        )


def test_bitfinex_live_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")

    from bfx_funding_bot.modules.execution.bus import DomainEventBus
    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(),
        bus=DomainEventBus(),
    )
    assert spec.ws_client_enabled is True
    assert spec.fill_tracker_enabled is False


def test_bitfinex_live_forwards_shared_auth_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shared per-key nonce MUST reach the live executor — a forgotten forward
    # is exactly the µs/ms divergence class this wiring fixes.
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")

    from bfx_funding_bot.modules.execution.bus import DomainEventBus
    sentinel = AuthRequestGate()
    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
        auth_gate=sentinel,
    )
    assert spec.executor._auth_gate is sentinel  # type: ignore[attr-defined]
