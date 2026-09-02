"""Closed submit-result vocabulary and deterministic audit helpers.

The venue submit boundary is intentionally asymmetric: an explicit venue
acknowledgement or structured rejection is authoritative, while every other
post-transport result remains UNKNOWN.  Keeping this policy in a small pure
module makes it reusable by live adapters, command gates, and deterministic
tests without coupling it to HTTP or persistence.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from typing import Any
from uuid import UUID

import httpx

__all__ = [
    "SubmitAcknowledged",
    "SubmitNotSent",
    "SubmitOutcome",
    "SubmitOutcomeKind",
    "SubmitOutcomeUnknown",
    "SubmitRejected",
    "classify_submit_response",
    "fingerprint_submit_payload",
    "normalize_submit_payload",
    "payload_sha256",
    "response_digest",
]


class SubmitOutcomeKind(StrEnum):
    """Stable wire/audit names for the four legal submit outcomes."""

    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    NOT_SENT = "not_sent"


def _require_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("submit outcome reason must be non-empty")
    return reason.strip()


def _require_venue_offer_id(venue_offer_id: str) -> str:
    if not isinstance(venue_offer_id, str) or not venue_offer_id.strip():
        raise ValueError("acknowledged submit requires a venue offer id")
    return venue_offer_id.strip()


@dataclass(frozen=True, slots=True)
class SubmitAcknowledged:
    """Bitfinex accepted the request and returned a venue offer identity."""

    venue_offer_id: str
    raw_response: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue_offer_id", _require_venue_offer_id(self.venue_offer_id))

    @property
    def kind(self) -> SubmitOutcomeKind:
        return SubmitOutcomeKind.ACKNOWLEDGED

    @property
    def outcome_kind(self) -> SubmitOutcomeKind:
        return self.kind


@dataclass(frozen=True, slots=True)
class SubmitRejected:
    """The venue gave explicit, structurally parseable rejection evidence."""

    reason: str
    raw_response: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _require_reason(self.reason))

    @property
    def kind(self) -> SubmitOutcomeKind:
        return SubmitOutcomeKind.REJECTED

    @property
    def outcome_kind(self) -> SubmitOutcomeKind:
        return self.kind


@dataclass(frozen=True, slots=True)
class SubmitOutcomeUnknown:
    """The request may have reached the venue, but no authoritative outcome exists."""

    reason: str
    transport_started: bool
    raw_response_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _require_reason(self.reason))
        if self.raw_response_digest is not None:
            digest = self.raw_response_digest.strip().lower()
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise ValueError("raw_response_digest must be a SHA-256 hex digest")
            object.__setattr__(self, "raw_response_digest", digest)

    @property
    def kind(self) -> SubmitOutcomeKind:
        return SubmitOutcomeKind.UNKNOWN

    @property
    def outcome_kind(self) -> SubmitOutcomeKind:
        return self.kind


@dataclass(frozen=True, slots=True)
class SubmitNotSent:
    """Local validation failed before any venue transport was started."""

    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _require_reason(self.reason))

    @property
    def kind(self) -> SubmitOutcomeKind:
        return SubmitOutcomeKind.NOT_SENT

    @property
    def outcome_kind(self) -> SubmitOutcomeKind:
        return self.kind


type SubmitOutcome = (
    SubmitAcknowledged | SubmitRejected | SubmitOutcomeUnknown | SubmitNotSent
)


# A 4xx is only rejection evidence when it belongs to a status family whose
# response semantics are deterministic for this write.  408/429 are excluded:
# the server may have started processing while asking the client to retry later.
ALLOWLISTED_4XX_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 409, 422})


def _bounded_text(value: object, *, limit: int = 256) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _structured_rejection_reason(body: Any) -> str | None:
    """Extract a bounded reason only from known Bitfinex error shapes."""
    if isinstance(body, list):
        # Funding REST responses use [mts, type, ..., status, ..., text].
        if len(body) < 7 or body[6] not in {"ERROR", "FAILURE"}:
            return None
        code = _bounded_text(body[1] if len(body) > 1 else None)
        text = _bounded_text(body[8] if len(body) > 8 else None)
    elif isinstance(body, Mapping):
        status = body.get("status", body.get("STATUS", body.get("result")))
        if str(status).upper() not in {"ERROR", "FAILURE", "REJECTED"}:
            return None
        code = _bounded_text(body.get("code", body.get("CODE")))
        text = _bounded_text(
            body.get("text", body.get("message", body.get("error")))
        )
    else:
        return None
    if code and text:
        return f"venue_rejected:{code}:{text}"
    if text:
        return f"venue_rejected:{text}"
    if code:
        return f"venue_rejected:{code}"
    return "venue_rejected"


def _venue_offer_id(body: Any) -> str | None:
    if not isinstance(body, list) or len(body) < 7 or body[6] != "SUCCESS":
        return None
    offer = body[4] if len(body) > 4 else None
    if not isinstance(offer, list) or not offer:
        return None
    value = offer[0]
    if isinstance(value, bool) or value is None:
        return None
    return str(value).strip() or None


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


def fingerprint_submit_payload(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 fingerprint of a normalized submit payload."""
    return sha256(_canonical_json(normalize_submit_payload(payload))).hexdigest()


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """Explicitly named alias used by the submission-attempt projection."""
    return fingerprint_submit_payload(payload)


