"""Bitfinex public funding-trades WebSocket (v2 `trades` channel) with its own reconnect loop.

Frames per subscribed symbol (channel id is assigned by the venue per connection):

- snapshot  ``[CHAN_ID, [[ID, MTS, AMOUNT, RATE, PERIOD], ...]]`` right after subscribing;
- executed  ``[CHAN_ID, "fte", [ID, MTS, AMOUNT, RATE, PERIOD]]``;
- update    ``[CHAN_ID, "ftu", [ID, MTS, AMOUNT, RATE, PERIOD]]``, the same trade confirmed;
- heartbeat ``[CHAN_ID, "hb"]``.

Only the snapshot and ``fte`` count (``ftu`` repeats a trade ``fte`` already delivered; the
consumer admits a trade once by id, so a repeat would be harmless only if the ids agree,
which this adapter does not assume). Every frame of a channel, heartbeats included, is
reported through ``on_alive`` with the local receive time: frames of a channel are ordered,
so a heartbeat proves no earlier trade is still in flight. ``on_connected`` fires only when
EVERY symbol is subscribed, ``on_disconnected`` whenever a connection ends.

The consumer owns what these mean (the simulated venue's live feed turns them into a
watermark and a gap backfill); this client only does the I/O and parsing. Reconnects back
off like the other public clients (1, 2, 4 ... 60 s).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import websockets

from bfx_funding_bot.external.bitfinex.rest import FundingTrade
from bfx_funding_bot.external.bitfinex.ws import BITFINEX_WS_URL, compute_backoff_secs

log = logging.getLogger(__name__)


class _Connection(Protocol):
    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> Any: ...


Connect = Callable[[str], Awaitable[_Connection]]


async def _connect(url: str) -> _Connection:
    return await websockets.connect(url, max_size=2**20)


class FundingTradesWSClient:
    def __init__(
        self, *, symbols: Sequence[str],
        on_trades: Callable[[str, list[FundingTrade]], None],
        on_alive: Callable[[str, int], None],
        on_connected: Callable[[], None],
        on_disconnected: Callable[[], None],
        clock_ms: Callable[[], int],
        url: str = BITFINEX_WS_URL, connect: Connect = _connect,
        backoff: Callable[[int], float] = compute_backoff_secs,
    ) -> None:
        self._symbols = tuple(symbols)
        self._on_trades = on_trades
        self._on_alive = on_alive
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._clock_ms = clock_ms
        self._url = url
        self._connect = connect
        self._backoff = backoff
        self._channels: dict[int, str] = {}
        self._subscribed: set[str] = set()
        self._announced = False

    def subscription_frames(self) -> tuple[str, ...]:
        return tuple(
            json.dumps({"event": "subscribe", "channel": "trades", "symbol": symbol})
            for symbol in self._symbols
        )

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            ws: _Connection | None = None
            try:
                ws = await self._connect(self._url)
                for frame in self.subscription_frames():
                    await ws.send(frame)
                attempt = 0
                await self._session(ws, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("funding_trades_ws_error error=%r", exc)
            finally:
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                self._end_session()
            if stop.is_set():
                return
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._backoff(attempt))

    async def _session(self, ws: _Connection, stop: asyncio.Event) -> None:
        reader = asyncio.ensure_future(self._read(ws))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (reader, waiter):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _read(self, ws: _Connection) -> None:
        async for raw in ws:
            self.handle_raw(raw)

    def _end_session(self) -> None:
        self._channels.clear()
        self._subscribed.clear()
        if self._announced:
            self._announced = False
            self._on_disconnected()

    def handle_raw(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("funding_trades_ws_bad_frame %r", raw[:200])
            return
        if isinstance(frame, dict):
            self._handle_event(frame)
        elif isinstance(frame, list) and len(frame) >= 2:
            self._handle_data(frame)

    def _handle_event(self, frame: dict[str, Any]) -> None:
        event = frame.get("event")
        if event == "subscribed" and frame.get("channel") == "trades":
            chan_id, symbol = frame.get("chanId"), frame.get("symbol")
            if isinstance(chan_id, int) and isinstance(symbol, str):
                self._channels[chan_id] = symbol
                self._subscribed.add(symbol)
                if not self._announced and self._subscribed >= set(self._symbols):
                    self._announced = True
                    self._on_connected()
        elif event == "error":
            log.error("funding_trades_ws_error_frame %s", frame)

    def _handle_data(self, frame: list[Any]) -> None:
        chan_id = frame[0]
        symbol = self._channels.get(chan_id) if isinstance(chan_id, int) else None
        if symbol is None:
            return
        received = self._clock_ms()
        payload = frame[1]
        rows: list[Any] = []
        if payload == "hb":
            pass
        elif payload == "fte" and len(frame) >= 3 and isinstance(frame[2], list):
            rows = [frame[2]]
        elif isinstance(payload, list) and payload and isinstance(payload[0], list):
            rows = payload  # the subscription snapshot
        try:
            parsed = [FundingTrade.from_bitfinex(row) for row in rows]
        except Exception:
            log.warning("funding_trades_ws_bad_row %r", rows[:2])
            return
        if parsed:
            self._on_trades(symbol, parsed)
        # After the trades: a frame proves completeness up to its own receive time.
        self._on_alive(symbol, received)
