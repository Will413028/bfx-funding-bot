from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.event_store.replay import (
    HistoricalReplayProvenance,
    _HistoricalReplayAuthorization,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import (
    __SCHEMA_VERSION__,
    DEFAULT_RECONCILE_SYMBOL,
    CancelAcknowledged,
    CancelRequested,
    CreditClosed,
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationReleased,
    ReservationUnknown,
    SnapshotCoverage,
    SubmitMatchedToVenueOffer,
    UncertaintyBoundToVenueOffer,
    UncertaintyManuallyResolved,
    UncertaintyMarkedNotAccepted,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
    _construct_historical_legacy_event,
)

# event_type string <-> domain class. Clean field names (we own this schema).
_TYPE_BY_CLASS: dict[type, str] = {
    CancelRequested: "CANCEL_REQUESTED",
    CancelAcknowledged: "CANCEL_ACKNOWLEDGED",
    ReservationIntent: "RESERVATION_INTENT",
    ReservationClaimed: "RESERVATION_CLAIMED",
    ReservationFailed: "RESERVATION_FAILED",
    OrderFilled: "ORDER_FILL",
    ReservationReleased: "RESERVATION_RELEASED",
    CreditClosed: "CREDIT_CLOSED",
    ReservationUnknown: "SUBMIT_OUTCOME_UNKNOWN",
    VenueOfferQuarantined: "VENUE_OFFER_QUARANTINED",
    SubmitMatchedToVenueOffer: "SUBMIT_MATCHED_TO_VENUE_OFFER",
    UncertaintyBoundToVenueOffer: "UNCERTAINTY_BOUND_TO_VENUE_OFFER",
    UncertaintyMarkedNotAccepted: "UNCERTAINTY_MARKED_NOT_ACCEPTED",
    UncertaintyManuallyResolved: "UNCERTAINTY_MANUALLY_RESOLVED",
    VenueSnapshotObserved: "VENUE_SNAPSHOT_OBSERVED",
}
_CLASS_BY_TYPE: dict[str, type] = {v: k for k, v in _TYPE_BY_CLASS.items()}

_FIELDS: dict[type, list[str]] = {
    cls: [f.name for f in dataclasses.fields(cls) if f.init]
    for cls in _CLASS_BY_TYPE.values()
}
_DECIMAL_FIELDS = {"size_usdt", "amount"}
_UUID_FIELDS = {"signal_correlation_id", "event_id", "uncertainty_id"}
_REF_FIELD = "reservation_ref"
_SUPPORTED_SCHEMA_VERSIONS = frozenset({2, __SCHEMA_VERSION__})
_CORRELATION_EVENT_TYPES = frozenset({
    "RESERVATION_INTENT",
    "RESERVATION_CLAIMED",
    "RESERVATION_FAILED",
    "SUBMIT_OUTCOME_UNKNOWN",
    "SUBMIT_MATCHED_TO_VENUE_OFFER",
    "ORDER_FILL",
    "RESERVATION_RELEASED",
})


@dataclasses.dataclass(frozen=True, slots=True)
class StoredEventIdentity:
    """Stable identity assigned while decoding one persisted event row.

    ``native`` identifies a schema-v3 event carrying its own UUID.  ``derived_v2``
    is the deterministic UUIDv5 compatibility identity for a historical row;
    the immutable row is intentionally left untouched.
    """

    event_id: UUID
    source: Literal["native", "derived_v2"]


def event_type_of(event: object) -> str:
    try:
        return _TYPE_BY_CLASS[type(event)]
    except KeyError:
        raise ValueError(f"unserializable event type: {type(event).__name__}") from None


def serialize_event(event: object) -> dict[str, Any]:
    """Domain event -> JSON-safe payload dict. Decimal->str, UUID->str."""
    etype = event_type_of(event)
    if getattr(event, "is_legacy_uncorrelated", False):
        raise ValueError("historical replay events cannot be serialized as current events")
    out: dict[str, Any] = {}
    for field in _FIELDS[type(event)]:
        out[field] = _encode_value(getattr(event, field))
    out["__event_type__"] = etype
    out["__schema_version__"] = __SCHEMA_VERSION__
    return out


def deserialize_event(event_type: str, payload: dict[str, Any]) -> object:
    """Decode an ordinary current-schema payload.

    Unversioned payloads are never interpreted as historical here.  Pre-version
    rows can only be decoded by :func:`deserialize_stored_event`, which requires
    durable ORM-row provenance.
    """
    version = payload.get("__schema_version__")
    if version is None:
        raise ValueError("unversioned event payload requires EventStore historical replay")
    if version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported event schema version: {version!r}")
    payload_event_type = payload.get("__event_type__")
    if payload_event_type != event_type:
        raise ValueError(
            f"event payload type mismatch: expected {event_type}, got {payload_event_type!r}",
        )
    if version == __SCHEMA_VERSION__:
        raw_event_id = payload.get("event_id")
        if raw_event_id is None:
            raise ValueError("schema-v3 event payload requires event_id")
        try:
            UUID(str(raw_event_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("schema-v3 event payload has invalid event_id") from exc
    decoded = _decode_payload(event_type, payload, schema_version=version)
    if version == 2 and hasattr(decoded, "schema_version"):
        object.__setattr__(decoded, "schema_version", 2)
    return decoded


def deserialize_stored_event(row: EventLogRow) -> object:
    """Decode one persistent event-log row, including genuine legacy rows.

    The returned domain event carries the resolved ``event_id``.  Call
    :func:`stored_event_identity` when the caller also needs the provenance
    (native v3 versus deterministic v2 compatibility identity).
    """
    payload = row.payload
    if not isinstance(payload, dict):
        raise TypeError("stored event payload must be an object")
    version = payload.get("__schema_version__")
    _validate_stored_schema_version(row, version)
    if version is not None:
        if version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported event schema version: {version!r}")
        payload_event_type = payload.get("__event_type__")
        if payload_event_type != row.event_type:
            raise ValueError(
                "stored event payload type mismatch: "
                f"expected {row.event_type}, got {payload_event_type!r}"
            )
    if version == __SCHEMA_VERSION__:
        decoded = deserialize_event(row.event_type, payload)
        identity = stored_event_identity(row)
        decoded = _with_canonical_account_id(decoded, row)
        if row.event_id is not None and row.event_id != identity.event_id:
            raise ValueError("stored event_id does not match schema-v3 payload")
        return decoded
    provenance = HistoricalReplayProvenance.from_stored_event(row)
    authorization = provenance.authorize_legacy_payload(
        event_type=row.event_type,
        payload=payload,
    )
    decoded = _decode_payload(
        row.event_type,
        payload,
        historical_authorization=authorization,
        schema_version=version,
    )
    _set_event_identity(decoded, stored_event_identity(row))
    return _with_canonical_account_id(decoded, row)


def stored_event_identity(row: EventLogRow) -> StoredEventIdentity:
    """Resolve a stored row's identity without changing its payload or row.

    Schema-v3 rows use their native UUID.  Both versioned-v2 and unversioned
    historical rows use the immutable row tuple and a stable UUIDv5 namespace.
    """
    payload = row.payload
    if not isinstance(payload, dict):
        raise TypeError("stored event payload must be an object")
    version = payload.get("__schema_version__")
    _validate_stored_schema_version(row, version)
    if version == __SCHEMA_VERSION__:
        raw_event_id = payload.get("event_id")
        if raw_event_id is None:
            raise ValueError("schema-v3 event payload requires event_id")
        try:
            event_id = UUID(str(raw_event_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("schema-v3 event payload has invalid event_id") from exc
        if row.event_id is not None and row.event_id != event_id:
            raise ValueError("stored event_id does not match schema-v3 payload")
        return StoredEventIdentity(event_id=event_id, source="native")
    if version is not None and version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported event schema version: {version!r}")
    from bfx_funding_bot.modules.execution.event_store.projector import derive_v2_event_id

    if row.event_seq is None:
        raise TypeError("historical event identity requires persistent event_seq")
    return StoredEventIdentity(
        event_id=derive_v2_event_id(
            event_seq=row.event_seq,
            account_id=row.account_id,
            deployment_environment=row.deployment_environment,
            event_type=row.event_type,
            occurred_at_ms=row.occurred_at_ms,
            payload=payload,
        ),
        source="derived_v2",
    )


def _validate_stored_schema_version(row: EventLogRow, payload_version: Any) -> None:
    """Reject a row whose metadata and immutable payload disagree.

    ``schema_version`` was added after historical rows already existed, so a
    transient ORM object may expose ``None`` before its server default is
    loaded.  Persistent rows always carry 2 or 3 and must agree exactly with
    the payload marker; an unversioned historical payload is equivalent to v2.
    """
    row_version = getattr(row, "schema_version", None)
    if row_version is None:
        return
    if row_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported stored event schema version: {row_version!r}")
    expected = 2 if payload_version is None else payload_version
    if row_version != expected:
        raise ValueError(
            "stored event schema_version does not match payload: "
            f"row={row_version!r}, payload={payload_version!r}"
        )


def _with_canonical_account_id(event: object, row: EventLogRow) -> object:
    """Use the durable UUID owner as replay identity, never legacy payload text."""
    if row.exchange_account_id is None:
        return event
    if not dataclasses.is_dataclass(event) or not hasattr(event, "account_id"):
        return event
    # Do not call ``dataclasses.replace`` here: a deliberately uncorrelated
    # historical event cannot pass the modern constructor's reservation-ref
    # validation again.  The stored row is already the trusted replay boundary.
    object.__setattr__(event, "account_id", str(row.exchange_account_id))
    return event


def _set_event_identity(event: object, identity: StoredEventIdentity) -> None:
    if not hasattr(event, "event_id"):
        return
    object.__setattr__(event, "event_id", identity.event_id)
    if identity.source == "derived_v2" and hasattr(event, "schema_version"):
        object.__setattr__(event, "schema_version", 2)


def _decode_payload(
    event_type: str,
    payload: dict[str, Any],
    *,
    historical_authorization: _HistoricalReplayAuthorization | None = None,
    schema_version: int | None = None,
) -> object:
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
    if historical_authorization is not None and payload.get("symbol") is None:
        payload = {**payload, "symbol": DEFAULT_RECONCILE_SYMBOL}
    # Task 4 introduced the immutable reservation reference. Rows written
    # before that schema have neither key; they remain explicitly
    # uncorrelated legacy data rather than receiving an invented decision id.
    is_historical_legacy = (
        historical_authorization is not None
        and event_type in _CORRELATION_EVENT_TYPES
        and _REF_FIELD not in payload
    )
    if is_historical_legacy:
        payload = {
            **payload,
            "reservation_ref": None,
        }
    if (
        is_historical_legacy
        and event_type == "RESERVATION_INTENT"
        and "execution_decision_id" not in payload
    ):
        payload = {**payload, "execution_decision_id": None}
    kwargs: dict[str, Any] = {}
    for field in _FIELDS[cls]:
        # v2 and unversioned rows predate event identity.  Omitting the field
        # lets the dataclass default generate a temporary value; the stored-row
        # decoder immediately replaces it with deterministic UUIDv5 identity.
        if field == "event_id" and schema_version != __SCHEMA_VERSION__:
            continue
        raw = payload.get(field)
        if cls is VenueSnapshotObserved:
            if field == "offers":
                if not isinstance(raw, list):
                    raise TypeError("snapshot offers must be an array")
                kwargs[field] = tuple(
                    _decode_nested(VenueOfferObservation, value) for value in raw
                )
                continue
            if field == "credits":
                if not isinstance(raw, list):
                    raise TypeError("snapshot credits must be an array")
                kwargs[field] = tuple(
                    _decode_nested(VenueCreditObservation, value) for value in raw
                )
                continue
            if field == "offer_history":
                if raw is None:
                    kwargs[field] = ()
                elif not isinstance(raw, list):
                    raise TypeError("snapshot offer_history must be an array")
                else:
                    kwargs[field] = tuple(
                        _decode_nested(VenueOfferObservation, value) for value in raw
                    )
                continue
            if field == "wallet_available":
                if not isinstance(raw, dict):
                    raise TypeError("snapshot wallet_available must be an object")
                kwargs[field] = {
                    str(symbol): Decimal(str(amount))
                    for symbol, amount in raw.items()
                }
                continue
            if field == "coverage":
                if not isinstance(raw, dict):
                    raise TypeError("snapshot coverage must be an object")
                kwargs[field] = SnapshotCoverage(**raw)
                continue
        kwargs[field] = _coerce(field, raw)
    if is_historical_legacy:
        assert historical_authorization is not None
        return _construct_historical_legacy_event(
            cls=cls,
            event_type=event_type,
            kwargs=kwargs,
            historical_authorization=historical_authorization,
        )
    if historical_authorization is not None:
        historical_authorization.consume(event_type=event_type)
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


def _encode_value(value: Any) -> Any:
    """Encode nested immutable snapshot values without leaking Python types."""
    if isinstance(value, ReservationRef):
        return {
            "execution_decision_id": value.execution_decision_id,
            "cid": value.cid,
            "signal_correlation_id": str(value.signal_correlation_id),
            "venue_offer_id": value.venue_offer_id,
        }
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value):
        return {
            field.name: _encode_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.init
        }
    if isinstance(value, Mapping):
        return {str(key): _encode_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode_value(item) for item in value]
    return value


def _decode_nested(cls: type[Any], raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise TypeError(f"{cls.__name__} must be an object")
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        if not field.init:
            continue
        value = raw.get(field.name)
        if field.name in {"amount", "amount_original", "amount_remaining", "rate"}:
            value = Decimal(str(value)) if value is not None else None
        elif field.name == "signal_correlation_id" and value is not None:
            value = UUID(str(value))
        kwargs[field.name] = value
    return cls(**kwargs)