def response_digest(response: Any) -> str:
    """Hash a parsed response without retaining arbitrary response text."""
    try:
        encoded = _canonical_json(response)
    except (TypeError, ValueError):
        encoded = repr(response).encode("utf-8", errors="replace")
    return sha256(encoded).hexdigest()


def _unknown(
    reason: str,
    *,
    transport_started: bool,
    parsed_body: Any = None,
) -> SubmitOutcomeUnknown:
    return SubmitOutcomeUnknown(
        reason=reason,
        transport_started=transport_started,
        raw_response_digest=(response_digest(parsed_body) if parsed_body is not None else None),
    )


def classify_submit_response(
    http_status: int | None = None,
    parsed_body: Any = None,
    transport_started: bool = False,
    exception: BaseException | None = None,
) -> SubmitOutcome:
    """Classify one submit attempt using conservative evidence rules.

    ``transport_started`` is supplied by the caller immediately before the
    network await.  Once it is true, local-looking exceptions are still
    UNKNOWN: the request may have reached Bitfinex and must never be retried.
    """
    if exception is not None:
        if not transport_started:
            if isinstance(exception, asyncio.CancelledError):
                return SubmitNotSent("cancelled_before_start")
            return SubmitNotSent("local_validation_failed")
        if isinstance(exception, asyncio.CancelledError):
            return _unknown("cancelled_after_start", transport_started=True)
        if isinstance(exception, httpx.TimeoutException):
            return _unknown("timeout", transport_started=True)
        if isinstance(exception, httpx.NetworkError):
            return _unknown("connection_error", transport_started=True)
        return _unknown("submit_exception", transport_started=True)

    if not transport_started:
        return SubmitNotSent("not_sent")
    if http_status is None:
        return _unknown("response_missing", transport_started=True, parsed_body=parsed_body)

    if http_status >= 500:
        return _unknown("http_5xx", transport_started=True, parsed_body=parsed_body)

    rejection_reason = _structured_rejection_reason(parsed_body)
    if 200 <= http_status < 300:
        venue_offer_id = _venue_offer_id(parsed_body)
        if venue_offer_id is not None:
            return SubmitAcknowledged(venue_offer_id=venue_offer_id, raw_response=parsed_body)
        if rejection_reason is not None:
            return SubmitRejected(reason=rejection_reason, raw_response=parsed_body)
        reason = "malformed_response" if isinstance(parsed_body, list) else "unrecognized_response"
        return _unknown(reason, transport_started=True, parsed_body=parsed_body)

    if (
        400 <= http_status < 500
        and http_status in ALLOWLISTED_4XX_REJECTION_STATUSES
        and rejection_reason is not None
    ):
        return SubmitRejected(reason=rejection_reason, raw_response=parsed_body)

    return _unknown("unrecognized_response", transport_started=True, parsed_body=parsed_body)
