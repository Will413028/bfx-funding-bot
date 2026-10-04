"""Tests for BitfinexAuthWSClient I/O shell lifecycle (Task 12 — Phase 4.4a)."""
import asyncio
import contextlib
import json
import logging
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import serve as ws_serve

from bfx_funding_bot.external.bitfinex.auth_ws import (
    AuthAck,
    BitfinexAuthWSClient,
)
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.protocols import Credentials
from tests.async_wait import until


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
        auth_gate=AuthRequestGate(lambda: 1000),
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
    client = BitfinexAuthWSClient(creds=creds, url=url, auth_gate=AuthRequestGate(lambda: 1000))

    async def run() -> None:
        async for _ in client.events():
            break

    task = asyncio.create_task(run())
    await asyncio.sleep(0.3)
    await client.close()
    await asyncio.wait_for(task, timeout=2.0)


class _AuthStatusServer:
    """Fake WS that replies to `auth` with a configurable status, then stays open."""

    def __init__(self, status: str) -> None:
        self._status = status
        self.connections: list = []
        self.auth_count = 0

    async def handler(self, websocket: Any) -> None:
        self.connections.append(websocket)
        try:
            async for msg in websocket:
                if json.loads(msg).get("event") == "auth":
                    self.auth_count += 1
                    await websocket.send(json.dumps({
                        "event": "auth", "status": self._status,
                        "chanId": 0, "userId": 1234,
                    }))
        except websockets.ConnectionClosed:
            pass


@contextlib.asynccontextmanager
async def _serve(status: str):
    state = _AuthStatusServer(status)
    server = await ws_serve(state.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield state, f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_auth_ok_resets_reconnect_attempts() -> None:
    async with _serve("OK") as (_state, url):
        client = BitfinexAuthWSClient(
            creds=Credentials(api_key="k", api_secret="s"),
            url=url, auth_gate=AuthRequestGate(lambda: 1000),
        )
        client.reconnect_attempts = 7  # simulate cumulative-since-boot climb

        async def collect() -> None:
            async for _ev in client.events():
                break  # first frame is the AuthAck

        task = asyncio.create_task(collect())
        await asyncio.wait_for(task, timeout=3.0)
        await client.close()

    # Genuine auth success clears the backoff counter → next drop starts at 0.
    assert client.reconnect_attempts == 0
    # ...and is counted (health poll reads auth_ok_count + connection_count to
    # tell "connected but never authed" apart from "authed fine").
    assert client.auth_ok_count == 1
    assert client.connection_count >= 1


@pytest.mark.asyncio
async def test_auth_failed_logs_loud_and_drops_into_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with _serve("FAILED") as (_state, url):
        client = BitfinexAuthWSClient(
            creds=Credentials(api_key="k", api_secret="s"),
            url=url, auth_gate=AuthRequestGate(lambda: 1000),
        )

        async def run() -> None:
            async for _ev in client.events():
                pass

        task = asyncio.create_task(run())
        with caplog.at_level(logging.ERROR):
            await asyncio.sleep(0.8)  # connect → auth FAILED → return → backoff
        await client.close()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

    # Loud, not silent; and counted as a failure (dropped into backoff) rather
    # than treated as an authed connection.
    assert "bfx_auth_ws_auth_FAILED" in caplog.text
    assert client.reconnect_attempts >= 1


@pytest.mark.asyncio
async def test_reconnect_count_increments_on_disconnect(fake_bfx_ws_server: Any) -> None:
    server_state, url = fake_bfx_ws_server
    creds = Credentials(api_key="k", api_secret="s")
    client = BitfinexAuthWSClient(
        creds=creds, url=url, hb_timeout_s=0.5,
        auth_gate=AuthRequestGate(lambda: 1000),
    )

    async def collect() -> None:
        try:
            async for _ in client.events():
                await asyncio.sleep(0.05)
        except Exception:
            pass

    task = asyncio.create_task(collect())
    await until(lambda: client.auth_ok_count >= 1, what="the first authenticated connection")

    # Close server-side connection(s) to trigger client reconnect
    for ws in server_state.connections:
        await ws.close()

    await until(lambda: client.reconnect_count_last_hour() >= 1, what="the recorded disconnect")
    # reconnect_attempts now resets on a successful re-auth (consecutive-failure
    # semantics), so assert the durable disconnect history instead.
    assert client.reconnect_count_last_hour() >= 1

    await client.close()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_auth_handshake_waits_for_in_flight_signed_rest(fake_bfx_ws_server: Any) -> None:
    """The handshake spends a nonce on the REST key: it must not be taken while
    a signed REST call (smaller nonce) is still in flight, or that call would
    arrive after it and be rejected "nonce: small"."""
    server_state, url = fake_bfx_ws_server
    gate = AuthRequestGate()
    client = BitfinexAuthWSClient(
        creds=Credentials(api_key="k", api_secret="s"), url=url, auth_gate=gate,
    )

    async def first_event() -> None:
        async for _ in client.events():
            return

    async with gate.nonce("read"):  # a REST request holding the gate
        task = asyncio.create_task(first_event())
        await asyncio.sleep(0.3)
        assert server_state.auth_received is False
    await asyncio.wait_for(task, timeout=3.0)
    assert server_state.auth_received is True
    assert not gate.busy
    await client.close()
