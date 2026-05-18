from __future__ import annotations

import asyncio
import json
import time as time_module
from typing import Any

import pytest
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
        consumer = asyncio.create_task(_drain(client))
        await asyncio.sleep(0.5)
        await client.close()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

    assert any("hb_timeout" in r for r in disconnects)


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
