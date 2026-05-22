"""Tests for BitfinexAuthWSClient I/O shell lifecycle (Task 12 — Phase 4.4a)."""
import asyncio
import contextlib
import json
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import serve as ws_serve

from bfx_funding_bot.external.bitfinex.auth_ws import (
    AuthAck,
    BitfinexAuthWSClient,
)
from bfx_funding_bot.modules.execution.protocols import Credentials


class _ServerState:
    def __init__(self) -> None:
        self.auth_received = False
        self.connections: list = []

    async def handler(self, websocket: Any) -> None:
        self.connections.append(websocket)
        try:
            async for msg in websocket:
                data = json.loads(msg)
                if data.get("event") == "auth":
                    self.auth_received = True
                    await websocket.send(json.dumps({
                        "event": "auth", "status": "OK",
                        "chanId": 0, "userId": 1234,
                    }))
        except websockets.ConnectionClosed:
            pass


@pytest.fixture
async def fake_bfx_ws_server():
    state = _ServerState()
    server = await ws_serve(state.handler, "127.0.0.1", 0)
    # websockets.asyncio.server.Server: get bound port via sockets attribute
    sockets = server.sockets
    port = sockets[0].getsockname()[1]
    try:
        yield state, f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_auth_sends_auth_payload_on_connect(fake_bfx_ws_server: Any) -> None:
    server_state, url = fake_bfx_ws_server
    creds = Credentials(api_key="k", api_secret="s")
    client = BitfinexAuthWSClient(
        creds=creds, url=url, hb_timeout_s=10.0,
        nonce_provider=lambda: 1000,
    )

    collected: list = []

    async def collect() -> None:
        async for ev in client.events():
            collected.append(ev)
            if len(collected) >= 1:
                break

    task = asyncio.create_task(collect())
    await asyncio.wait_for(task, timeout=3.0)
    await client.close()

    assert server_state.auth_received is True
    assert len(collected) == 1
    assert isinstance(collected[0], AuthAck)
    assert collected[0].status == "OK"


@pytest.mark.asyncio
async def test_close_terminates_iterator(fake_bfx_ws_server: Any) -> None:
    _server_state, url = fake_bfx_ws_server
    creds = Credentials(api_key="k", api_secret="s")
    client = BitfinexAuthWSClient(creds=creds, url=url, nonce_provider=lambda: 1000)

    async def run() -> None:
        async for _ in client.events():
            break

    task = asyncio.create_task(run())
    await asyncio.sleep(0.3)
    await client.close()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_reconnect_count_increments_on_disconnect(fake_bfx_ws_server: Any) -> None:
    server_state, url = fake_bfx_ws_server
    creds = Credentials(api_key="k", api_secret="s")
    client = BitfinexAuthWSClient(
        creds=creds, url=url, hb_timeout_s=0.5,
        nonce_provider=lambda: 1000,
    )

    async def collect() -> None:
        try:
            async for _ in client.events():
                await asyncio.sleep(0.05)
        except Exception:
            pass

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.3)

    # Close server-side connection(s) to trigger client reconnect
    for ws in server_state.connections:
        await ws.close()

    await asyncio.sleep(1.5)  # let backoff + reconnect attempt happen
    assert client.reconnect_attempts >= 1

    await client.close()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
