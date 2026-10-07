"""Closed submit-result vocabulary and deterministic audit helpers.

The venue submit boundary is intentionally asymmetric: an explicit venue
acknowledgement or structured rejection is authoritative, while every other
post-transport result remains UNKNOWN.  Keeping this policy in a small pure
module makes it reusable by live adapters, command gates, and deterministic
tests without coupling it to HTTP or persistence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid5

import httpx

from bfx_funding_bot.external.bitfinex.submit_wire import (
    _VENUE_ERROR_TEXT_MAX,
    _canonical_json,
    normalize_submit_payload,
    response_digest,
)
from bfx_funding_bot.external.bitfinex.submit_wire import (
    _normalize_value as _normalize_value,
)
from bfx_funding_bot.external.bitfinex.submit_wire import venue_error as venue_error

__all__ = [
    "SubmissionAttemptPayload",
    "SubmitAcknowledged",
    "SubmitCancelledNotSent",
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

_ATTEMPT_ID_NAMESPACE = UUID("a49b16e1-e2e8-5d7e-bc91-a8cdf80a5783")


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
    # What the venue said, when it said anything structured. Absent for a
    # timeout or a reset, which is exactly the distinction that matters.
    venue_error_code: int | None = None
    venue_error_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _require_reason(self.reason))
        if self.venue_error_code is not None and (
            isinstance(self.venue_error_code, bool)
            or not isinstance(self.venue_error_code, int)
        ):
            raise TypeError("venue_error_code must be an int")
        if self.venue_error_message is not None:
            if not isinstance(self.venue_error_message, str):
                raise TypeError("venue_error_message must be a str")
            object.__setattr__(
                self, "venue_error_message", self.venue_error_message[:_VENUE_ERROR_TEXT_MAX]
            )
        if not isinstance(self.transport_started, bool):
            raise TypeError("transport_started must be a bool")
        if not self.transport_started:
            # A pre-transport failure is authoritative NOT_SENT.  Allowing it
            # to masquerade as UNKNOWN would open an uncertainty record without
            # any possibility that the venue received the request.
            raise ValueError("UNKNOWN outcome requires transport_started=True")
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


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: Any) -> Any:
    """Convert the immutable audit representation back to JSON primitives."""
    if isinstance(value, Mapping):
        return {str(key): _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SubmissionAttemptPayload:
    """Immutable audit identity for one and only one venue submit attempt."""

    execution_decision_id: str
    account_id: UUID | str
    environment: str
    symbol: str
    normalized_payload: Mapping[str, Any]
    attempt_id: UUID | str | None = None
    payload_sha256: str | None = None
    started_at_ms: int = 0
    completed_at_ms: int | None = None
    outcome_kind: SubmitOutcomeKind | str | None = None
    outcome_reason: str | None = None
    venue_offer_id: str | None = None
    last_event_seq: int | None = None

    def __post_init__(self) -> None:
        if not self.execution_decision_id.strip():
            raise ValueError("execution_decision_id must be non-empty")
        try:
            canonical_account_id = (
                self.account_id
                if isinstance(self.account_id, UUID)
                else UUID(str(self.account_id))
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("account_id must be a canonical UUID") from exc
        object.__setattr__(self, "account_id", canonical_account_id)
        if not self.environment.strip():
            raise ValueError("environment must be non-empty")
        if not self.symbol.strip():
            raise ValueError("symbol must be non-empty")
        if self.started_at_ms < 0:
            raise ValueError("started_at_ms must be non-negative")
        if self.completed_at_ms is not None and self.completed_at_ms < self.started_at_ms:
            raise ValueError("completed_at_ms cannot precede started_at_ms")
        if self.last_event_seq is not None and self.last_event_seq < 0:
            raise ValueError("last_event_seq must be non-negative")

        normalized = normalize_submit_payload(self.normalized_payload)
        expected_digest = fingerprint_submit_payload(normalized)
        if self.payload_sha256 is None:
            object.__setattr__(self, "payload_sha256", expected_digest)
        else:
            supplied_digest = self.payload_sha256.strip().lower()
            if supplied_digest != expected_digest:
                raise ValueError("payload_sha256 does not match normalized_payload")
            object.__setattr__(self, "payload_sha256", supplied_digest)
        object.__setattr__(self, "normalized_payload", _freeze_value(normalized))
        if self.attempt_id is None:
            identity = "\x1f".join(
                (
                    self.execution_decision_id,
                    str(canonical_account_id),
                    self.environment,
                    self.symbol,
                    expected_digest,
                )
            )
            object.__setattr__(
                self,
                "attempt_id",
                uuid5(_ATTEMPT_ID_NAMESPACE, identity),
            )
        else:
            try:
                canonical_attempt_id = (
                    self.attempt_id
                    if isinstance(self.attempt_id, UUID)
                    else UUID(str(self.attempt_id))
                )
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("attempt_id must be a canonical UUID") from exc
            object.__setattr__(self, "attempt_id", canonical_attempt_id)

        if self.outcome_kind is not None:
            try:
                normalized_kind = SubmitOutcomeKind(self.outcome_kind)
            except ValueError as exc:
                raise ValueError(f"unsupported submit outcome kind: {self.outcome_kind!r}") from exc
            object.__setattr__(self, "outcome_kind", normalized_kind)
            if normalized_kind is SubmitOutcomeKind.ACKNOWLEDGED and self.venue_offer_id is None:
                raise ValueError("acknowledged outcome requires venue_offer_id")
            if normalized_kind is not SubmitOutcomeKind.ACKNOWLEDGED and self.venue_offer_id is not None:
                raise ValueError("non-acknowledged outcome must not carry venue_offer_id")
            if normalized_kind is not SubmitOutcomeKind.ACKNOWLEDGED and not self.outcome_reason:
                raise ValueError(f"{normalized_kind.value} outcome requires outcome_reason")
        if self.outcome_reason is not None:
            object.__setattr__(self, "outcome_reason", _require_reason(self.outcome_reason))
        if self.venue_offer_id is not None:
            object.__setattr__(self, "venue_offer_id", _require_venue_offer_id(self.venue_offer_id))

    @property
    def payload_fingerprint(self) -> str:
        assert self.payload_sha256 is not None
        return self.payload_sha256

    def as_storage_dict(self) -> dict[str, Any]:
        """Return a JSON/JSONB-safe copy for the audit writer.

        The in-memory value stays deeply immutable so callers cannot mutate the
        fingerprinted identity after construction.  Persistence adapters should
        use this boundary instead of handing ``MappingProxyType``/tuples to a
        JSON encoder that may not understand them.
        """
        return {
            "attempt_id": str(self.attempt_id),
            "execution_decision_id": self.execution_decision_id,
            "account_id": str(self.account_id),
            "environment": self.environment,
            "symbol": self.symbol,
            "normalized_payload": _thaw_value(self.normalized_payload),
            "payload_sha256": self.payload_sha256,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "outcome_kind": (
                self.outcome_kind.value
                if isinstance(self.outcome_kind, SubmitOutcomeKind)
                else self.outcome_kind
            ),
            "outcome_reason": self.outcome_reason,
            "venue_offer_id": self.venue_offer_id,
            "last_event_seq": self.last_event_seq,
        }


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


class SubmitCancelledNotSent(asyncio.CancelledError):
    """The submit was cancelled before anything reached the venue.

    Still a ``CancelledError``, so cancellation is honoured everywhere it
    propagates; it only adds the fact a bare cancellation cannot carry: no
    request was sent, so the durable intent can be closed as NOT_SENT instead
    of being left for recovery to escalate to UNKNOWN.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"submit cancelled before transport: {reason}")
        self.outcome = SubmitNotSent(reason)


