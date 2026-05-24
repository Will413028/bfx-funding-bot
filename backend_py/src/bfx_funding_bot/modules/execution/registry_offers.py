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

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_upcasters import upcast_row
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.marketfeed.schemas import EventType

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


# ---------------------------------------------------------------------------
# Imperative shell — OfferRegistry
# ---------------------------------------------------------------------------

class _AxiomQueryProtocol(Protocol):
    """Structural protocol for the Axiom query adapter.

    Phase 4.4b D1: signature now mirrors `AxiomReplayQueryAdapter.fetch_events`
    (keyword-only `event_types` / `up_to_ms` / `account_id`) so the real adapter
    type-checks against this protocol. The stub `_OfferRegistryQueryStub` that
    used `**kwargs` was deleted with the wiring switch.
    """
    async def fetch_events(
        self,
        *,
        event_types: list[str],
        up_to_ms: int | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


class OfferRegistry:
    """In-memory FSM projection of ReservationClaimed/OrderFilled/ReservationReleased.

    Subscribe to bus → handle each event → atomic snapshot swap.
    Cold-start: replay_from_axiom rebuilds from event log (Phase 4.3 _AxiomQuery stub
    returns []; real adapter ships in separate ADR before 4.4b cutover).
    """

    def __init__(
        self,
        *,
        axiom_query: _AxiomQueryProtocol,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._axiom_query = axiom_query
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
            level_fn = log.info if d.level == "info" else log.warning
            level_fn(
                "offer_registry_diag voi=%s msg=%s",
                d.venue_offer_id, d.message,
            )
        self._snapshot = new_snapshot

    async def replay_from_axiom(self, up_to_ms: int | None = None) -> None:
        """Cold-start projection rebuild from Axiom event log.

        Phase 4.4a: _AxiomQueryAdapter stub returns [] → no-op rebuild.
        Real query adapter ships in separate ADR before 4.4b cutover.
        """
        raw_rows = await self._axiom_query.fetch_events(
            event_types=[EventType.RESERVATION_CLAIMED.value, EventType.ORDER_FILL.value, EventType.RESERVATION_RELEASED.value],
            up_to_ms=up_to_ms,
        )
        rows = [upcast_row(r) for r in raw_rows]
        rows.sort(key=lambda r: (r.get("occurred_at_ms") or 0, r.get("event_seq") or 0))
        for row in rows:
            event = self._parse_event(row)
            if event is not None:
                await self.handle(event)

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
        """Load registry from the offer_claims snapshot table (no replay).

        Uses __new__ + manual attribute assignment as a transition shim; a later
        plan will remove the mandatory axiom_query parameter from __init__.
        """
        from sqlalchemy import select

        from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow

        reg = cls.__new__(cls)
        reg._axiom_query = None  # type: ignore[assignment]
        reg._clock = clock or (lambda: int(time.time() * 1000))
        reg._snapshot = {}
        rows = (
            await session.execute(
                select(OfferClaimRow).where(
                    OfferClaimRow.account_id == account_id,
                    OfferClaimRow.deployment_environment == deployment_environment,
                    OfferClaimRow.venue_offer_id.is_not(None),
                )
            )
        ).scalars().all()
        for r in rows:
            assert r.venue_offer_id is not None
            reg._snapshot[r.venue_offer_id] = ClaimRecord(
                venue_offer_id=r.venue_offer_id,
                cid=r.cid,
                signal_correlation_id=UUID(r.signal_correlation_id),
                size_usdt=Decimal(str(r.size_usdt)),
                account_id=r.account_id,
                state=RegistryState(r.state),
                occurred_at_ms=r.occurred_at_ms,
                last_updated_ms=r.last_updated_ms,
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

    @staticmethod
    def _parse_event(row: dict[str, Any]) -> Any | None:
        """Parse upcast row dict to domain event. Returns None for unknown types.

        Schema bridge mirrors PaperPositionLedger.replay_from_axiom (ledger.py:164-182):
          - RESERVATION_CLAIMED: v2-aligned payload field names
          - ORDER_FILL: legacy OrderFillPayload field names (offer_id, fill_size_usdt,
            fill_price) — schema asymmetry documented in spec Out-of-scope
          - RESERVATION_RELEASED: v2-aligned payload field names

        account_id source order: row root → payload → "" (empty fallback).
        """
        et = row.get("event_type")
        payload = row.get("payload") or {}

        if et == EventType.RESERVATION_CLAIMED.value:
            return ReservationClaimed(
                cid=payload["cid"],
                venue_offer_id=payload["venue_offer_id"],
                size_usdt=Decimal(str(payload["size_usdt"])),
                signal_correlation_id=UUID(payload["signal_correlation_id"]),
                account_id=row.get("account_id") or payload.get("account_id", ""),
                is_simulated=payload.get("is_simulated", False),
                venue_seq=payload.get("venue_seq"),
                event_seq=payload.get("event_seq"),
                occurred_at_ms=payload.get("occurred_at_ms"),
                recorded_at_ms=payload.get("recorded_at_ms"),
            )

        if et == EventType.ORDER_FILL.value:
            # Legacy OrderFillPayload schema bridge (mirrors ledger.py:164-173)
            return OrderFilled(
                cid=payload["cid"],
                venue_offer_id=payload["offer_id"],         # legacy field name
                credit_id=None,                              # not in legacy schema
                size_usdt=Decimal(str(payload["fill_size_usdt"])),
                fill_rate=payload["fill_price"],
                signal_correlation_id=UUID(payload["signal_correlation_id"]),
                account_id=row.get("account_id") or payload.get("account_id", ""),
                is_simulated=payload.get("is_simulated", False),
                venue_seq=payload.get("venue_seq"),
                event_seq=payload.get("event_seq"),
                occurred_at_ms=payload.get("occurred_at_ms"),
                recorded_at_ms=payload.get("recorded_at_ms"),
            )

        if et == EventType.RESERVATION_RELEASED.value:
            return ReservationReleased(
                cid=payload["cid"],
                venue_offer_id=payload["venue_offer_id"],
                size_usdt=Decimal(str(payload["size_usdt"])),
                reason=payload["reason"],
                signal_correlation_id=UUID(payload["signal_correlation_id"]),
                account_id=row.get("account_id") or payload.get("account_id", ""),
                is_simulated=payload.get("is_simulated", False),
                venue_seq=payload.get("venue_seq"),
                event_seq=payload.get("event_seq"),
                occurred_at_ms=payload.get("occurred_at_ms"),
                recorded_at_ms=payload.get("recorded_at_ms"),
            )

        return None
