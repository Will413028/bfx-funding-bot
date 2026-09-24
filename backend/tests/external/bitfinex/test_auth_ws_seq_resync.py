"""auth_ws fires on_resync_needed on reconnect and on a public-seq gap, and sends
the SEQ_ALL conf frame on connect."""
import asyncio
import contextlib
import json
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import serve as ws_serve

from bfx_funding_bot.external.bitfinex.auth_ws import (
    SEQ_ALL_FLAG,
    BitfinexAuthWSClient,
)
from bfx_funding_bot.modules.execution.protocols import Credentials


class _SeqServer:
    """After auth, optionally push a scripted list of channel frames."""

    def __init__(self, frames: list[list] | None = None) -> None:
        self.received: list[dict] = []
        self.connections: list = []
        self._frames = frames or []

    async def handler(self, websocket: Any) -> None:
        self.connections.append(websocket)
        try:
            async for msg in websocket:
                data = json.loads(msg)
                self.received.append(data)
                if data.get("event") == "auth":
                    await websocket.send(json.dumps({
                        "event": "auth", "status": "OK", "chanId": 0, "userId": 1,
                    }))
                    for fr in self._frames:
                        await websocket.send(json.dumps(fr))
        except websockets.ConnectionClosed:
            pass


async def _serve(server: _SeqServer):
    s = await ws_serve(server.handler, "127.0.0.1", 0)
    port = s.sockets[0].getsockname()[1]
    return s, f"ws://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_conf_seq_all_sent_on_connect():
    server = _SeqServer()
    s, url = await _serve(server)
    client = BitfinexAuthWSClient(creds=Credentials(api_key="k", api_secret="s"),
                                  url=url, nonce_provider=lambda: 1)

    async def run():
        async for _ in client.events():
            break

    task = asyncio.create_task(run())
    deadline = asyncio.get_running_loop().time() + 3.0
    while not any(
        m.get("event") == "conf" and m.get("flags") == SEQ_ALL_FLAG
        for m in server.received
    ):
        if asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.02)
    await client.close()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    s.close()
    await s.wait_closed()

    assert any(
        m.get("event") == "conf" and m.get("flags") == SEQ_ALL_FLAG
        for m in server.received
    )


@pytest.mark.asyncio
async def test_reconnect_fires_resync_but_first_connect_does_not():
    server = _SeqServer()
    s, url = await _serve(server)
    reasons: list[str] = []
    client = BitfinexAuthWSClient(
        creds=Credentials(api_key="k", api_secret="s"), url=url,
        nonce_provider=lambda: 1, on_resync_needed=reasons.append,
    )

    async def run():
        with contextlib.suppress(Exception):
            async for _ in client.events():
                await asyncio.sleep(0.02)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.3)
    assert "reconnect" not in reasons  # first connection: no resync

    for ws in list(server.connections):
        await ws.close()  # force a client reconnect
    deadline = asyncio.get_running_loop().time() + 5.0
    while "reconnect" not in reasons and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)

    await client.close()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    s.close()
    await s.wait_closed()

    assert "reconnect" in reasons


@pytest.mark.asyncio
async def test_public_seq_gap_fires_seq_gap_resync():
    # Two heartbeats with a gap: seq 10 then 13 → "seq_gap".
    server = _SeqServer(frames=[[0, "hb", 10], [0, "hb", 13]])
    s, url = await _serve(server)
    reasons: list[str] = []
    client = BitfinexAuthWSClient(
        creds=Credentials(api_key="k", api_secret="s"), url=url,
        nonce_provider=lambda: 1, on_resync_needed=reasons.append,
    )

    async def run():
        with contextlib.suppress(Exception):
            n = 0
            async for _ in client.events():
                n += 1
                if n >= 3:  # AuthAck + hb(seq=10) + hb(seq=13)
                    break

    task = asyncio.create_task(run())
    await asyncio.wait_for(task, timeout=3.0)
    await client.close()
    s.close()
    await s.wait_closed()

    assert "seq_gap" in reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [20051, 20061])
async def test_venue_reconnect_notice_reconnects_and_resyncs(code: int):
    """The venue asks for a fresh connection without closing this one; the client
    must reconnect on its own, and the reconnect resyncs the ledger."""
    server = _SeqServer(frames=[{"event": "info", "code": code}])  # type: ignore[list-item]
    s, url = await _serve(server)
    reasons: list[str] = []
    client = BitfinexAuthWSClient(
        creds=Credentials(api_key="k", api_secret="s"), url=url,
        nonce_provider=lambda: 1, on_resync_needed=reasons.append,
    )

    async def run():
        with contextlib.suppress(Exception):
            async for _ in client.events():
                pass

    task = asyncio.create_task(run())
    deadline = asyncio.get_running_loop().time() + 5.0
    while "reconnect" not in reasons and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)

    await client.close()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    s.close()
    await s.wait_closed()

    assert "reconnect" in reasons
    assert len(server.connections) >= 2
