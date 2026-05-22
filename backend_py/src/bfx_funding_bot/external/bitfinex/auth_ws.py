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

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)


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
    offer_id_meta: int | None  # cross-map to our venue_offer_id (TBV vs real API)
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
    pass


@dataclass(frozen=True, slots=True)
class ChannelInfo(BfxWSEvent):
    version: int | None
    platform_status: int | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Unknown(BfxWSEvent):
    raw: Any


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


def _parse_channel_msg(msg: list[Any]) -> BfxWSEvent | None:
    if len(msg) < 2:
        return None
    payload = msg[1]
    if payload == "hb":
        return Heartbeat()
    if not isinstance(payload, str) or len(msg) < 3:
        return None

    msg_type = payload
    data = msg[2]
    raw_seq = msg[3] if len(msg) > 3 and isinstance(msg[3], int) else None

    if not isinstance(data, list):
        return None

    try:
        if msg_type == "fcn":
            return _parse_fcn(data, raw_seq)
        if msg_type == "fcu":
            return _parse_fcu(data, raw_seq)
        if msg_type == "foc":
            return _parse_foc(data, raw_seq)
    except (IndexError, TypeError, ValueError) as e:
        log.warning("bfx_ws_parse_failed type=%s err=%r", msg_type, e)
        return None

    return Unknown(raw=msg)


def _parse_fcn(d: list[Any], raw_seq: int | None) -> FcnEvent:
    # Bitfinex FCN array layout (0-indexed):
    # 0=id, 1=symbol, 2=side, 3=mts_create, 4=mts_update, 5=amount,
    # 6=flags, 7=status, 8-10=?, 11=rate_type, 12=rate, 13=period,
    # 14=mts_opening, 15=?, 16=notify, 17=hidden, ...
    return FcnEvent(
        credit_id=int(d[0]),
        symbol=str(d[1]),
        side=int(d[2]),
        mts_create=int(d[3]),
        mts_update=int(d[4]),
        amount=Decimal(str(d[5])),
        rate=float(d[12]),
        period_days=int(d[13]),
        offer_id_meta=int(d[14]) if len(d) > 14 and d[14] is not None else None,
        raw_seq=raw_seq,
        raw=d,
    )


def _parse_fcu(d: list[Any], raw_seq: int | None) -> FcuEvent:
    # Same array layout as FCN; rate at index 12
    return FcuEvent(
        credit_id=int(d[0]),
        symbol=str(d[1]),
        mts_update=int(d[4]),
        amount=Decimal(str(d[5])),
        rate=float(d[12]),
        raw_seq=raw_seq,
        raw=d,
    )


def _parse_foc(d: list[Any], raw_seq: int | None) -> FocEvent:
    return FocEvent(
        venue_offer_id=str(d[0]),
        symbol=str(d[2]),
        mts_create=int(d[3]),
        mts_update=int(d[4]),
        amount=Decimal(str(d[5])),
        status=str(d[7]),
        rate=float(d[11]),
        period_days=int(d[12]),
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
