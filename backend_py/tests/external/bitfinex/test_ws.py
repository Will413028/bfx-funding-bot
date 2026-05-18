from __future__ import annotations

import asyncio
import json
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
                if candles:
                    await client.close()
                    break

        await asyncio.wait_for(collect(), timeout=2.0)
        assert candles[0].symbol == "fUSD"
        assert candles[0].period_agg == "a30"
        assert candles[0].mts == 1747584000000
