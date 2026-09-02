"""Pure identity and entity-reduction primitives for serialized execution.

Database transactions, advisory locks, and SQLAlchemy upserts belong to the
writer layer.  This module stays deterministic and side-effect free so the
same event stream can be used for a live projection and an empty-schema replay.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditState,
    VenueOfferState,
    is_terminal_offer_status,
)

__all__ = [
    "V2_EVENT_NAMESPACE",
    "InvalidVenueOfferTransition",
    "VenueCreditState",
    "VenueOfferState",
    "apply_offer_transition",
    "derive_v2_event_id",
    "gross_exposure",
]


# UUIDv5 namespaces are part of the compatibility contract.  Do not replace
# this with uuid4 or a process-local namespace: historical rows must produce the
# same identity after a restart, on another host, and during an audit replay.
V2_EVENT_NAMESPACE = uuid5(NAMESPACE_URL, "bfx-funding-bot/event-log/v2")


class InvalidVenueOfferTransition(ValueError):  # noqa: N818 - public contract name
    """An event would violate the venue-offer state machine or event order."""


def derive_v2_event_id(
    *,
    event_seq: int,
    account_id: str,
    deployment_environment: str,
    event_type: str,
    occurred_at_ms: int,
    payload: dict[str, Any],
) -> UUID:
    """Derive the immutable UUIDv5 identity for a pre-v3 event row.

    JSON is canonicalized with sorted keys before it participates in the UUID
    name, so equivalent dictionaries produce the same result regardless of
    insertion order.  ``default=str`` keeps this helper usable for historical
    JSON-decoded Decimal/UUID-compatible values without mutating the payload.
    """
    payload_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    name = json.dumps(
        [
            int(event_seq),
            str(account_id),
            str(deployment_environment),
            str(event_type),
            int(occurred_at_ms),
            payload_json,
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return uuid5(V2_EVENT_NAMESPACE, name)


def _decimal(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def gross_exposure(
    *,
    offered_amount: Decimal,
    lent_amount: Decimal,
    uncertain_amount: Decimal,
) -> Decimal:
    """Return account gross exposure using exact Decimal arithmetic."""
    return _decimal(offered_amount) + _decimal(lent_amount) + _decimal(uncertain_amount)


_NON_TERMINAL_STATUS_ALIASES = frozenset({
    "active",
    "open",
    "pending",
    "submitted",
    "claiming",
    "claimed",
    "partially_filled",
    "partial",
    "unknown",
})


def _normalize_status(status: str) -> str:
    return str(status).strip().lower().replace(" ", "_")


def _status_is_known(status: str) -> bool:
    return status in _NON_TERMINAL_STATUS_ALIASES or is_terminal_offer_status(status)


def apply_offer_transition(
    previous: VenueOfferState | None,
    *,
    status: str,
    event_seq: int,
    mts_updated: int,
    venue_offer_id: str | None = None,
    symbol: str | None = None,
    amount_original: Decimal | int | float | str | None = None,
    amount_remaining: Decimal | int | float | str | None = None,
    rate: Decimal | int | float | str | None = None,
    period_days: int | None = None,
    mts_created: int | None = None,
    cid: int | None = None,
    execution_decision_id: str | None = None,
    signal_correlation_id: UUID | None = None,
    flags: dict[str, Any] | None = None,
) -> VenueOfferState:
    """Apply one venue-offer observation/event without mutating prior state.

    Event sequence is the ordering authority.  A repeated terminal event is
    idempotent; a terminal object can never be reopened or changed into another
    terminal state.  Unknown statuses fail closed instead of silently creating
    a state that a replay cannot reason about.
    """
    next_status = _normalize_status(status)
    if not next_status or not _status_is_known(next_status):
        raise InvalidVenueOfferTransition(f"unsupported venue offer status: {status!r}")
    if event_seq < 0:
        raise InvalidVenueOfferTransition("event_seq must be non-negative")

    if previous is None:
        if venue_offer_id is None or symbol is None:
            raise InvalidVenueOfferTransition(
                "venue_offer_id and symbol are required for a new offer"
            )
        if amount_original is None or amount_remaining is None:
            raise InvalidVenueOfferTransition(
                "amount_original and amount_remaining are required for a new offer"
            )
        if mts_updated < 0:
            raise InvalidVenueOfferTransition("mts_updated must be non-negative")
        if mts_created is not None and mts_created < 0:
            raise InvalidVenueOfferTransition("mts_created must be non-negative")
        return VenueOfferState(
            venue_offer_id=venue_offer_id,
            symbol=symbol,
            amount_original=_decimal(amount_original),
            amount_remaining=_decimal(amount_remaining),
            rate=None if rate is None else _decimal(rate),
            period_days=period_days,
            status=next_status,
            mts_created=mts_updated if mts_created is None else mts_created,
            mts_updated=mts_updated,
            first_seen_event_seq=event_seq,
            last_seen_event_seq=event_seq,
            cid=cid,
            execution_decision_id=execution_decision_id,
            signal_correlation_id=signal_correlation_id,
            flags={} if flags is None else flags,
        )

    if event_seq < previous.last_seen_event_seq:
        raise InvalidVenueOfferTransition(
            f"event_seq {event_seq} precedes last seen {previous.last_seen_event_seq}"
        )
    if venue_offer_id is not None and venue_offer_id != previous.venue_offer_id:
        raise InvalidVenueOfferTransition("venue offer identity changed")
    if symbol is not None and symbol != previous.symbol:
        raise InvalidVenueOfferTransition("venue offer symbol changed")
    if mts_updated < previous.mts_updated:
        raise InvalidVenueOfferTransition(
            f"mts_updated {mts_updated} precedes last seen {previous.mts_updated}"
        )

    if event_seq == previous.last_seen_event_seq:
        # Same stream position is a duplicate only when it describes the same
        # state.  Treating a conflicting duplicate as a no-op would hide a
        # broken event identity or an ordering bug.
        if next_status != previous.status:
            raise InvalidVenueOfferTransition("conflicting duplicate event_seq")
        if mts_updated != previous.mts_updated:
            raise InvalidVenueOfferTransition("conflicting duplicate mts_updated")
        if amount_remaining is not None and _decimal(amount_remaining) != previous.amount_remaining:
            raise InvalidVenueOfferTransition("conflicting duplicate amount_remaining")
        if amount_original is not None and _decimal(amount_original) != previous.amount_original:
            raise InvalidVenueOfferTransition("conflicting duplicate amount_original")
        if rate is not None and (previous.rate is None or _decimal(rate) != previous.rate):
            raise InvalidVenueOfferTransition("conflicting duplicate rate")
        if period_days is not None and period_days != previous.period_days:
            raise InvalidVenueOfferTransition("conflicting duplicate period")
        if mts_created is not None and mts_created != previous.mts_created:
            raise InvalidVenueOfferTransition("conflicting duplicate mts_created")
        if cid is not None and cid != previous.cid:
            raise InvalidVenueOfferTransition("conflicting duplicate cid")
        if (
            execution_decision_id is not None
            and execution_decision_id != previous.execution_decision_id
        ):
            raise InvalidVenueOfferTransition("conflicting duplicate decision id")
        if (
            signal_correlation_id is not None
            and signal_correlation_id != previous.signal_correlation_id
        ):
            raise InvalidVenueOfferTransition("conflicting duplicate signal correlation")
        if flags is not None and dict(flags) != dict(previous.flags):
            raise InvalidVenueOfferTransition("conflicting duplicate flags")
        return previous

    if previous.is_terminal:
        if next_status != previous.status:
            raise InvalidVenueOfferTransition(
                f"terminal offer {previous.venue_offer_id} cannot transition "
                f"from {previous.status!r} to {next_status!r}"
            )
        if amount_original is not None and _decimal(amount_original) != previous.amount_original:
            raise InvalidVenueOfferTransition("conflicting terminal amount_original")
        if rate is not None and (previous.rate is None or _decimal(rate) != previous.rate):
            raise InvalidVenueOfferTransition("conflicting terminal rate")
        if period_days is not None and period_days != previous.period_days:
            raise InvalidVenueOfferTransition("conflicting terminal period")
        if cid is not None and cid != previous.cid:
            raise InvalidVenueOfferTransition("conflicting terminal cid")
        if (
            execution_decision_id is not None
            and execution_decision_id != previous.execution_decision_id
        ):
            raise InvalidVenueOfferTransition("conflicting terminal decision id")
        if (
            signal_correlation_id is not None
            and signal_correlation_id != previous.signal_correlation_id
        ):
            raise InvalidVenueOfferTransition("conflicting terminal signal correlation")
        if flags is not None and dict(flags) != dict(previous.flags):
            raise InvalidVenueOfferTransition("conflicting terminal flags")
        # A duplicate terminal observation may advance the stream fence while
        # retaining the terminal object.  This is safe and replay-deterministic.
        if amount_remaining is not None and _decimal(amount_remaining) != previous.amount_remaining:
            raise InvalidVenueOfferTransition("conflicting terminal amount_remaining")
        return replace(
            previous,
            last_seen_event_seq=event_seq,
            mts_updated=max(previous.mts_updated, mts_updated),
        )

    next_amount_original = (
        previous.amount_original
        if amount_original is None
        else _decimal(amount_original)
    )
    next_amount_remaining = (
        previous.amount_remaining
        if amount_remaining is None
        else _decimal(amount_remaining)
    )
    if next_amount_original < 0 or next_amount_remaining < 0:
        raise InvalidVenueOfferTransition("offer amounts must be non-negative")
    if next_amount_remaining > next_amount_original:
        raise InvalidVenueOfferTransition("amount_remaining exceeds amount_original")

    return replace(
        previous,
        amount_original=next_amount_original,
        amount_remaining=next_amount_remaining,
        rate=previous.rate if rate is None else _decimal(rate),
        period_days=previous.period_days if period_days is None else period_days,
        status=next_status,
        mts_created=previous.mts_created if mts_created is None else mts_created,
        mts_updated=mts_updated,
        last_seen_event_seq=event_seq,
        is_terminal=is_terminal_offer_status(next_status),
        cid=previous.cid if cid is None else cid,
        execution_decision_id=(
            previous.execution_decision_id
            if execution_decision_id is None
            else execution_decision_id
        ),
        signal_correlation_id=(
            previous.signal_correlation_id
            if signal_correlation_id is None
            else signal_correlation_id
        ),
        flags=previous.flags if flags is None else flags,
    )
