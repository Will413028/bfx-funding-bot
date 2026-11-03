"""build_executor factory.

The executor is always the Bitfinex live executor: on the ``simulated`` venue it talks to
the simulated venue's own ``httpx`` client, so the code that runs is the code that runs
live. ``BFX_EXECUTOR`` is refused, not ignored.

Whether the authenticated WebSocket runs is not the executor's to decide: the composition
root reads it from the venue wiring's ``VenueCapabilities`` (CC4), never a flag or a name.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate


class ExecutorConfigError(RuntimeError):
    """Inconsistent env config — daemon should not start."""


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    executor: BitfinexLiveExecutor


def build_executor(
    *,
    clock: Callable[[], int],
    configured_symbols: frozenset[str] | None = None,
    http: httpx.AsyncClient | None = None,
    auth_gate: AuthRequestGate | None = None,
) -> ExecutorSpec:
    if "BFX_EXECUTOR" in os.environ:
        raise ExecutorConfigError("BFX_EXECUTOR is removed: the venue follows BFX_PHASE")
    if http is None:
        raise ExecutorConfigError("build_executor() requires an http client")
    return ExecutorSpec(
        executor=BitfinexLiveExecutor(
            http=http,
            configured_symbols=configured_symbols or frozenset(),
            auth_gate=auth_gate,
            clock=clock,
        ),
    )
