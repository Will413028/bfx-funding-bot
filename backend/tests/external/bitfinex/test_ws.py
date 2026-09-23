from __future__ import annotations

import asyncio
import json
import time as time_module
from typing import Any

import websockets

from bfx_funding_bot.external.bitfinex.ws import (
    BitfinexWSClient,
    CandleMessage,
    ChannelSpec,
)


class FakeBitfinexWSServer:
    """In-process fake WS server emulating Bitfinex subscribe + candles + hb protocol."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self._chan_id = 100

    async def handler(self, websocket):
        await websocket.send(json.dumps({"event": "info", "version": 2}))
        async for raw in websocket:
            msg = json.loads(raw)
            if msg.get("event") == "subscribe":
                self._chan_id += 1
                chan_id = self._chan_id
                await websocket.send(json.dumps({
                    "event": "subscribed", "channel": "candles",
                    "chanId": chan_id, "key": msg["key"],
                }))
                await websocket.send(json.dumps([chan_id, [
                    [1747584000000, 0.0001, 0.0001, 0.0001, 0.0001, 100.0],
                ]]))
                await websocket.send(json.dumps([chan_id, "hb"]))
                self.sent.append((chan_id, msg["key"]))


async def test_connect_and_subscribe_yields_candle(unused_tcp_port: int):
    server_state = FakeBitfinexWSServer()
    async with websockets.serve(server_state.handler, "127.0.0.1", unused_tcp_port):
        client = BitfinexWSClient(
            url=f"ws://127.0.0.1:{unused_tcp_port}",
            channels=[ChannelSpec(symbol="fUSD", timeframe="1h", period_agg="a30")],
        )
        candles: list[CandleMessage] = []

        async def collect():
            async for c in client.candles():
                candles.append(c)
                await client.close()
                break

        await asyncio.wait_for(collect(), timeout=2.0)
        assert candles[0].symbol == "fUSD"
        assert candles[0].period_agg == "a30"
        assert candles[0].mts == 1747584000000


async def test_hb_timeout_triggers_disconnect_callback(unused_tcp_port: int):
    server_state = FakeBitfinexWSServer()
    async with websockets.serve(server_state.handler, "127.0.0.1", unused_tcp_port):
        disconnects: list[str] = []
        client = BitfinexWSClient(
            url=f"ws://127.0.0.1:{unused_tcp_port}",
            channels=[ChannelSpec(symbol="fUSD", timeframe="1h", period_agg="a30")],
            hb_timeout_s=0.3,
            on_disconnect=lambda reason: disconnects.append(reason),
        )
        # candles() self-terminates once the watchdog drops the connection
        # (no external cancel needed — that is the Fix A contract). The drain
        # therefore returns on its own within the timeout.
        await asyncio.wait_for(_drain(client), timeout=2.0)
        await client.close()

    assert any("hb_timeout" in r for r in disconnects)


async def test_candles_terminates_when_connection_drops(unused_tcp_port: int):
    """Regression (canary 2026-05-30): when the hb watchdog detects staleness and
    closes the socket, candles() MUST terminate so the daemon's reconnect loop
    (which is driven by the iterator raising/returning) can run. Before the fix
    candles() blocked forever on the empty queue -> 5 hb_timeout / 0 reconnect,
    WS hung until a Koyeb restart.

    The server sends one candle + hb then goes silent; the watchdog fires at
    hb_timeout_s and drops the connection. The consumer must end ON ITS OWN
    (no external cancel) within the timeout."""
    server_state = FakeBitfinexWSServer()
    async with websockets.serve(server_state.handler, "127.0.0.1", unused_tcp_port):
        client = BitfinexWSClient(
            url=f"ws://127.0.0.1:{unused_tcp_port}",
            channels=[ChannelSpec(symbol="fUSD", timeframe="1h", period_agg="a30")],
            hb_timeout_s=0.3,
        )
        seen = 0

        async def consume() -> None:
            nonlocal seen
            async for _ in client.candles():
                seen += 1

        # No cancel(): the iterator itself must return after the watchdog drops the
        # connection. Blocks forever (→ TimeoutError) before the fix.
        await asyncio.wait_for(consume(), timeout=3.0)
        assert seen >= 1  # consumed the candle, then terminated on disconnect
        await client.close()


class _PeerCloseServer:
    """Sends one candle then closes the connection (peer-initiated close)."""

    async def handler(self, websocket):
        await websocket.send(json.dumps({"event": "info", "version": 2}))
        async for raw in websocket:
            msg = json.loads(raw)
            if msg.get("event") == "subscribe":
                await websocket.send(json.dumps({
                    "event": "subscribed", "channel": "candles",
                    "chanId": 101, "key": msg["key"],
                }))
                await websocket.send(json.dumps([101, [
                    [1747584000000, 0.0001, 0.0001, 0.0001, 0.0001, 100.0],
                ]]))
                await websocket.close(code=1006)
                return


async def test_candles_terminates_on_peer_close(unused_tcp_port: int):
    """The other disconnect entry point: the peer closes the socket (recv loop
    raises ConnectionClosed) rather than the hb watchdog. candles() must still
    terminate on its own so the daemon reconnect loop runs. hb_timeout_s is high
    so this exercises the ConnectionClosed -> sentinel path, not the watchdog."""
    server = _PeerCloseServer()
    async with websockets.serve(server.handler, "127.0.0.1", unused_tcp_port):
        client = BitfinexWSClient(
            url=f"ws://127.0.0.1:{unused_tcp_port}",
            channels=[ChannelSpec(symbol="fUSD", timeframe="1h", period_agg="a30")],
            hb_timeout_s=30.0,
        )
        seen = 0

        async def consume() -> None:
            nonlocal seen
            async for _ in client.candles():
                seen += 1

        await asyncio.wait_for(consume(), timeout=3.0)
        assert seen == 1  # the single candle, then terminated on peer close
        await client.close()


async def _drain(client: BitfinexWSClient):
    try:
        async for _ in client.candles():
            pass
    except Exception:
        pass


def test_backoff_schedule_caps_at_60s():
    from bfx_funding_bot.external.bitfinex.ws import compute_backoff_secs
    assert compute_backoff_secs(1) == 1
    assert compute_backoff_secs(2) == 2
    assert compute_backoff_secs(6) == 32
    assert compute_backoff_secs(7) == 60
    assert compute_backoff_secs(20) == 60


def test_reconnect_attempts_resets_after_5min_alive():
    from bfx_funding_bot.external.bitfinex.ws import BitfinexWSClient
    client = BitfinexWSClient(channels=[], url="ws://invalid")
    client.reconnect_attempts = 3
    client._connected_at = time_module.monotonic() - 301
    client.maybe_reset_backoff()
    assert client.reconnect_attempts == 0
