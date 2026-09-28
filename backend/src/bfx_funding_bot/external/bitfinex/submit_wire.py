"""Pure Bitfinex submit payload and response wire helpers."""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from math import isfinite
from typing import Any
from uuid import UUID

_VENUE_ERROR_TEXT_MAX = 200


def venue_error(body: Any) -> tuple[int, str] | None:
    """The venue's own refusal, as it states it: ``["error", CODE, MESSAGE]``.

    Bitfinex delivers business rejections in this shape *with an HTTP 5xx*, so a
    classifier that reads only the status cannot tell "the venue refused, and
    here is why" from "the venue fell over and I have no idea what it did". Both
    become UNKNOWN, which halts the symbol and costs an operator adjudication --
    for an event the venue had already explained.

    Only the parsed, shape-checked pair is ever returned, never response text.
    That is what makes this safe where keeping the body is not: an upstream echo
    of a credential is neither an int in slot 1 nor a bounded message in slot 2,
    so it cannot reach a caller through here.
    """
    if (
        not isinstance(body, list)
        or len(body) < 3
        or body[0] != "error"
        or isinstance(body[1], bool)
        or not isinstance(body[1], int)
        or not isinstance(body[2], str)
    ):
        return None
    return body[1], body[2][:_VENUE_ERROR_TEXT_MAX]


def _canonical_json(value: Any) -> bytes:
    normalized = normalize_submit_payload(value) if isinstance(value, Mapping) else _normalize_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("submit payload cannot contain non-finite floats")
        return format(Decimal(str(value)), "f")
    if isinstance(value, (UUID, date, datetime)):
        return str(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("submit payload mapping keys must be strings")
            normalized[key] = _normalize_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    raise TypeError(f"submit payload value is not JSON-compatible: {type(value).__name__}")


def normalize_submit_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return an immutable-audit-ready JSON-shaped payload.

    Decimal and float values use the same fixed-point representation as the
    venue adapter, while object key order is canonicalized by the fingerprint
    serializer.  No headers, signatures, or credentials belong in this value.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("submit payload must be a mapping")
    normalized = _normalize_value(payload)
    assert isinstance(normalized, dict)
    return normalized


def response_digest(response: Any) -> str:
    """Hash a parsed response without retaining arbitrary response text."""
    try:
        encoded = _canonical_json(response)
    except (TypeError, ValueError):
        encoded = repr(response).encode("utf-8", errors="replace")
    return sha256(encoded).hexdigest()


