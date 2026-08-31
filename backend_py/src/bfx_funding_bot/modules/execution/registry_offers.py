"""OfferRegistry pure types and FSM transition function.

§6.1 — OfferRegistry state machine.

Tracks the lifecycle of a venue offer from the moment it is claimed
(capital reserved at Bitfinex) through to its terminal state (filled or
released).  This module is deliberately free of I/O, clock calls, and
side-effects so that the transition logic can be tested deterministically.

State graph:
  PENDING  — reserved for write-ahead intent; durable, voi unknown
  CLAIMED  — ReservationClaimed received; offer live at venue
  RELEASED — OrderFilled or ReservationReleased received; terminal state
  FAILED   — submit-failed terminal

Transition rules:
  ReservationClaimed  + voi not in snapshot  → add CLAIMED record
  ReservationClaimed  + voi exists           → no-op (idempotent dedup)
  OrderFilled         + voi not in snapshot  → info diag "fill before claim";
                                               no mutation (reconcile converges)
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

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.exchange_accounts import account_scope_clause
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)

log = logging.getLogger(__name__)


class RegistryState(Enum):
    PENDING = "pending"   # A2 write-ahead intent — durable, voi unknown
    CLAIMED = "claimed"
    RELEASED = "released"
    FAILED = "failed"     # A2 submit-failed terminal


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
    symbol: str = "fUSD"  # populated from ReservationClaimed.symbol
    reservation_ref: ReservationRef | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticLog:
    level: str  # "info" | "warn" | "error"
    message: str
    venue_offer_id: str | None = None


class ReservationCorrelationError(RuntimeError):
    """A venue lifecycle event cannot be uniquely matched to one reservation."""


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
        if incoming.reservation_ref is None:
            return snapshot, [DiagnosticLog(
                level="error",
                message=f"uncorrelated reservation claim for voi={voi}",
                venue_offer_id=voi,
            )]
        if voi in snapshot:
            existing = snapshot[voi]
            if (
                existing.reservation_ref is not None
                and incoming.reservation_ref is not None
                and existing.cid == incoming.cid
                and existing.reservation_ref == incoming.reservation_ref
                and existing.signal_correlation_id == incoming.signal_correlation_id
            ):
                return snapshot, []  # exact idempotent duplicate
            return snapshot, [DiagnosticLog(
                level="error",
                message=f"conflicting reservation correlation for voi={voi}",
                venue_offer_id=voi,
            )]
        occurred = incoming.occurred_at_ms if incoming.occurred_at_ms is not None else now_ms
        assert incoming.amount is not None  # invariant: _resolve_amount guarantees this
        record = ClaimRecord(
            venue_offer_id=voi,
            cid=incoming.cid,
            signal_correlation_id=incoming.signal_correlation_id,
            size_usdt=incoming.amount,
            account_id=incoming.account_id,
            state=RegistryState.CLAIMED,
            occurred_at_ms=occurred,
            last_updated_ms=now_ms,
            symbol=incoming.symbol,
            reservation_ref=incoming.reservation_ref,
        )
        return {**snapshot, voi: record}, []

    # ── OrderFilled ─────────────────────────────────────────────────────────
    if isinstance(incoming, OrderFilled):
        if incoming.reservation_ref is None:
            return snapshot, [DiagnosticLog(
                level="error",
                message=f"uncorrelated fill for voi={voi}",
                venue_offer_id=voi,
            )]
        if voi not in snapshot:
            return snapshot, [
                DiagnosticLog(
                    level="error",
                    message=f"unmatched fill correlation for voi={voi}",
                    venue_offer_id=voi,
                )
            ]
        existing = snapshot[voi]
        if existing.reservation_ref is None or existing.reservation_ref != incoming.reservation_ref:
            return snapshot, [DiagnosticLog("error", f"conflicting fill correlation for voi={voi}", voi)]
        if existing.state == RegistryState.RELEASED:
            # Idempotent
            return snapshot, []
        # CLAIMED → RELEASED
        updated = replace(existing, state=RegistryState.RELEASED, last_updated_ms=now_ms)
        return {**snapshot, voi: updated}, []

    # ── ReservationReleased ─────────────────────────────────────────────────
    if isinstance(incoming, ReservationReleased):
        if incoming.reservation_ref is None:
            return snapshot, [DiagnosticLog(
                level="error",
                message=f"uncorrelated release for voi={voi}",
                venue_offer_id=voi,
            )]
        if voi not in snapshot:
            return snapshot, [
                DiagnosticLog(
                    level="error",
                    message=f"unmatched release correlation for voi={voi}",
                    venue_offer_id=voi,
                )
            ]
        existing = snapshot[voi]
        if existing.reservation_ref is None or existing.reservation_ref != incoming.reservation_ref:
            return snapshot, [DiagnosticLog("error", f"conflicting release correlation for voi={voi}", voi)]
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


# ---------------------------------------------------------------------------
# Imperative shell — OfferRegistry
# ---------------------------------------------------------------------------

class OfferRegistry:
    """In-memory FSM projection of ReservationClaimed/OrderFilled/ReservationReleased.

    Subscribe to bus → handle each event → atomic snapshot swap.
    Cold-start: from_snapshot loads state from PostgreSQL offer_claims table (no replay).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._snapshot: dict[str, ClaimRecord] = {}

    def snapshot(self) -> dict[str, ClaimRecord]:
        """Return immutable view (dict copy)."""
        return dict(self._snapshot)

    async def handle(self, event: Any) -> None:
        """Bus subscriber callback — transition + atomic swap."""
        now_ms = self._clock()
        new_snapshot, diags = transition(self._snapshot, event, now_ms)
        for d in diags:
            if d.level == "error":
                raise ReservationCorrelationError(d.message)
            level_fn = log.info if d.level == "info" else log.warning
            level_fn(
                "offer_registry_diag voi=%s msg=%s",
                d.venue_offer_id, d.message,
            )
        self._snapshot = new_snapshot

    # ---------- cold-start loader (PostgreSQL snapshot) ----------

    @classmethod
    async def from_snapshot(
        cls,
        session: AsyncSession,
        *,
        account_id: str,
        deployment_environment: str,
        clock: Callable[[], int] | None = None,
    ) -> OfferRegistry:
        """Load registry from the offer_claims snapshot table (no replay)."""
        from sqlalchemy import select

        from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow

        reg = cls(clock=clock)
        rows = (
            await session.execute(
                select(OfferClaimRow).where(
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=OfferClaimRow.exchange_account_id,
                        legacy_account_column=OfferClaimRow.account_id,
                    ),
                    OfferClaimRow.deployment_environment == deployment_environment,
                    OfferClaimRow.venue_offer_id.is_not(None),
                )
            )
        ).scalars().all()
        for r in rows:
            assert r.venue_offer_id is not None
            if r.venue_offer_id in reg._snapshot:
                raise ReservationCorrelationError(
                    f"ambiguous offer claim rows for voi={r.venue_offer_id}",
                )
            reference = None
            if r.execution_decision_id is not None:
                reference = ReservationRef(
                    execution_decision_id=r.execution_decision_id,
                    cid=r.cid,
                    signal_correlation_id=UUID(r.signal_correlation_id),
                    venue_offer_id=r.venue_offer_id,
                )
            reg._snapshot[r.venue_offer_id] = ClaimRecord(
                venue_offer_id=r.venue_offer_id,
                cid=r.cid,
                signal_correlation_id=UUID(r.signal_correlation_id),
                size_usdt=Decimal(str(r.size_usdt)),
                account_id=(
                    str(exchange_account_id)
                    if (exchange_account_id := getattr(r, "exchange_account_id", None))
                    is not None
                    else r.account_id
                ),
                state=RegistryState(r.state),
                occurred_at_ms=r.occurred_at_ms,
                last_updated_ms=r.last_updated_ms,
                symbol=r.symbol,
                reservation_ref=reference,
            )
        return reg

    def cleanup_terminal(self, older_than_ms: int) -> None:
        """Remove RELEASED records whose last_updated_ms is older than threshold.

        Args:
            older_than_ms: TTL threshold (e.g. 24 * 3600 * 1000 for 24hr).
                Records with last_updated_ms < (now - older_than_ms) and state=RELEASED
                are removed. CLAIMED records are never cleaned.
        """
        now = self._clock()
        threshold = now - older_than_ms
        self._snapshot = {
            voi: rec for voi, rec in self._snapshot.items()
            if rec.state != RegistryState.RELEASED or rec.last_updated_ms >= threshold
        }
