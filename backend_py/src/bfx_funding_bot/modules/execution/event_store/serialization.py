from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any
from uuid import UUID

from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)

# event_type string <-> domain class. Clean field names (we own this schema).
_TYPE_BY_CLASS: dict[type, str] = {
    ReservationClaimed: "RESERVATION_CLAIMED",
    OrderFilled: "ORDER_FILL",
    ReservationReleased: "RESERVATION_RELEASED",
}
_CLASS_BY_TYPE: dict[str, type] = {v: k for k, v in _TYPE_BY_CLASS.items()}

_FIELDS: dict[type, list[str]] = {
    cls: [f.name for f in dataclasses.fields(cls)]
    for cls in _CLASS_BY_TYPE.values()
}
_DECIMAL_FIELDS = {"size_usdt"}
_UUID_FIELDS = {"signal_correlation_id"}


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
        if isinstance(value, (Decimal, UUID)):
            out[field] = str(value)
        else:
            out[field] = value
    out["__event_type__"] = etype
    return out


def deserialize_event(event_type: str, payload: dict[str, Any]) -> object:
    cls = _CLASS_BY_TYPE.get(event_type)
    if cls is None:
        raise ValueError(f"unknown event_type: {event_type}")
    kwargs: dict[str, Any] = {field: _coerce(field, payload.get(field)) for field in _FIELDS[cls]}
    return cls(**kwargs)


def _coerce(field: str, raw: Any) -> Any:
    if raw is None:
        return None
    if field in _DECIMAL_FIELDS:
        return Decimal(str(raw))
    if field in _UUID_FIELDS:
        return UUID(str(raw))
    return raw
