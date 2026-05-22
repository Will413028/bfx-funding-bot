"""OfferRegistry pure types and FSM transition function.

§6.1 — OfferRegistry state machine (Phase 4.4a).

Tracks the lifecycle of a venue offer from the moment it is claimed
(capital reserved at Bitfinex) through to its terminal state (filled or
released).  This module is deliberately free of I/O, clock calls, and
side-effects so that the transition logic can be tested deterministically
and composed freely by the shell class introduced in Task 9.

State graph (4.4a subset):
  PENDING  — reserved for 4.4b multi-step pre-submission; not used here
  CLAIMED  — ReservationClaimed received; offer live at venue
  RELEASED — OrderFilled or ReservationReleased received; terminal state

Transition rules:
  ReservationClaimed  + voi not in snapshot  → add CLAIMED record
  ReservationClaimed  + voi exists           → no-op (idempotent dedup)
  OrderFilled         + voi not in snapshot  → info diag "fcn before claimed";
                                               no mutation (OOO buffer hint)
  OrderFilled         + voi RELEASED         → no-op (idempotent)
  OrderFilled         + voi CLAIMED          → CLAIMED → RELEASED
  ReservationReleased + voi not in snapshot  → warn diag "not in registry";
                                               no mutation
  ReservationReleased + voi RELEASED         → no-op (idempotent)
  ReservationReleased + voi CLAIMED          → CLAIMED → RELEASED
  Unknown event type                         → info diag "unknown event type"
  Event missing venue_offer_id               → warn diag "event missing venue_offer_id"
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)


class RegistryState(Enum):
    PENDING = "pending"   # reserved for 4.4b; not used in 4.4a
    CLAIMED = "claimed"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    venue_offer_id: str
    cid: int
    signal_correlation_id: UUID
    size_usdt: Decimal
    account_id: str
    state: RegistryState
    occurred_at_ms: int
    last_updated_ms: int


@dataclass(frozen=True, slots=True)
class DiagnosticLog:
    level: str  # "info" | "warn"
    message: str
    venue_offer_id: str | None = None


def transition(
    snapshot: dict[str, ClaimRecord],
    incoming: Any,
    now_ms: int,
) -> tuple[dict[str, ClaimRecord], list[DiagnosticLog]]:
    """Apply a single domain event to the registry snapshot.

    Pure function — no I/O, no clock, no mutation of the input dict.
    Returns a (new_snapshot, diagnostics) pair.  The caller is responsible
    for persisting the new snapshot.

    Args:
        snapshot:  Current mapping of venue_offer_id → ClaimRecord.
        incoming:  Any domain event object.  Unknown types yield a diagnostic.
        now_ms:    Caller-injected wall-clock timestamp (milliseconds since
                   epoch).  Used for last_updated_ms; enables deterministic
                   testing.

    Returns:
        (new_snapshot, list[DiagnosticLog]) — new_snapshot is always a fresh
        dict; it may be identical in *value* to snapshot for no-op cases.
    """
    # Guard: event must have venue_offer_id attribute
    if not hasattr(incoming, "venue_offer_id"):
        return snapshot, [
            DiagnosticLog(
                level="warn",
                message=f"event missing venue_offer_id: {incoming!r}",
            )
        ]

    voi: str = incoming.venue_offer_id

    # ── ReservationClaimed ──────────────────────────────────────────────────
    if isinstance(incoming, ReservationClaimed):
        if voi in snapshot:
            # Idempotent dedup — already claimed
            return snapshot, []
        occurred = incoming.occurred_at_ms if incoming.occurred_at_ms is not None else now_ms
        record = ClaimRecord(
            venue_offer_id=voi,
            cid=incoming.cid,
            signal_correlation_id=incoming.signal_correlation_id,
            size_usdt=incoming.size_usdt,
            account_id=incoming.account_id,
            state=RegistryState.CLAIMED,
            occurred_at_ms=occurred,
            last_updated_ms=now_ms,
        )
        return {**snapshot, voi: record}, []

    # ── OrderFilled ─────────────────────────────────────────────────────────
    if isinstance(incoming, OrderFilled):
        if voi not in snapshot:
            return snapshot, [
                DiagnosticLog(
                    level="info",
                    message=(
                        f"fcn before claimed — dispatcher should stage in OOO buffer (voi={voi})"
                    ),
                    venue_offer_id=voi,
                )
            ]
        existing = snapshot[voi]
        if existing.state == RegistryState.RELEASED:
            # Idempotent
            return snapshot, []
        # CLAIMED → RELEASED
        updated = replace(existing, state=RegistryState.RELEASED, last_updated_ms=now_ms)
        return {**snapshot, voi: updated}, []

    # ── ReservationReleased ─────────────────────────────────────────────────
    if isinstance(incoming, ReservationReleased):
        if voi not in snapshot:
            return snapshot, [
                DiagnosticLog(
                    level="warn",
                    message=(
                        f"release event for voi={voi} not in registry"
                        " — fill_tracker should have dedupped"
                    ),
                    venue_offer_id=voi,
                )
            ]
        existing = snapshot[voi]
        if existing.state == RegistryState.RELEASED:
            # Idempotent
            return snapshot, []
        # CLAIMED → RELEASED
        updated = replace(existing, state=RegistryState.RELEASED, last_updated_ms=now_ms)
        return {**snapshot, voi: updated}, []

    # ── Unknown event type ──────────────────────────────────────────────────
    return snapshot, [
        DiagnosticLog(
            level="info",
            message=f"unknown event type: {type(incoming).__name__}",
            venue_offer_id=voi,
        )
    ]
