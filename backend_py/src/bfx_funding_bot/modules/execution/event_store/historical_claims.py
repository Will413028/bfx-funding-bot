"""Prevalidate stored claim cycles; authority never reaches the live projector."""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect

from bfx_funding_bot.modules.execution.event_store.serialization import deserialize_stored_event
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

_INTENT = "RESERVATION_INTENT"
_CLAIM = "RESERVATION_CLAIMED"
_FAILED = "RESERVATION_FAILED"
_TERMINALS = frozenset({_FAILED, "ORDER_FILL", "RESERVATION_RELEASED"})
_TYPES = frozenset({
    _INTENT, _CLAIM, *_TERMINALS,
    "SUBMIT_OUTCOME_UNKNOWN", "SUBMIT_MATCHED_TO_VENUE_OFFER",
})


@dataclass
class _Cycle:
    start: int
    identity: tuple[str, Decimal, str]
    historical: bool
    began_with_intent: bool
    state: str
    venue: str | None
    decision: str | None


def historical_claim_reset_sequences(
    rows: Sequence[EventLogRow], *, account_id: str, environment: str,
) -> frozenset[int]:
    """Return reset intents only after validating the entire immutable scope.

    Single-cycle streams retain the strict projector's identity semantics.
    Reused CIDs additionally need complete historical INTENT -> CLAIM -> full
    FILL/RELEASE, or INTENT -> FAILED evidence for every predecessor. Partial
    fills cannot establish that boundary: their amount differs from the claim.
    Venue ownership is retained across resets so an older cycle cannot return.
    """
    decoded: list[tuple[EventLogRow, Any]] = []
    previous_seq = 0
    for row in rows:
        if not inspect(row).persistent or row.event_seq is None:
            raise TypeError("historical claim replay requires a persistent event_log row")
        owner = str(row.exchange_account_id) if row.exchange_account_id else row.account_id
        if owner != account_id or row.deployment_environment != environment:
            raise ValueError("historical claim replay scope conflict")
        if row.event_seq <= previous_seq:
            raise ValueError("historical claim replay requires increasing event_seq")
        previous_seq = row.event_seq
        event: Any = deserialize_stored_event(row)
        if getattr(event, "account_id", account_id) != account_id:
            raise ValueError("historical claim replay decoded account conflict")
        if row.payload.get("account_id") not in {row.account_id, account_id}:
            raise ValueError("historical claim replay payload account conflict")
        if row.event_type not in _TYPES:
            continue
        if row.cid != event.cid or row.venue_offer_id != getattr(event, "venue_offer_id", None):
            raise ValueError("historical claim replay row identity conflict")
        if row.payload.get("is_legacy_uncorrelated"):
            raise ValueError("historical claim replay rejects caller historical flags")
        decoded.append((row, event))

    intent_counts = Counter(row.cid for row, _ in decoded if row.event_type == _INTENT)
    cycles: dict[int, _Cycle] = {}
    venues: dict[str, tuple[int, int]] = {}
    decisions: dict[str, int] = {}
    resets: set[int] = set()
    for row, event in decoded:
        cid = event.cid
        if cid is None:
            continue
        kind = row.event_type
        amount = Decimal(str(event.amount))
        identity = (event.symbol, amount, str(event.signal_correlation_id))
        ref = getattr(event, "reservation_ref", None)
        decision = getattr(event, "execution_decision_id", None)
        if ref is not None:
            decision = ref.execution_decision_id
        historical = (
            row.schema_version == 2
            and getattr(event, "is_legacy_uncorrelated", False)
            and ref is None and decision is None
            and getattr(event, "submission_attempt", None) is None
            # Legacy constructors ignore undeclared payload fields. Known
            # modern audit identity must not hide in those ignored fields.
            and row.payload.get("execution_decision_id") is None
            and row.payload.get("submission_attempt") is None
        )
        venue = getattr(event, "venue_offer_id", None)
        reused = intent_counts[cid] > 1
        cycle = cycles.get(cid)
        if cycle is not None and kind == _INTENT:
            if not (
                cycle.historical and historical and cycle.began_with_intent
                and cycle.state in _TERMINALS
            ):
                raise ValueError(f"historical claim replay cid={cid}: unproven cycle boundary")
            resets.add(row.event_seq)
            cycle = None
        if cycle is None:
            if reused and (kind != _INTENT or not historical):
                raise ValueError(f"historical claim replay cid={cid}: missing historical intent")
            cycle = _Cycle(row.event_seq, identity, historical, kind == _INTENT,
                           kind, None, None)
            cycles[cid] = cycle
        else:
            if identity != cycle.identity:
                raise ValueError(f"historical claim replay cid={cid}: cycle identity conflict")
            if cycle.venue is not None and venue != cycle.venue:
                raise ValueError(f"historical claim replay cid={cid}: venue offer conflict")
            if cycle.decision is not None and decision != cycle.decision:
                raise ValueError(f"historical claim replay cid={cid}: decision conflict")
            if reused:
                valid_transition = (
                    (cycle.state == _INTENT and kind in {_CLAIM, _FAILED})
                    or (cycle.state == _CLAIM and kind in {"ORDER_FILL", "RESERVATION_RELEASED"})
                )
                if not historical or not cycle.historical or not valid_transition:
                    raise ValueError(f"historical claim replay cid={cid}: unproven lifecycle")
            cycle.historical = cycle.historical and historical
            cycle.state = kind
        if reused and (
            not amount.is_finite() or amount <= 0
            or not event.symbol or event.signal_correlation_id is None
            or (kind in {_INTENT, _FAILED} and venue is not None)
            or (kind in {_CLAIM, "ORDER_FILL", "RESERVATION_RELEASED"} and not venue)
        ):
            raise ValueError(f"historical claim replay cid={cid}: incomplete cycle identity")
        if venue is not None:
            attribution = (cid, cycle.start)
            if venue in venues and venues[venue] != attribution:
                raise ValueError(f"historical claim replay cid={cid}: stale or shared venue offer")
            venues[venue] = attribution
            cycle.venue = venue
        if decision is not None:
            if decision in decisions and decisions[decision] != cid:
                raise ValueError(f"historical claim replay cid={cid}: shared decision")
            decisions[decision] = cid
            cycle.decision = decision
    return frozenset(resets)
