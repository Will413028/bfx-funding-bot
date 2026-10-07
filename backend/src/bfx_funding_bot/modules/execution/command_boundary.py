"""The command gate's boundary: a scope, a journal, and its effects.

``CommandBoundary`` is everything the gate needs from the capital authority:
the scope it commands, the session factory its admission transaction runs on,
the ``CommandJournal`` that makes an attempt and its outcome durable, and the
``CommandEffects`` that run once an outcome is durable. The ledger's effects only
announce the committed outcome.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.ledger import CommandJournal, CommandOutcome, OutcomeKind, Scope

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandFacts:
    """What the gate knew about one submit when its outcome became durable."""

    scope: Scope
    attempt_id: UUID
    symbol: str
    amount: Decimal
    signal_correlation_id: UUID
    reference: ReservationRef
    offer_rate: Decimal | None
    is_simulated: bool


@dataclass(frozen=True, slots=True)
class CommandOutcomeNotice:
    """One durable submit outcome, announced strictly after its transaction committed."""

    scope: Scope
    attempt_id: UUID
    kind: OutcomeKind
    symbol: str
    venue_offer_id: str | None
    amount: Decimal
    completed_at_ms: int


class CommandEffects(Protocol):
    """What follows a durable outcome; an authority chooses its own."""

    def new_event_id(self) -> UUID | None:
        """The identity an authority that stores events wants shared by its row and its notice."""
        ...

    async def outcome_recorded(self, facts: CommandFacts, outcome: CommandOutcome) -> None:
        """Run after ``CommandJournal.record_outcome`` returned (its transaction committed)."""
        ...


@dataclass(frozen=True, slots=True)
class CommandBoundary:
    scope: Scope
    session_factory: async_sessionmaker[AsyncSession]
    journal: CommandJournal
    effects: CommandEffects


class LedgerCommandEffects:
    """Announce the committed outcome; the ledger journal is the only record of it."""

    def __init__(self, bus: DomainEventBus) -> None:
        self._bus = bus

    def new_event_id(self) -> UUID | None:
        return None

    async def outcome_recorded(self, facts: CommandFacts, outcome: CommandOutcome) -> None:
        await publish_best_effort(self._bus, CommandOutcomeNotice(
            facts.scope, facts.attempt_id, outcome.kind, facts.symbol,
            outcome.venue_offer_id, facts.amount, outcome.completed_at_ms,
        ))


async def publish_best_effort(bus: DomainEventBus, event: object) -> None:
    """Bus failure must not fail a submit: the outcome is already durable."""
    try:
        await bus.publish(event)
    except Exception as exc:
        log.critical(
            "bus_publish_failed_outer event=%s err=%r — projection lost, SoT already persisted",
            type(event).__name__,
            exc,
        )
