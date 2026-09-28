"""Bitfinex authenticated WebSocket client (Phase 4.4a).

Pure types + parse_frame function (this file — Task 10).
Signer (Task 11) + I/O shell (Task 12) follow.

WS user channel reference: https://docs.bitfinex.com/reference/ws-auth-input
Event taxonomy:
  fon/fou/foc — funding offer new/update/cancel
  fcn/fcu/fcc — funding credit new/update/close
  ws/wu — wallet snapshot / update
  os/on/ou/oc — order snapshot/new/update/cancel (margin, not used in 4.4a)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import websockets
from websockets.asyncio.client import ClientConnection

from bfx_funding_bot.external.bitfinex.credentials import Credentials
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate

log = logging.getLogger(__name__)

SEQ_ALL_FLAG = 65536  # Bitfinex `conf` flag: append a sequence number to every packet


class SequenceTracker:
    """Detects dropped packets via Bitfinex's monotonic public sequence number.

    Pure logic — no socket, no domain types. `reset()` at each (re)connect, since
    the sequence restarts per connection. A forward jump means packets were lost.
    A `None` seq carries no information (flag not applied / non-seq frame).
    """

    def __init__(self) -> None:
        self._expected: int | None = None

    def reset(self) -> None:
        self._expected = None

    def observe(self, seq: int | None) -> Literal["ok", "gap"]:
        if seq is None:
            return "ok"
        if self._expected is not None and seq > self._expected:
            self._expected = seq + 1
            return "gap"
        if self._expected is None or seq + 1 > self._expected:
            self._expected = seq + 1
        return "ok"


@dataclass(frozen=True, slots=True)
class BfxWSEvent:
    """Base sentinel for type union — never instantiated directly."""


@dataclass(frozen=True, slots=True)
class FcnEvent(BfxWSEvent):
    """FUNDING CREDIT NEW — credit was created (an offer was matched)."""
    credit_id: int
    symbol: str
    side: int
    mts_create: int          # bitemporal occurred_at
    mts_update: int
    amount: Decimal
    rate: float
    period_days: int
    raw_seq: int | None        # WS SEQ if present
    raw: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FcuEvent(BfxWSEvent):
    """FUNDING CREDIT UPDATE — credit was modified."""
    credit_id: int
    symbol: str
    mts_update: int
    amount: Decimal
    rate: float
    raw_seq: int | None
    raw: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FccEvent(BfxWSEvent):
    """FUNDING CREDIT CLOSE — credit ended (matured or borrower returned early).

    The close time is mts_last_payout: the venue pays out at close, while
    mts_update can still equal mts_create on the close frame (a credit repaid
    after 14 minutes, 2026-09-25, carried mts_update == mts_create). The venue
    does NOT carry the originating offer id, so downstream attribution joins on
    (symbol, amount, mts_create).
    """
    credit_id: int
    symbol: str
    mts_create: int
    mts_update: int
    amount: Decimal
    status: str
    rate: float
    period_days: int
    raw_seq: int | None
    mts_opening: int | None = None
    mts_last_payout: int | None = None  # close time
    raw: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FocEvent(BfxWSEvent):
    """FUNDING OFFER CLOSE — offer was cancelled or executed."""
    venue_offer_id: str
    symbol: str
    mts_create: int
    mts_update: int
    amount: Decimal
    status: str
    rate: float
    period_days: int
    raw_seq: int | None
    raw: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AuthAck(BfxWSEvent):
    status: str
    user_id: int | None
    chan_id: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Heartbeat(BfxWSEvent):
    raw_seq: int | None = None


@dataclass(frozen=True, slots=True)
class ChannelInfo(BfxWSEvent):
    version: int | None
    platform_status: int | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Unknown(BfxWSEvent):
    raw: Any
    raw_seq: int | None = None


def parse_frame(raw: str | bytes) -> BfxWSEvent | None:
    """Parse a Bitfinex WS frame. Returns None for un-parseable / empty / non-event.

    Never raises (G: safe on hypothesis-fuzzed input).
    """
    if not raw:
        return None
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None

    if isinstance(msg, dict):
        return _parse_event_dict(msg)
    if isinstance(msg, list):
        return _parse_channel_msg(msg)
    return None


def _parse_event_dict(msg: dict[str, Any]) -> BfxWSEvent | None:
    ev = msg.get("event")
    if ev == "auth":
        return AuthAck(
            status=msg.get("status", ""),
            user_id=msg.get("userId"),
            chan_id=msg.get("chanId", 0),
            raw=msg,
        )
    if ev == "info":
        return ChannelInfo(
            version=msg.get("version"),
            platform_status=(msg.get("platform", {}) or {}).get("status"),
            raw=msg,
        )
    return None


def _public_seq_of(event: BfxWSEvent) -> int | None:
    """Extract raw_seq from a parsed event (None for events that don't carry one)."""
    return getattr(event, "raw_seq", None)


def _public_seq(msg: list[Any]) -> int | None:
    """Public sequence number from a SEQ_ALL channel frame.

    Channel-0 data frames end with [..., MSG_SEQ, AUTH_SEQ] -> public = 2nd-to-last;
    heartbeats / frames without an auth seq end with [..., MSG_SEQ] -> public = last.
    Returns None when no trailing int seq is present (flag not applied).
    """
    if len(msg) >= 2 and isinstance(msg[-1], int) and isinstance(msg[-2], int):
        return msg[-2]
    if msg and isinstance(msg[-1], int):
        return msg[-1]
    return None


def _parse_channel_msg(msg: list[Any]) -> BfxWSEvent | None:
    if len(msg) < 2:
        return None
    raw_seq = _public_seq(msg)
    payload = msg[1]
    if payload == "hb":
        return Heartbeat(raw_seq=raw_seq)
    if not isinstance(payload, str) or len(msg) < 3:
        return None

    msg_type = payload
    data = msg[2]

    if not isinstance(data, list):
        return None

    try:
        if msg_type == "fcn":
            return _parse_fcn(data, raw_seq)
        if msg_type == "fcu":
            return _parse_fcu(data, raw_seq)
        if msg_type == "fcc":
            return _parse_fcc(data, raw_seq)
        if msg_type == "foc":
            return _parse_foc(data, raw_seq)
    except (IndexError, TypeError, ValueError, BitfinexShapeError) as e:
        log.warning("bfx_ws_parse_failed type=%s err=%r", msg_type, e)
        return None

    return Unknown(raw=msg, raw_seq=raw_seq)


def _optional_mts(value: Any) -> int | None:
    # 0 is the venue's "not set", never a real time.
    return int(value) if value else None


def _parse_fcn(d: list[Any], raw_seq: int | None) -> FcnEvent:
    # Bitfinex funding credit array (0-indexed), same for fcn/fcu/fcc and the
    # REST credits endpoints (auth_rest.parse_active_funding_credits):
    # 0=id, 1=symbol, 2=side, 3=mts_create, 4=mts_update, 5=amount,
    # 6=flags, 7=status, 8=rate_type, 9-10=_, 11=rate, 12=period,
    # 13=mts_opening, 14=mts_last_payout, 15=notify, 16=hidden, ...
    # Rate and period were once read one slot late ([12], [13]): every
    # CREDIT_CLOSED before 2026-09-27 stored the period as its rate and
    # mts_opening as its period.
    return FcnEvent(
        credit_id=int(d[0]),
        symbol=str(d[1]),
        side=int(d[2]),
        mts_create=int(d[3]),
        mts_update=int(d[4]),
        amount=Decimal(str(d[5])),
        rate=float(d[11]),
        period_days=int(d[12]),
        raw_seq=raw_seq,
        raw=d,
    )


def _parse_fcc(d: list[Any], raw_seq: int | None) -> FccEvent:
    # Same array layout as FCN; status at index 7, mts_last_payout (index 14) = close time
    return FccEvent(
        credit_id=int(d[0]),
        symbol=str(d[1]),
        mts_create=int(d[3]),
        mts_update=int(d[4]),
        amount=Decimal(str(d[5])),
        status=str(d[7]),
        rate=float(d[11]),
        period_days=int(d[12]),
        raw_seq=raw_seq,
        raw=d,
        mts_opening=_optional_mts(d[13]),
        mts_last_payout=_optional_mts(d[14]),
    )


def _parse_fcu(d: list[Any], raw_seq: int | None) -> FcuEvent:
    # Same array layout as FCN; rate at index 11
    return FcuEvent(
        credit_id=int(d[0]),
        symbol=str(d[1]),
        mts_update=int(d[4]),
        amount=Decimal(str(d[5])),
        rate=float(d[11]),
        raw_seq=raw_seq,
        raw=d,
    )


def _parse_foc(d: list[Any], raw_seq: int | None) -> FocEvent:
    # foc is a funding-offer array — layout owned by parse_funding_offer_row,
    # shared with the REST parser so the two can never drift.
    row = parse_funding_offer_row(d)
    return FocEvent(
        venue_offer_id=row.venue_offer_id,
        symbol=row.symbol,
        mts_create=row.mts_create,
        mts_update=row.mts_update,
        amount=row.amount,
        status=row.status,
        rate=row.rate if row.rate is not None else 0.0,
        period_days=row.period_days if row.period_days is not None else 0,
        raw_seq=raw_seq,
        raw=d,
    )


def build_auth_payload(*, api_key: str, api_secret: str, nonce_ms: int) -> dict[str, Any]:
    """Build Bitfinex WS auth subscribe payload.

    Per https://docs.bitfinex.com/reference/ws-auth — HMAC-SHA384 signature
    over "AUTH" + nonce string, using api_secret as key.
    """
    nonce_str = str(nonce_ms)
    auth_payload = f"AUTH{nonce_str}"
    sig = hmac.new(
        api_secret.encode("utf-8"),
        auth_payload.encode("utf-8"),
        hashlib.sha384,
    ).hexdigest()
    return {
        "event": "auth",
        "apiKey": api_key,
        "authSig": sig,
        "authNonce": nonce_ms,
        "authPayload": auth_payload,
    }


def sign_request(*, body: bytes, nonce: int, api_secret: str, path: str) -> dict[str, str]:
    """Build Bitfinex REST auth headers for a private endpoint.

    Per https://docs.bitfinex.com/docs/rest-auth — HMAC-SHA384 over
    "/api/" + path + nonce + body, using api_secret as key.
    """
    payload = f"/api/{path}{nonce}".encode() + body
    sig = hmac.new(
        api_secret.encode("utf-8"),
        payload,
        hashlib.sha384,
    ).hexdigest()
    return {
        "bfx-nonce": str(nonce),
        "bfx-signature": sig,
    }


BITFINEX_AUTH_WS_URL = "wss://api.bitfinex.com/ws/2"


class BitfinexAuthWSClient:
    """Authenticated Bitfinex WS user channel client.

    Phase 4.4a: pure I/O. Subscribes user stream → emits typed BfxWSEvent.
    Does NOT know about OfferRegistry, DomainEventBus, or domain semantics.

    Reconnect: exponential backoff 1, 2, 4, ..., 60s cap.
    Heartbeat watchdog: hb_timeout_s default 30s (advisory; reconnect on disconnect).
    """

    def __init__(
        self,
        *,
        creds: Credentials,
        url: str = BITFINEX_AUTH_WS_URL,
        hb_timeout_s: float = 30.0,
        auth_gate: AuthRequestGate | None = None,
        on_disconnect: Callable[[str], None] | None = None,
        on_resync_needed: Callable[[str], None] | None = None,
    ) -> None:
        self._creds = creds
        self._url = url
        self._hb_timeout_s = hb_timeout_s
        self._auth_gate = auth_gate or AuthRequestGate(lambda: int(time.time() * 1000))
        self._on_disconnect = on_disconnect
        self._on_resync_needed = on_resync_needed
        self._ws: ClientConnection | None = None
        self._stop = False
        self.reconnect_attempts = 0
        self.auth_ok_count = 0  # successful AuthAck OK count (health: 0 = never authed)
        self._reconnect_history: deque[float] = deque(maxlen=1000)
        self._last_msg_ts: float = time.monotonic()
        self._seq = SequenceTracker()
        self._connection_count = 0

    @property
    def connection_count(self) -> int:
        """WS connections opened so far. `connection_count>0 and auth_ok_count==0`
        = the 'connected but never authenticates' failure (health-poll reads it)."""
        return self._connection_count

    def reconnect_count_last_hour(self) -> int:
        cutoff = time.monotonic() - 3600
        return sum(1 for t in self._reconnect_history if t >= cutoff)

    def last_msg_age_ms(self) -> int:
        return int((time.monotonic() - self._last_msg_ts) * 1000)

    async def events(self) -> AsyncIterator[BfxWSEvent]:
        """Connect + auth + yield typed events. Yields forever until close()."""
        while not self._stop:
            clean_close = False
            try:
                async for ev in self._connect_and_stream():
                    self._last_msg_ts = time.monotonic()
                    yield ev
                # Stream ended without exception — server closed the connection cleanly
                clean_close = True
            except (websockets.ConnectionClosed, OSError, TimeoutError) as e:
                if self._stop:
                    return
                self._reconnect_history.append(time.monotonic())
                self.reconnect_attempts += 1
                if self._on_disconnect is not None:
                    with contextlib.suppress(Exception):
                        self._on_disconnect(str(type(e).__name__))
                backoff = min(60, 2 ** max(0, self.reconnect_attempts - 1))
                log.warning(
                    "bfx_auth_ws_reconnect attempt=%d backoff_s=%d err=%r",
                    self.reconnect_attempts, backoff, e,
                )
                await asyncio.sleep(backoff)
            else:
                if clean_close and not self._stop:
                    # Server initiated close: count as disconnect, apply backoff
                    self._reconnect_history.append(time.monotonic())
                    self.reconnect_attempts += 1
                    backoff = min(60, 2 ** max(0, self.reconnect_attempts - 1))
                    log.warning(
                        "bfx_auth_ws_reconnect attempt=%d backoff_s=%d err=ConnectionClosed(clean)",
                        self.reconnect_attempts, backoff,
                    )
                    await asyncio.sleep(backoff)

    def _fire_resync(self, reason: str) -> None:
        if self._on_resync_needed is not None:
            with contextlib.suppress(Exception):
                self._on_resync_needed(reason)

    async def _connect_and_stream(self) -> AsyncIterator[BfxWSEvent]:
        async with websockets.connect(self._url, max_size=2**20) as ws:
            self._ws = ws
            self._seq.reset()
            self._connection_count += 1
            if self._connection_count > 1:
                # A reconnect: events during the gap were lost → resync the ledger.
                # (First connection is covered by boot reconcile, so it fires nothing.)
                self._fire_resync("reconnect")
            # The handshake spends a nonce on the same key as the REST clients.
            # Holding the shared gate while taking it and writing the frame
            # means no signed REST call with a smaller nonce is still in flight,
            # and every later one is signed with a larger nonce after this frame
            # is on the wire -- so a REST call can never lose to the handshake.
            # Not held until the AuthAck: the stream below yields to consumers
            # that may themselves need the gate. The residual race (a later
            # REST call overtaking this frame) can only fail the handshake,
            # which is logged and retried with backoff.
            async with self._auth_gate.nonce("read", label="ws_auth") as nonce:
                auth = build_auth_payload(
                    api_key=self._creds.api_key,
                    api_secret=self._creds.api_secret,
                    nonce_ms=nonce,
                )
                await ws.send(json.dumps(auth))
            await ws.send(json.dumps({"event": "conf", "flags": SEQ_ALL_FLAG}))

            async for raw in ws:
                event = parse_frame(raw)
                if event is not None:
                    if isinstance(event, AuthAck):
                        if event.status.upper() == "OK":
                            # Genuine auth success → clear backoff so
                            # reconnect_attempts means "consecutive failures",
                            # not cumulative-since-boot (the public ws.py resets
                            # on stability; this client had no reset at all).
                            self.reconnect_attempts = 0
                            self.auth_ok_count += 1
                            log.info(
                                "bfx_auth_ws_authed user_id=%s chan=%d",
                                event.user_id, event.chan_id,
                            )
                        else:
                            # Auth rejected (e.g. "nonce: small") → the socket is
                            # useless; log LOUD and drop into backoff instead of
                            # silently reconnecting forever.
                            log.error(
                                "bfx_auth_ws_auth_FAILED status=%s raw=%r",
                                event.status, event.raw,
                            )
                            return
                    if isinstance(event, ChannelInfo) and event.raw.get("code") in (
                        20051, 20061,
                    ):
                        # Server restart / maintenance over: Bitfinex asks for a
                        # fresh connection. Returning closes this one; events()
                        # reconnects and the reconnect fires a ledger resync.
                        log.warning("bfx_auth_ws_venue_reconnect_requested raw=%r", event.raw)
                        return
                    if self._seq.observe(_public_seq_of(event)) == "gap":
                        self._fire_resync("seq_gap")
                    yield event
                if self._stop:
                    return

    async def close(self) -> None:
        self._stop = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
