"""The public funding-trades WS client: frames in, callbacks out, reconnects with backoff."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from bfx_funding_bot.external.bitfinex.funding_trades_ws import FundingTradesWSClient
from bfx_funding_bot.external.bitfinex.rest import FundingTrade


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.now = 1_000

    def client(self, symbols=("fUST", "fUSD"), **kwargs: Any) -> FundingTradesWSClient:
        return FundingTradesWSClient(
            symbols=symbols,
            on_trades=lambda symbol, rows: self.events.append(("trades", symbol, rows)),
            on_alive=lambda symbol, at: self.events.append(("alive", symbol, at)),
            on_connected=lambda: self.events.append(("connected",)),
            on_disconnected=lambda: self.events.append(("disconnected",)),
            clock_ms=lambda: self.now, **kwargs)


def _subscribed(client: FundingTradesWSClient) -> None:
    client.handle_raw(json.dumps(
        {"event": "subscribed", "channel": "trades", "chanId": 7, "symbol": "fUST"}))
    client.handle_raw(json.dumps(
        {"event": "subscribed", "channel": "trades", "chanId": 8, "symbol": "fUSD"}))


def test_it_subscribes_to_the_public_trades_channel_of_every_symbol() -> None:
    frames = [json.loads(f) for f in Recorder().client().subscription_frames()]
    assert frames == [{"event": "subscribe", "channel": "trades", "symbol": "fUST"},
                      {"event": "subscribe", "channel": "trades", "symbol": "fUSD"}]


def test_connected_fires_only_when_every_symbol_is_subscribed() -> None:
    rec = Recorder()
    client = rec.client()
    client.handle_raw(json.dumps(
        {"event": "subscribed", "channel": "trades", "chanId": 7, "symbol": "fUST"}))
    assert rec.events == []
    client.handle_raw(json.dumps(
        {"event": "subscribed", "channel": "trades", "chanId": 8, "symbol": "fUSD"}))
    assert rec.events == [("connected",)]


def test_snapshot_executions_and_heartbeats_become_trades_and_liveness() -> None:
    rec = Recorder()
    client = rec.client()
    _subscribed(client)
    rec.events.clear()
    rec.now = 5_000
    client.handle_raw(json.dumps([7, [[11, 4_000, -250.5, 0.0002, 2], [12, 4_500, 10, 0.0003, 30]]]))
    client.handle_raw(json.dumps([7, "fte", [13, 4_900, -75, 0.0002, 2]]))
    client.handle_raw(json.dumps([7, "ftu", [13, 4_900, -75, 0.0002, 2]]))  # a repeat: ignored
    rec.now = 6_000
    client.handle_raw(json.dumps([8, "hb"]))
    client.handle_raw(json.dumps([99, "hb"]))  # an unknown channel
    assert rec.events == [
        ("trades", "fUST", [FundingTrade(11, 4_000, -250.5, 0.0002, 2),
                            FundingTrade(12, 4_500, 10.0, 0.0003, 30)]),
        ("alive", "fUST", 5_000),
        ("trades", "fUST", [FundingTrade(13, 4_900, -75.0, 0.0002, 2)]),
        ("alive", "fUST", 5_000),
        ("alive", "fUST", 5_000),  # the ftu frame is still a frame of the channel
        ("alive", "fUSD", 6_000),
    ]


def test_a_malformed_row_is_dropped_without_liveness_or_a_crash() -> None:
    rec = Recorder()
    client = rec.client()
    _subscribed(client)
    rec.events.clear()
    client.handle_raw("not json")
    client.handle_raw(json.dumps([7, "fte", [1, 2]]))
    assert rec.events == []


class FakeConnection:
    """One scripted connection: frames to deliver, then it closes (or hangs)."""

    def __init__(self, frames: list[str], *, hang: bool = False) -> None:
        self.frames, self.hang = frames, hang
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> FakeConnection:
        return self

    async def __anext__(self) -> str:
        if self.frames:
            await asyncio.sleep(0)
            return self.frames.pop(0)
        if self.hang:
            await asyncio.Event().wait()
        raise StopAsyncIteration


SUB = [json.dumps({"event": "subscribed", "channel": "trades", "chanId": 7, "symbol": "fUST"})]


async def test_run_reconnects_after_a_dropped_connection_and_reports_each_edge() -> None:
    rec = Recorder()
    stop = asyncio.Event()
    first = FakeConnection([*SUB, json.dumps([7, "hb"])])
    second = FakeConnection(list(SUB), hang=True)
    scripted = [first, second]

    async def connect(url: str) -> FakeConnection:
        return scripted.pop(0)

    client = rec.client(symbols=("fUST",), connect=connect, backoff=lambda attempt: 0)
    task = asyncio.create_task(client.run(stop))
    for _ in range(200):
        if scripted == [] and rec.events.count(("connected",)) == 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    kinds = [e[0] for e in rec.events]
    assert kinds[:4] == ["connected", "alive", "disconnected", "connected"]
    assert first.closed and second.closed
    assert first.sent == second.sent == list(client.subscription_frames())


async def test_a_failing_connect_backs_off_and_reports_no_edge() -> None:
    rec = Recorder()
    stop = asyncio.Event()
    attempts = {"n": 0}

    async def connect(url: str) -> FakeConnection:
        attempts["n"] += 1
        if attempts["n"] >= 2:
            stop.set()
        raise OSError("refused")

    client = rec.client(symbols=("fUST",), connect=connect, backoff=lambda attempt: 0)
    await asyncio.wait_for(client.run(stop), 5)
    assert rec.events == []  # never connected, so never "disconnected"
    assert attempts["n"] >= 2
