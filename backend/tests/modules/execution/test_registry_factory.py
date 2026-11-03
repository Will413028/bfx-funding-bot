"""build_executor factory: the Bitfinex executor build path on either venue."""
import httpx
import pytest

from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.registry import (
    ExecutorConfigError,
    build_executor,
)


def _clock() -> int:
    return 1_700_000_000_000


_COMPOSITION = {"clock": _clock}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("BFX_EXECUTOR", "BFX_FILL_TRACKER_ENABLED", "BFX_WS_CLIENT_ENABLED",
              "BFX_API_KEY", "BFX_API_SECRET"):  # the removed flags must change nothing
        monkeypatch.delenv(k, raising=False)


def test_bitfinex_live_builder_does_not_require_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = build_executor(
        **_COMPOSITION, configured_symbols=frozenset({"fUST"}), http=httpx.AsyncClient(),
    )
    assert spec.executor is not None


def test_bitfinex_live_without_http_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    with pytest.raises(ExecutorConfigError, match=r"build_executor\(\) requires an http client"):
        build_executor(**_COMPOSITION, configured_symbols=frozenset({"fUST"}))


def test_bitfinex_live_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")

    http = httpx.AsyncClient()
    spec = build_executor(**_COMPOSITION, configured_symbols=frozenset({"fUST"}), http=http)
    assert isinstance(spec.executor, BitfinexLiveExecutor)
    assert spec.executor._http is http


def test_bitfinex_live_forwards_shared_auth_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shared per-key nonce MUST reach the live executor — a forgotten forward
    # is exactly the µs/ms divergence class this wiring fixes.
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")

    sentinel = AuthRequestGate()
    spec = build_executor(
        **_COMPOSITION, configured_symbols=frozenset({"fUST"}),
        http=httpx.AsyncClient(), auth_gate=sentinel,
    )
    assert spec.executor._auth_gate is sentinel


@pytest.mark.parametrize("value", ["paper", "bitfinex_live", "foo", ""])
def test_bfx_executor_env_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The executor knob is gone: a set value is a config error, never silently ignored."""
    monkeypatch.setenv("BFX_EXECUTOR", value)
    with pytest.raises(ExecutorConfigError, match="BFX_EXECUTOR"):
        build_executor(
            **_COMPOSITION, configured_symbols=frozenset({"fUST"}), http=httpx.AsyncClient(),
        )


def test_the_executor_gets_the_composition_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: the registry stops forwarding ``clock`` (wall time again)."""
    spec = build_executor(
        **_COMPOSITION, configured_symbols=frozenset({"fUST"}), http=httpx.AsyncClient(),
    )
    assert spec.executor._clock is _clock


def test_the_simulated_venue_builds_the_same_executor() -> None:
    """The simulated venue differs only in the HTTP client handed in; the configured
    symbols reach the executor unchanged (and default to none)."""
    spec = build_executor(
        **_COMPOSITION, configured_symbols=frozenset({"fUST"}), http=httpx.AsyncClient(),
    )
    assert isinstance(spec.executor, BitfinexLiveExecutor)
    assert spec.executor._configured_symbols == frozenset({"fUST"})
    bare = build_executor(**_COMPOSITION, http=httpx.AsyncClient())
    assert bare.executor._configured_symbols == frozenset()
