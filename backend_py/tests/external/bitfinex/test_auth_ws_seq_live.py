"""Gated-live contract: real Bitfinex auth stream with SEQ_ALL on.

Run manually:  BFX_API_KEY=... BFX_API_SECRET=... \
  uv run pytest -m integration tests/external/bitfinex/test_auth_ws_seq_live.py -q

Verifies the public-seq layout `_public_seq` assumes is real, and that the public
seq increments contiguously across consecutive frames (incl. heartbeats). If this
fails, `_public_seq` needs to match the captured layout before seq-gap is trusted.
"""
from __future__ import annotations

import asyncio
import itertools
import os

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import (
    BitfinexAuthWSClient,
    _public_seq_of,
)
from bfx_funding_bot.modules.execution.protocols import Credentials

_KEY = os.environ.get("BFX_API_KEY")
_SECRET = os.environ.get("BFX_API_SECRET")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(not (_KEY and _SECRET), reason="needs BFX_API_KEY/SECRET")
async def test_real_auth_stream_public_seq_is_contiguous() -> None:
    assert _KEY is not None and _SECRET is not None
    client = BitfinexAuthWSClient(creds=Credentials(api_key=_KEY, api_secret=_SECRET))
    seqs: list[int] = []

    async def collect() -> None:
        async for ev in client.events():
            s = _public_seq_of(ev)
            if s is not None:
                seqs.append(s)
            if len(seqs) >= 5:  # auth ack + a few heartbeats
                break

    try:
        await asyncio.wait_for(collect(), timeout=60.0)
    finally:
        await client.close()

    assert len(seqs) >= 2, f"no public seq observed — SEQ_ALL layout wrong? {seqs}"
    # consecutive observed frames must be strictly contiguous (no real loss in 60s)
    assert all(b == a + 1 for a, b in itertools.pairwise(seqs)), f"non-contiguous: {seqs}"
