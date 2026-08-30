"""Bitfinex public funding-book WebSocket protocol adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import zlib
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from bfx_funding_bot.external.bitfinex.ws import BITFINEX_WS_URL

log = logging.getLogger(__name__)

SEQ_ALL = 65_536
OB_CHECKSUM = 131_072
BOOK_CONF_FLAGS = SEQ_ALL | OB_CHECKSUM

BookLevels = Sequence[object]


def funding_book_checksum(*, bids: BookLevels, asks: BookLevels) -> int:
    """Return Bitfinex's signed CRC32 for the top 25 funding-book levels."""

    def value(level: object, index: int, attribute: str) -> object:
        if (
            isinstance(level, Sequence)
            and not isinstance(level, (str, bytes))
            and len(level) > index
        ):
            return level[index]
        return getattr(level, attribute)

    def token(level: object) -> str:
        return ":".join(
            str(value(level, index, attribute))
            for index, attribute in ((0, "rate"), (1, "period"), (3, "amount"))
        )

    ordered_bids = sorted(
        bids, key=lambda level: Decimal(str(value(level, 0, "rate"))), reverse=True
    )[:25]
    ordered_asks = sorted(asks, key=lambda level: Decimal(str(value(level, 0, "rate"))))[:25]
    checksum_values: list[str] = []
    for bid, ask in zip(ordered_bids, ordered_asks, strict=False):
        checksum_values.append(token(bid))
        checksum_values.append(token(ask))
    checksum_values.extend(token(level) for level in ordered_bids[len(ordered_asks) :])
    checksum_values.extend(token(level) for level in ordered_asks[len(ordered_bids) :])
    unsigned = zlib.crc32(":".join(checksum_values).encode())
    return unsigned - 2**32 if unsigned >= 2**31 else unsigned


class FundingBookWSClient:
    """Parse and maintain the I/O shell for Bitfinex funding-book frames."""

    def __init__(
        self,
        *,
        symbols: Sequence[str],
        length: int = 25,
        url: str = BITFINEX_WS_URL,
        on_snapshot: Callable[[str, list[list[object]], int | None], None] | None = None,
        on_update: Callable[[str, list[object], int | None], None] | None = None,
        on_checksum: Callable[[str, int, int | None], None] | None = None,
        on_sequence: Callable[[str, int], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._symbols = tuple(symbols)
        self._length = length
        self._url = url
        self._on_snapshot = on_snapshot
        self._on_update = on_update
        self._on_checksum = on_checksum
        self._on_sequence = on_sequence
        self._on_disconnect = on_disconnect
        self._channels: dict[int, str] = {}
        self._snapshot_channels: set[int] = set()
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None

    def subscription_frames(self) -> tuple[str, ...]:
        conf = json.dumps({"event": "conf", "flags": BOOK_CONF_FLAGS})
        subscriptions = tuple(
            json.dumps(
                {
                    "event": "subscribe",
                    "channel": "book",
                    "symbol": symbol,
                    "prec": "P0",
                    "freq": "F0",
                    "len": self._length,
                }
            )
            for symbol in self._symbols
        )
        return (conf, *subscriptions)

    def set_handlers(
        self,
        *,
        on_snapshot: Callable[[str, list[list[object]], int | None], None],
        on_update: Callable[[str, list[object], int | None], None],
        on_checksum: Callable[[str, int, int | None], None],
        on_sequence: Callable[[str, int], None],
        on_disconnect: Callable[[], None],
    ) -> None:
        self._on_snapshot = on_snapshot
        self._on_update = on_update
        self._on_checksum = on_checksum
        self._on_sequence = on_sequence
        self._on_disconnect = on_disconnect

    def checksum(self, *, bids: BookLevels, asks: BookLevels) -> int:
        return funding_book_checksum(bids=bids, asks=asks)

    async def start(self) -> None:
        if self._ws is not None:
            return
        self._ws = await websockets.connect(self._url, max_size=2**20)
        for frame in self.subscription_frames():
            await self._ws.send(frame)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def stop(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
            self._recv_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                self.handle_raw(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._ws = None
            self.mark_disconnected()

    def mark_disconnected(self) -> None:
        self._channels.clear()
        self._snapshot_channels.clear()
        if self._on_disconnect is not None:
            self._on_disconnect()

    def handle_raw(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("bitfinex_funding_book_bad_frame %r", raw[:200])
            return
        if isinstance(frame, dict):
            self._handle_event(frame)
        elif isinstance(frame, list) and len(frame) >= 2:
            self._handle_data(frame)

    def _handle_event(self, frame: dict[str, Any]) -> None:
        event = frame.get("event")
        if event == "subscribed" and frame.get("channel") == "book":
            chan_id = frame.get("chanId")
            symbol = frame.get("symbol")
            if isinstance(chan_id, int) and isinstance(symbol, str):
                self._channels[chan_id] = symbol
                self._snapshot_channels.discard(chan_id)
        elif event == "error":
            log.error("bitfinex_funding_book_error %s", frame)
            self.mark_disconnected()

    def _handle_data(self, frame: list[object]) -> None:
        chan_id = frame[0]
        if not isinstance(chan_id, int):
            return
        symbol = self._channels.get(chan_id)
        if symbol is None:
            return
        payload = frame[1]
        sequence = frame[2] if len(frame) >= 3 and isinstance(frame[2], int) else None
        if payload == "hb":
            if sequence is not None and self._on_sequence is not None:
                self._on_sequence(symbol, sequence)
            return
        if payload == "cs":
            checksum = frame[2] if len(frame) >= 3 and isinstance(frame[2], int) else None
            checksum_sequence = frame[3] if len(frame) >= 4 and isinstance(frame[3], int) else None
            if checksum is not None and self._on_checksum is not None:
                self._on_checksum(symbol, checksum, checksum_sequence)
            return
        if isinstance(payload, list) and (
            chan_id not in self._snapshot_channels or (payload and isinstance(payload[0], list))
        ):
            levels = [list(level) for level in payload if isinstance(level, list)]
            self._snapshot_channels.add(chan_id)
            if self._on_snapshot is not None:
                self._on_snapshot(symbol, levels, sequence)
        elif isinstance(payload, list) and self._on_update is not None:
            self._on_update(symbol, list(payload), sequence)
