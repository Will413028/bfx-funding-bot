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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger(__name__)

BITFINEX_WS_URL = "wss://api-pub.bitfinex.com/ws/2"


def compute_backoff_secs(attempt: int) -> int:
    """1, 2, 4, 8, 16, 32, 60, 60, ... capped at 60."""
    if attempt < 1:
        return 1
    return min(60, int(2 ** (attempt - 1)))


def _bitfinex_ws_period_agg(period_agg: str) -> str:
    """Translate user-facing period_agg → Bitfinex WS subscription key suffix.

    For aggregate candles (e.g. "a30"), Bitfinex requires an explicit period
    range: "a30:p2:p30". Sending bare "a30" subscribes silently but receives
    no data (Phase 4.2.0 e1a1cb5 shadow run discovery: 0 a30 candles over 7hr).

    Single-period values ("p2", "p30") pass through unchanged.

    Mirrors REST helper `_bitfinex_period_agg_path` in rest.py — keep both
    in sync; future refactor should consolidate into one module-level helper.
    """
    if period_agg.startswith("a") and ":" not in period_agg:
        return f"{period_agg}:p2:p30"
    return period_agg


@dataclass(frozen=True)
class ChannelSpec:
    symbol: str         # e.g. "fUSD" / "fUST"
    timeframe: str      # e.g. "1h" / "30m"
    period_agg: str     # e.g. "a30" / "p2"

    @property
    def key(self) -> str:
        return (
            f"trade:{self.timeframe}:{self.symbol}:"
            f"{_bitfinex_ws_period_agg(self.period_agg)}"
        )


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
        on_disconnect: Callable[[str], None] | None = None,
    ) -> None:
        self.url = url
        self.channels = {c.key: _ChannelState(spec=c) for c in channels}
        self.hb_timeout_s = hb_timeout_s
        self._on_disconnect = on_disconnect
        self._ws: ClientConnection | None = None
        # None is the disconnect sentinel: _recv_loop enqueues it when the
        # connection ends so candles() terminates and the daemon's reconnect
        # loop (driven by the iterator returning) can run.
        self._candle_q: asyncio.Queue[CandleMessage | None] = asyncio.Queue()
        self._stop = False
        self.reconnect_attempts = 0
        self._reconnect_history: deque[float] = deque(maxlen=1000)
        self._recv_task: asyncio.Task[None] | None = None
        self._hb_task: asyncio.Task[None] | None = None
        self._connected_at: float | None = None

    async def candles(self) -> AsyncIterator[CandleMessage]:
        await self._ensure_connected()
        while not self._stop:
            msg = await self._candle_q.get()
            if msg is None:
                # Disconnect sentinel: the connection ended (peer close, hb
                # watchdog, or library ping timeout). Terminate the iterator so
                # the daemon's reconnect loop runs instead of blocking forever.
                return
            yield msg

    def reconnect_count_last_hour(self) -> int:
        cutoff = time.monotonic() - 3600
        return sum(1 for t in self._reconnect_history if t >= cutoff)

    def last_msg_age_ms(self) -> int:
        if not self.channels:
            return 0
        newest = max((s.last_msg_ts for s in self.channels.values()), default=time.monotonic())
        return int((time.monotonic() - newest) * 1000)

    def maybe_reset_backoff(self) -> None:
        """Reset reconnect_attempts if connection has been stable for >= 5 minutes.

        Called by daemon (Task 7) after successful reconnect to clear backoff history.
        """
        if self._connected_at is None:
            return
        if time.monotonic() - self._connected_at >= 300:
            self.reconnect_attempts = 0

    def _fire_disconnect(self, reason: str) -> None:
        # NOTE: reconnect_attempts / _reconnect_history are mutated here only.
        # asyncio single-thread ensures no concurrent mutation today; if Task 7 daemon
        # adds its own _fire_disconnect calls, audit for double-increment.
        self._reconnect_history.append(time.monotonic())
        self.reconnect_attempts += 1
        if self._on_disconnect is not None:
            self._on_disconnect(reason)

    async def _hb_watchdog(self) -> None:
        """Poll for heartbeat staleness. On timeout: fires on_disconnect, closes ws, exits.

        Reconnect is the caller's (daemon, Task 7) responsibility after on_disconnect fires.
        """
        try:
            while not self._stop:
                # poll at 2x Nyquist: detect stale within at most one timeout period
                await asyncio.sleep(self.hb_timeout_s / 2)
                stale = any(
                    time.monotonic() - s.last_msg_ts > self.hb_timeout_s
                    for s in self.channels.values()
                    if s.chan_id is not None
                )
                if stale:
                    self._fire_disconnect("hb_timeout")
                    if self._ws is not None:
                        await self._ws.close()
                        self._ws = None  # allow _ensure_connected to reconnect after on_disconnect
                    return
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        self._stop = True
        for task in (self._recv_task, self._hb_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._recv_task = None
        self._hb_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _ensure_connected(self) -> None:
        if self._ws is not None:
            return
        self._ws = await websockets.connect(self.url, max_size=2**20)
        self._connected_at = time.monotonic()
        for spec in [s.spec for s in self.channels.values()]:
            await self._ws.send(json.dumps({
                "event": "subscribe", "channel": "candles", "key": spec.key,
            }))
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._hb_task = asyncio.create_task(self._hb_watchdog())

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                self._handle_raw(raw)
        except asyncio.CancelledError:
            # Shutdown (close() cancels this task) — no reconnect signal; close()
            # sets _stop and the consumer is being torn down.
            raise
        except websockets.ConnectionClosed:
            pass
        # The connection ended (peer close, hb watchdog closing _ws, or the
        # websockets library's ping-timeout). Signal candles() to terminate so
        # the daemon reconnect loop runs. Skipped during shutdown (_stop set).
        if not self._stop:
            self._candle_q.put_nowait(None)

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
