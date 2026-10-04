"""What follows a durable command outcome, per authority (statement order is the contract)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import (
    CommandFacts,
    CommandOutcomeNotice,
    LedgerCommandEffects,
)
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.legacy_command_effects import LegacyCommandEffects
from bfx_funding_bot.modules.ledger import CommandOutcome, Scope

SCOPE = Scope(uuid4(), "ci")
ATTEMPT = uuid4()
CORRELATION = uuid4()


def _facts(*, filled: bool = False) -> CommandFacts:
    return CommandFacts(
        scope=SCOPE, attempt_id=ATTEMPT, symbol="fUST", amount=Decimal("200.000005"),
        signal_correlation_id=CORRELATION,
        reference=ReservationRef("decision", 7, CORRELATION, venue_offer_id="m-1"),
        offer_rate=Decimal("0.0001"), is_simulated=False, filled=filled,
    )


def _outcome(kind: str, offer: str | None = None, reason: str | None = None,
             *, event_id=None) -> CommandOutcome:
    return CommandOutcome(kind, offer, reason, 1100, {}, event_id=event_id)  # type: ignore[arg-type]


class _Trace:
    """One ordered record of persister, bus and handler calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bus = DomainEventBus()

        async def on(event) -> None:
            self.calls.append(("publish", type(event).__name__))

        for event_type in (ReservationClaimed, OrderFilled, CommandOutcomeNotice,
                           ReservationFailed, ReservationUnknown):
            self.bus.subscribe(event_type, on)

    async def persist(self, *events) -> None:
        self.calls.append(("persist", "+".join(type(event).__name__ for event in events)))

    async def handler(self, event: ReservationUnknown) -> None:
        self.calls.append(("uncertainty", type(event).__name__))


def _legacy(trace: _Trace) -> LegacyCommandEffects:
    return LegacyCommandEffects(trace, trace.bus, trace.handler)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_legacy_filled_ack_persists_the_fill_before_either_publish() -> None:
    trace = _Trace()
    await _legacy(trace).outcome_recorded(_facts(filled=True), _outcome("ack", "m-1"))
    assert trace.calls == [("persist", "OrderFilled"), ("publish", "ReservationClaimed"),
                           ("publish", "OrderFilled")]


@pytest.mark.asyncio
async def test_legacy_plain_ack_only_publishes_the_claim() -> None:
    trace = _Trace()
    await _legacy(trace).outcome_recorded(_facts(), _outcome("ack", "m-1"))
    assert trace.calls == [("publish", "ReservationClaimed")]


@pytest.mark.asyncio
async def test_legacy_unknown_opens_the_uncertainty_and_publishes_nothing() -> None:
    trace = _Trace()
    await _legacy(trace).outcome_recorded(_facts(), _outcome("unknown", None, "timeout"))
    assert trace.calls == [("uncertainty", "ReservationUnknown")]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rejected", "not_sent"])
async def test_legacy_failures_have_no_effect_after_the_journal(kind) -> None:
    trace = _Trace()
    await _legacy(trace).outcome_recorded(_facts(), _outcome(kind, None, "why"))
    assert trace.calls == []


@pytest.mark.asyncio
async def test_legacy_events_carry_the_stored_row_identity() -> None:
    trace = _Trace()
    event_id = uuid4()
    handled: list[ReservationUnknown] = []

    async def handler(event: ReservationUnknown) -> None:
        handled.append(event)

    effects = LegacyCommandEffects(trace, trace.bus, handler)  # type: ignore[arg-type]
    await effects.outcome_recorded(_facts(), _outcome("unknown", None, "timeout", event_id=event_id))
    assert [event.event_id for event in handled] == [event_id]
    assert handled[0].reason == "timeout" and handled[0].reservation_ref.cid == 7
    assert effects.new_event_id() != effects.new_event_id()


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "offer", "reason"), [
    ("ack", "m-1", None), ("unknown", None, "timeout"),
    ("rejected", None, "venue_rejected"), ("not_sent", None, "local_pre_transport"),
])
async def test_ledger_effects_only_announce_the_outcome(kind, offer, reason) -> None:
    trace = _Trace()
    effects = LedgerCommandEffects(trace.bus)
    assert effects.new_event_id() is None
    await effects.outcome_recorded(_facts(filled=kind == "ack"), _outcome(kind, offer, reason))
    assert trace.calls == [("publish", "CommandOutcomeNotice")]


@pytest.mark.asyncio
async def test_a_failing_bus_subscriber_never_fails_the_outcome() -> None:
    bus = DomainEventBus()

    async def boom(event) -> None:
        raise RuntimeError("subscriber")

    bus.subscribe(CommandOutcomeNotice, boom)
    await LedgerCommandEffects(bus).outcome_recorded(_facts(), _outcome("ack", "m-1"))
