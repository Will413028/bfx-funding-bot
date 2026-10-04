"""build_executor factory: Bitfinex executor build path + CC4 ws_client_enabled invariant."""
from typing import Any

import httpx
import pytest

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.registry import (
    ExecutorConfigError,
    build_executor,
)
from bfx_funding_bot.modules.strategy import StrategyName


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("BFX_EXECUTOR", "BFX_FILL_TRACKER_ENABLED", "BFX_WS_CLIENT_ENABLED",
              "BFX_API_KEY", "BFX_API_SECRET"):
        monkeypatch.delenv(k, raising=False)


def test_bitfinex_live_builder_does_not_require_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    from bfx_funding_bot.modules.execution.bus import DomainEventBus

    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.LIVE,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
    )
    assert spec.ws_client_enabled is True


def test_bitfinex_live_without_ws_client_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    # ws_client_enabled defaults false → invalid
    with pytest.raises(ExecutorConfigError, match="BFX_WS_CLIENT_ENABLED"):
        build_executor(
            event_sink=_EventCapture(), phase=Phase.LIVE,
            strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="C-1",
        )


def test_bitfinex_live_without_http_or_bus_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    with pytest.raises(ExecutorConfigError, match="http"):
        build_executor(
            event_sink=_EventCapture(), phase=Phase.LIVE,
            strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="C-1",
        )


def test_bitfinex_live_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")

    from bfx_funding_bot.modules.execution.bus import DomainEventBus
    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.LIVE,
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
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")

    from bfx_funding_bot.modules.execution.bus import DomainEventBus
    sentinel = AuthRequestGate()
    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.LIVE,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
        auth_gate=sentinel,
    )
    assert spec.executor._auth_gate is sentinel  # type: ignore[attr-defined]


@pytest.mark.parametrize("value", ["paper", "bitfinex_live", "foo", ""])
def test_bfx_executor_env_is_ignored(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The executor knob is gone: whatever it holds, the Bitfinex executor is built."""
    from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
    from bfx_funding_bot.modules.execution.bus import DomainEventBus

    monkeypatch.setenv("BFX_EXECUTOR", value)
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    spec = build_executor(
        event_sink=_EventCapture(), phase=Phase.LIVE,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
    )
    assert isinstance(spec.executor, BitfinexLiveExecutor)