def _structured_rejection_reason(body: Any) -> str | None:
    """Extract a bounded reason only from known Bitfinex error shapes."""
    if isinstance(body, list):
        # Funding REST responses use [mts, type, message_id, _, offer, code, status, text].
        if len(body) < 7 or body[6] not in {"ERROR", "FAILURE"}:
            return None
        code = _bounded_text(body[1] if len(body) > 1 else None)
        text = _bounded_text(body[7] if len(body) > 7 else None)
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


def fingerprint_submit_payload(payload: Mapping[str, Any]) -> str:
    """Return the SHA-256 fingerprint of a normalized submit payload."""
    return sha256(_canonical_json(normalize_submit_payload(payload))).hexdigest()


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """Explicitly named alias used by the submission-attempt projection."""
    return fingerprint_submit_payload(payload)


def _bounded_response_diagnostic(
    body: Any,
    *,
    status: str,
    venue_offer_id: str | None = None,
    reason: str | None = None,
) -> dict[str, str]:
    """Keep only bounded, allowlisted response diagnostics.

    The parsed venue body can contain arbitrary text and fields.  It is useful
    for a short-lived log, but it must never become part of the durable submit
    attempt/audit envelope.  The digest preserves forensic correlation while the
    selected fields keep operator-facing diagnostics bounded.
    """
    diagnostic: dict[str, str] = {
        "response_digest": response_digest(body),
        "status": status,
    }
    if venue_offer_id is not None:
        diagnostic["venue_offer_id"] = venue_offer_id
    if reason is not None:
        bounded_reason = _bounded_text(reason)
        if bounded_reason is not None:
            diagnostic["reason"] = bounded_reason
    if isinstance(body, list):
        request_type = _bounded_text(body[1] if len(body) > 1 else None)
        if request_type is not None:
            diagnostic["request_type"] = request_type
        venue_status = _bounded_text(body[6] if len(body) > 6 else None)
        if venue_status is not None:
            diagnostic["venue_status"] = venue_status
    elif isinstance(body, Mapping):
        for key in ("code", "CODE", "message", "text", "error"):
            value = _bounded_text(body.get(key))
            if value is not None:
                diagnostic[key.lower()] = value
    return diagnostic


def _unknown(
    reason: str,
    *,
    transport_started: bool,
    parsed_body: Any = None,
) -> SubmitOutcomeUnknown:
    venue = venue_error(parsed_body)
    return SubmitOutcomeUnknown(
        reason=reason,
        transport_started=transport_started,
        raw_response_digest=(response_digest(parsed_body) if parsed_body is not None else None),
        venue_error_code=None if venue is None else venue[0],
        venue_error_message=None if venue is None else venue[1],
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
            return SubmitAcknowledged(
                venue_offer_id=venue_offer_id,
                raw_response=_bounded_response_diagnostic(
                    parsed_body,
                    status="SUCCESS",
                    venue_offer_id=venue_offer_id,
                ),
            )
        if rejection_reason is not None:
            return SubmitRejected(
                reason=rejection_reason,
                raw_response=_bounded_response_diagnostic(
                    parsed_body,
                    status="REJECTED",
                    reason=rejection_reason,
                ),
            )
        reason = "malformed_response" if isinstance(parsed_body, list) else "unrecognized_response"
        return _unknown(reason, transport_started=True, parsed_body=parsed_body)

    if (
        400 <= http_status < 500
        and http_status in ALLOWLISTED_4XX_REJECTION_STATUSES
        and rejection_reason is not None
    ):
        return SubmitRejected(
            reason=rejection_reason,
            raw_response=_bounded_response_diagnostic(
                parsed_body,
                status=f"HTTP_{http_status}",
                reason=rejection_reason,
            ),
        )

    return _unknown("unrecognized_response", transport_started=True, parsed_body=parsed_body)
