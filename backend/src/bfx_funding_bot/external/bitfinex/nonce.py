"""Shared monotonic nonce source for Bitfinex auth (REST + executor + WS).

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
"""
from __future__ import annotations

import time
from collections.abc import Callable


def make_monotonic_us_nonce() -> Callable[[], int]:
    """One strictly-increasing µs-scale nonce source to share across every auth
    client on a single API key. Two calls in the same microsecond still return
    distinct increasing values (the `last + 1` term), which also closes the
    latent same-µs collision between the REST and executor clients.

    Race-free without a lock: the daemon is one asyncio process/thread and the
    body has no `await` between reading and writing `last`.
    # ponytail: no lock — single-process cooperative daemon. Add one only if
    # auth ever runs from multiple threads/processes on the same key.
    """
    last = 0

    def nonce() -> int:
        nonlocal last
        last = max(last + 1, int(time.time() * 1_000_000))
        return last

    return nonce
