"""build_executor factory: the Bitfinex executor build path; the auth WS follows the venue (CC4)."""
from datetime import date
from typing import Any

import httpx
import pytest

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.core.venue import VenueCapabilities
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.registry import (
    ExecutorConfigError,
    build_executor,
)
from bfx_funding_bot.modules.strategy import StrategyName


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


def _clock() -> int:
    return 1_700_000_000_000


def _today() -> date:
    return date(2023, 11, 14)


BITFINEX = VenueCapabilities(auth_ws="required")
SIMULATED = VenueCapabilities(auth_ws="forbidden")
_COMPOSITION = {"clock": _clock, "date_provider": _today}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("BFX_EXECUTOR", "BFX_FILL_TRACKER_ENABLED", "BFX_WS_CLIENT_ENABLED",
              "BFX_API_KEY", "BFX_API_SECRET"):  # the removed flags must change nothing
        monkeypatch.delenv(k, raising=False)


def test_bitfinex_live_builder_does_not_require_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bfx_funding_bot.modules.execution.bus import DomainEventBus

    spec = build_executor(
        capabilities=BITFINEX, **_COMPOSITION, event_sink=_EventCapture(), phase=Phase.LIVE,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
    )
    assert spec.ws_client_enabled is True


def test_bitfinex_live_without_http_or_bus_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    with pytest.raises(ExecutorConfigError, match="http"):
        build_executor(
            capabilities=BITFINEX, **_COMPOSITION, event_sink=_EventCapture(), phase=Phase.LIVE,
            strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="C-1",
        )


def test_bitfinex_live_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")

    from bfx_funding_bot.modules.execution.bus import DomainEventBus
    spec = build_executor(
        capabilities=BITFINEX, **_COMPOSITION, event_sink=_EventCapture(), phase=Phase.LIVE,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(),
        bus=DomainEventBus(),
    )
    assert spec.ws_client_enabled is True


def test_bitfinex_live_forwards_shared_auth_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shared per-key nonce MUST reach the live executor — a forgotten forward
    # is exactly the µs/ms divergence class this wiring fixes.
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")

    from bfx_funding_bot.modules.execution.bus import DomainEventBus
    sentinel = AuthRequestGate()
    spec = build_executor(
        capabilities=BITFINEX, **_COMPOSITION, event_sink=_EventCapture(), phase=Phase.LIVE,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
        auth_gate=sentinel,
    )
    assert spec.executor._auth_gate is sentinel  # type: ignore[attr-defined]


@pytest.mark.parametrize("value", ["paper", "bitfinex_live", "foo", ""])
def test_bfx_executor_env_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The executor knob is gone: a set value is a config error, never silently ignored."""
    from bfx_funding_bot.modules.execution.bus import DomainEventBus

    monkeypatch.setenv("BFX_EXECUTOR", value)
    with pytest.raises(ExecutorConfigError, match="BFX_EXECUTOR"):
        build_executor(
            capabilities=BITFINEX, **_COMPOSITION, event_sink=_EventCapture(), phase=Phase.LIVE,
            strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="C-1",
            http=httpx.AsyncClient(), bus=DomainEventBus(),
        )


def test_the_executor_gets_the_composition_clock_and_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: the registry stops forwarding ``clock``/``date_provider`` (wall time again)."""
    from bfx_funding_bot.modules.execution.bus import DomainEventBus

    spec = build_executor(
        capabilities=BITFINEX, **_COMPOSITION, event_sink=_EventCapture(), phase=Phase.LIVE,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
    )
    assert spec.executor._clock is _clock  # type: ignore[attr-defined]
    assert spec.executor._date_provider is _today  # type: ignore[attr-defined]


@pytest.mark.parametrize(("capabilities", "phase", "expected"), [
    (BITFINEX, Phase.LIVE, True), (SIMULATED, Phase.SHADOW, False),
])
@pytest.mark.parametrize("stale_flag", [None, "true", "false"])
def test_the_auth_websocket_follows_the_venue_capabilities(
    monkeypatch: pytest.MonkeyPatch, capabilities: VenueCapabilities, phase: Phase,
    expected: bool, stale_flag: str | None,
) -> None:
    """Mutation: read ``BFX_WS_CLIENT_ENABLED`` again, or invert the capability test."""
    from bfx_funding_bot.modules.execution.bus import DomainEventBus

    if stale_flag is not None:  # a removed flag left in an env file changes nothing
        monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", stale_flag)
    spec = build_executor(
        capabilities=capabilities, **_COMPOSITION, event_sink=_EventCapture(), phase=phase,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
    )
    assert spec.ws_client_enabled is expected


def test_the_simulated_venue_builds_the_same_executor() -> None:
    from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
    from bfx_funding_bot.modules.execution.bus import DomainEventBus

    spec = build_executor(
        capabilities=SIMULATED, **_COMPOSITION, event_sink=_EventCapture(), phase=Phase.SHADOW,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        http=httpx.AsyncClient(), bus=DomainEventBus(),
    )
    assert isinstance(spec.executor, BitfinexLiveExecutor)
    assert spec.ws_client_enabled is False
