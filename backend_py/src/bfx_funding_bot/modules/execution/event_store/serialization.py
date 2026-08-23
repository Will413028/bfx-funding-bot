from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any
from uuid import UUID

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    DEFAULT_RECONCILE_SYMBOL,
    CreditClosed,
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationReleased,
    _construct_legacy_event,
)

# event_type string <-> domain class. Clean field names (we own this schema).
_TYPE_BY_CLASS: dict[type, str] = {
    ReservationIntent: "RESERVATION_INTENT",
    ReservationClaimed: "RESERVATION_CLAIMED",
    ReservationFailed: "RESERVATION_FAILED",
    OrderFilled: "ORDER_FILL",
    ReservationReleased: "RESERVATION_RELEASED",
    CreditClosed: "CREDIT_CLOSED",
}
_CLASS_BY_TYPE: dict[str, type] = {v: k for k, v in _TYPE_BY_CLASS.items()}

_FIELDS: dict[type, list[str]] = {
    cls: [f.name for f in dataclasses.fields(cls)]
    for cls in _CLASS_BY_TYPE.values()
}
_DECIMAL_FIELDS = {"size_usdt", "amount"}
_UUID_FIELDS = {"signal_correlation_id"}
_REF_FIELD = "reservation_ref"
_CORRELATION_EVENT_TYPES = frozenset({
    "RESERVATION_INTENT",
    "RESERVATION_CLAIMED",
    "RESERVATION_FAILED",
    "ORDER_FILL",
    "RESERVATION_RELEASED",
})


def event_type_of(event: object) -> str:
    try:
        return _TYPE_BY_CLASS[type(event)]
    except KeyError:
        raise ValueError(f"unserializable event type: {type(event).__name__}") from None


def serialize_event(event: object) -> dict[str, Any]:
    """Domain event -> JSON-safe payload dict. Decimal->str, UUID->str."""
    etype = event_type_of(event)
    out: dict[str, Any] = {}
    for field in _FIELDS[type(event)]:
        value = getattr(event, field)
        if isinstance(value, ReservationRef):
            out[field] = {
                "execution_decision_id": value.execution_decision_id,
                "cid": value.cid,
                "signal_correlation_id": str(value.signal_correlation_id),
                "venue_offer_id": value.venue_offer_id,
            }
        elif isinstance(value, (Decimal, UUID)):
            out[field] = str(value)
        else:
            out[field] = value
    out["__event_type__"] = etype
    return out


def deserialize_event(event_type: str, payload: dict[str, Any]) -> object:
    cls = _CLASS_BY_TYPE.get(event_type)
    if cls is None:
        raise ValueError(f"unknown event_type: {event_type}")
    # Upcast: each of the 5 reserve events gained a mandatory `symbol` (the 4
    # position events in Phase 2; Intent/Failed in the fUSD-prereq work) AFTER
    # early event_log rows were written. Inject the historically-correct value
    # for ANY legacy payload missing it (not just Intent/Failed) — otherwise a
    # rebuild_snapshot_from_log over legacy ORDER_FILL/CLAIMED/RELEASED rows hits
    # the offer_claims symbol guard or the position_state tail fold drops them.
    # New rows already carry symbol so this is a no-op. The canary was fUST-only
    # when every symbol-less row existed.
    if payload.get("symbol") is None:
        payload = {**payload, "symbol": DEFAULT_RECONCILE_SYMBOL}
    # Task 4 introduced the immutable reservation reference. Rows written
    # before that schema have neither key; they remain explicitly
    # uncorrelated legacy data rather than receiving an invented decision id.
    is_historical_legacy = event_type in _CORRELATION_EVENT_TYPES and _REF_FIELD not in payload
    if is_historical_legacy:
        payload = {
            **payload,
            "reservation_ref": None,
            "is_legacy_uncorrelated": True,
        }
    if event_type == "RESERVATION_INTENT" and "execution_decision_id" not in payload:
        payload = {**payload, "execution_decision_id": None}
    kwargs: dict[str, Any] = {field: _coerce(field, payload.get(field)) for field in _FIELDS[cls]}
    if is_historical_legacy:
        return _construct_legacy_event(cls, kwargs)
    return cls(**kwargs)


def _coerce(field: str, raw: Any) -> Any:
    if raw is None:
        return None
    if field in _DECIMAL_FIELDS:
        return Decimal(str(raw))
    if field in _UUID_FIELDS:
        return UUID(str(raw))
    if field == _REF_FIELD:
        if not isinstance(raw, dict):
            raise TypeError("reservation_ref must be an object or null")
        return ReservationRef(
            execution_decision_id=str(raw["execution_decision_id"]),
            cid=int(raw["cid"]),
            signal_correlation_id=UUID(str(raw["signal_correlation_id"])),
            venue_offer_id=raw.get("venue_offer_id"),
        )
    return raw
