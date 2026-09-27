"""Shared nonce source and request gate for Bitfinex auth (REST + executor + WS).

Bitfinex enforces ONE strictly-increasing nonce PER API KEY, shared across REST
*and* WS auth (docs.bitfinex.com/docs/requirements-and-limitations: "multiple
HTTP OR WebSocket connections ... need separate API keys ... to avoid requests
being rejected on account of an incorrect nonce"). All auth clients in this
daemon share ONE key, so they MUST draw nonces from ONE monotonic source —
otherwise a smaller nonce (the auth WS's ms-scale default vs the REST client's
µs-scale) is rejected as "nonce: small" and that connection can never auth
(this is what left the auth WS flapping forever, fill path silently on REST
reconcile only).

Scale is standardized UP to microseconds: the key has already emitted µs-scale
nonces, so a ms-scale value would be permanently rejected.

A monotonic *generator* is not enough, though: the venue checks the order in
which requests ARRIVE. A caller that takes nonce N and then awaits HTTP can be
overtaken by a concurrent caller holding N+1; when N lands second it is
rejected (Bitfinex answers HTTP 500 with a nonce error body). So the shared
object is an :class:`AuthRequestGate` that owns both the counter and a lock, and
every signed request holds the gate from taking its nonce until its response
arrives (or it fails / times out). Arrival order then equals nonce order.
Order-path requests (submit / cancel / kill) jump ahead of queued reads.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

log = logging.getLogger(__name__)


def make_monotonic_us_nonce() -> Callable[[], int]:
    """One strictly-increasing µs-scale nonce source. Two calls in the same
    microsecond still return distinct increasing values (the `last + 1` term).

    Generation needs no lock (one asyncio thread, no `await` between reading and
    writing `last`), but generation order is not arrival order: share it only
    through :class:`AuthRequestGate`, never as a bare callable across clients.
    """
    last = 0

    def nonce() -> int:
        nonlocal last
        last = max(last + 1, int(time.time() * 1_000_000))
        return last

    return nonce


GateKind = Literal["order", "read"]

# A wait longer than this is logged: it means an order-path request sat behind
# a slow in-flight call, or reads are piling up behind each other.
SLOW_GATE_WAIT_S = 2.0


class AuthRequestGate:
    """The one nonce counter + priority lock for every signed call on one key.

    Hold :meth:`nonce` across "take nonce -> sign -> send -> receive response";
    exiting the block (normally, by exception, timeout or cancellation) hands
    the gate to the next waiter. Held per request, never per paging loop.

    Two waiter classes. ``"order"`` (submit, cancel, cancel-all -- the kill
    path) always goes next after the in-flight request; ``"read"`` (reconcile,
    history syncs, the WS handshake) only proceeds when no order is waiting.
    So a kill waits for at most ONE in-flight request plus earlier orders, never
    for a queue of reads -- which is why reads also carry a short total
    deadline (see auth_rest.READ_DEADLINE_S).

    One gate per key per process: it cannot order requests made by another
    process on the same key.
    """

    def __init__(
        self,
        nonce_source: Callable[[], int] | None = None,
        *,
        slow_wait_s: float = SLOW_GATE_WAIT_S,
    ) -> None:
        self._nonce_source = nonce_source or make_monotonic_us_nonce()
        self._slow_wait_s = slow_wait_s
        self._held = False
        self._waiters: dict[GateKind, deque[asyncio.Future[None]]] = {
            "order": deque(), "read": deque(),
        }

    @asynccontextmanager
    async def nonce(self, kind: GateKind, *, label: str = "") -> AsyncIterator[int]:
        """Wait for the gate (orders before reads), then yield a fresh nonce."""
        nonce = await self.acquire(kind, label=label)
        try:
            yield nonce
        finally:
            self.release()

    async def acquire(self, kind: GateKind, *, label: str = "") -> int:
        """Explicit form of :meth:`nonce` for a caller that must tell a
        cancellation *while waiting* (nothing sent) from one after entry. The
        caller owns the gate on return and must call :meth:`release`."""
        await self._acquire(kind, label)
        return self._nonce_source()

    def release(self) -> None:
        self._release()

    @property
    def busy(self) -> bool:
        return self._held

    def waiting(self, kind: GateKind) -> int:
        return sum(1 for f in self._waiters[kind] if not f.done())

    def _can_enter(self, kind: GateKind) -> bool:
        if self._held or self.waiting("order"):
            return False
        return kind == "order" or not self.waiting("read")

    async def _acquire(self, kind: GateKind, label: str) -> None:
        if self._can_enter(kind):  # no await: uncontended entry never yields
            self._held = True
            return
        started = time.monotonic()
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters[kind].append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # Handed the gate in the same tick we were cancelled: pass it on.
                self._release()
            raise
        finally:
            with contextlib.suppress(ValueError):
                self._waiters[kind].remove(fut)
            waited = time.monotonic() - started
            if waited > self._slow_wait_s:
                log.warning(
                    "bitfinex_auth_gate_slow_wait kind=%s label=%s waited_s=%.2f "
                    "orders_waiting=%d reads_waiting=%d",
                    kind, label, waited, self.waiting("order"), self.waiting("read"),
                )

    def _release(self) -> None:
        for kind in ("order", "read"):
            queue = self._waiters[kind]
            while queue:
                fut = queue.popleft()
                if not fut.done():
                    fut.set_result(None)  # ownership passes directly; _held stays
                    return
        self._held = False
