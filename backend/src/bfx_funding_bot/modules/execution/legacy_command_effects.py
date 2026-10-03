"""The legacy authority's command effects: today's event_log, bus and quarantine statements.

The legacy journal already wrote the terminal Reservation* row. What remains is
what the gate used to do inline, in the same order: persist ``OrderFilled``,
publish the claim and the fill, and open the symbol's uncertainty for UNKNOWN.
``persist_simulated_outcome`` is the same sequence for the paper path, where the
persister (not a journal) writes the terminal row.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import (
    CommandFacts,
    publish_best_effort,
)
from bfx_funding_bot.modules.execution.event_store.persister import EventPersister
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationUnknown,
)
from bfx_funding_bot.modules.ledger import CommandOutcome

type UncertaintyHandler = Callable[[ReservationUnknown], Awaitable[None]]


def _fields(facts: CommandFacts, outcome: CommandOutcome) -> dict[str, object]:
    return {
        "cid": facts.reference.cid, "size_usdt": facts.amount,
        "signal_correlation_id": facts.signal_correlation_id,
        "account_id": str(facts.scope.exchange_account_id),
        "is_simulated": facts.is_simulated, "occurred_at_ms": outcome.completed_at_ms,
        "symbol": facts.symbol, "reservation_ref": facts.reference,
    }


def terminal_event(
    facts: CommandFacts, outcome: CommandOutcome,
) -> ReservationClaimed | ReservationFailed | ReservationUnknown:
    """The Reservation* event of a terminal outcome (``outcome.event_id`` names the stored row)."""
    fields = _fields(facts, outcome)
    if outcome.event_id is not None:
        fields["event_id"] = outcome.event_id
    if outcome.kind == "ack":
        return ReservationClaimed(**fields, venue_offer_id=outcome.venue_offer_id or "")  # type: ignore[arg-type]
    if outcome.kind == "unknown":
        return ReservationUnknown(**fields, reason=outcome.reason or "submit_outcome_unknown")  # type: ignore[arg-type]
    return ReservationFailed(**fields, reason=outcome.reason or "submit_rejected")  # type: ignore[arg-type]


def _filled_event(facts: CommandFacts, outcome: CommandOutcome) -> OrderFilled:
    return OrderFilled(
        **_fields(facts, outcome),  # type: ignore[arg-type]
        venue_offer_id=outcome.venue_offer_id or "", credit_id=None,
        fill_rate=float(facts.offer_rate or 0),  # paper fill event field
    )


class LegacyCommandEffects:
    """After the legacy journal recorded the terminal row."""

    def __init__(
        self, persister: EventPersister, bus: DomainEventBus, uncertainty_handler: UncertaintyHandler,
    ) -> None:
        self._persister = persister
        self._bus = bus
        self._uncertainty_handler = uncertainty_handler

    def new_event_id(self) -> UUID:
        return uuid4()

    async def outcome_recorded(self, facts: CommandFacts, outcome: CommandOutcome) -> None:
        event = terminal_event(facts, outcome)
        if isinstance(event, ReservationUnknown):
            # Lending envelope D3 level 2: the open uncertainty quarantines this
            # symbol until a snapshot resolves it; nothing is halted.
            await self._uncertainty_handler(event)
            return
        if not isinstance(event, ReservationClaimed):
            return
        filled = _filled_event(facts, outcome) if facts.filled else None
        if filled is not None:
            await self._persister.persist(filled)
        await publish_best_effort(self._bus, event)
        if filled is not None:
            await publish_best_effort(self._bus, filled)


async def persist_simulated_outcome(
    persister: EventPersister, bus: DomainEventBus,
    uncertainty_handler: UncertaintyHandler | None,
    facts: CommandFacts, outcome: CommandOutcome,
) -> None:
    """The paper path: the persister writes the terminal row (and the fill with a claim)."""
    event = terminal_event(facts, outcome)
    if isinstance(event, ReservationUnknown):
        await persister.persist(event)
        if uncertainty_handler is not None:
            await uncertainty_handler(event)
        return
    if not isinstance(event, ReservationClaimed):
        await persister.persist(event)
        return
    filled = _filled_event(facts, outcome) if facts.filled else None
    if filled is None:
        await persister.persist(event)
    else:
        await persister.persist(event, filled)
    await publish_best_effort(bus, event)
    if filled is not None:
        await publish_best_effort(bus, filled)
