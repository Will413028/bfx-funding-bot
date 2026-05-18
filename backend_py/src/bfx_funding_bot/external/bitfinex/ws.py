"""Bitfinex public candles WS client.

Phase 4.1: candles-only subscription, candle-close-driven 策略消費。
Resilience (reconnect + gap-fill) 在 Task 6 / 7 加。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger(__name__)

BITFINEX_WS_URL = "wss://api-pub.bitfinex.com/ws/2"


@dataclass(frozen=True)
class ChannelSpec:
    symbol: str         # e.g. "fUSD" / "fUST"
    timeframe: str      # e.g. "1h" / "30m"
    period_agg: str     # e.g. "a30" / "p2"

    @property
    def key(self) -> str:
        return f"trade:{self.timeframe}:{self.symbol}:{self.period_agg}"


@dataclass(frozen=True)
class CandleMessage:
    symbol: str
    timeframe: str
    period_agg: str
    mts: int
    open: float
    close: float
    high: float
    low: float
    volume: float


@dataclass
class _ChannelState:
    spec: ChannelSpec
    chan_id: int | None = None
    last_msg_ts: float = field(default_factory=time.monotonic)


class BitfinexWSClient:
    """Public funding candles WebSocket client.

    Usage:
        client = BitfinexWSClient(channels=[...])
        async for candle in client.candles():
            ...
    """

    def __init__(
        self,
        channels: list[ChannelSpec],
        *,
        url: str = BITFINEX_WS_URL,
        hb_timeout_s: float = 30.0,
    ) -> None:
        self.url = url
        self.channels = {c.key: _ChannelState(spec=c) for c in channels}
        self.hb_timeout_s = hb_timeout_s
        self._ws: ClientConnection | None = None
        self._candle_q: asyncio.Queue[CandleMessage] = asyncio.Queue()
        self._stop = False
        self.reconnect_attempts = 0
        self._reconnect_history: deque[float] = deque(maxlen=1000)
        self._recv_task: asyncio.Task[None] | None = None

    async def candles(self) -> AsyncIterator[CandleMessage]:
        await self._ensure_connected()
        while not self._stop:
            msg = await self._candle_q.get()
            yield msg

    async def close(self) -> None:
        self._stop = True
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _ensure_connected(self) -> None:
        if self._ws is not None:
            return
        self._ws = await websockets.connect(self.url, max_size=2**20)
        for spec in [s.spec for s in self.channels.values()]:
            await self._ws.send(json.dumps({
                "event": "subscribe", "channel": "candles", "key": spec.key,
            }))
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        with contextlib.suppress(websockets.ConnectionClosed, asyncio.CancelledError):
            async for raw in self._ws:
                self._handle_raw(raw)

    def _handle_raw(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("bitfinex_ws_bad_frame %r", raw[:200])
            return
        if isinstance(msg, dict):
            self._handle_event(msg)
            return
        if isinstance(msg, list) and len(msg) >= 2:
            chan_id = msg[0]
            payload = msg[1]
            state = self._state_for_chan_id(chan_id)
            if state is None:
                return
            state.last_msg_ts = time.monotonic()
            if payload == "hb":
                return
            self._handle_candle_payload(state, payload)

    def _handle_event(self, msg: dict[str, Any]) -> None:
        ev = msg.get("event")
        if ev == "subscribed" and msg.get("channel") == "candles":
            state = self.channels.get(msg["key"])
            if state is not None:
                state.chan_id = msg["chanId"]
        elif ev == "info":
            log.debug("bitfinex_ws_info %s", msg)
        elif ev == "error":
            log.error("bitfinex_ws_error %s", msg)

    def _state_for_chan_id(self, chan_id: int) -> _ChannelState | None:
        for state in self.channels.values():
            if state.chan_id == chan_id:
                return state
        return None

    def _handle_candle_payload(
        self,
        state: _ChannelState,
        payload: Any,
    ) -> None:
        if not payload:
            return
        candles: list[list[float]] = payload if isinstance(payload[0], list) else [payload]
        for c in candles:
            if not c or len(c) < 6:
                continue
            cmsg = CandleMessage(
                symbol=state.spec.symbol,
                timeframe=state.spec.timeframe,
                period_agg=state.spec.period_agg,
                mts=int(c[0]),
                open=float(c[1]),
                close=float(c[2]),
                high=float(c[3]),
                low=float(c[4]),
                volume=float(c[5]),
            )
            self._candle_q.put_nowait(cmsg)
