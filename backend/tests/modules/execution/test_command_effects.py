"""What follows a durable command outcome: the ledger only announces it."""

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
from bfx_funding_bot.modules.ledger import CommandOutcome, Scope

SCOPE = Scope(uuid4(), "ci")
ATTEMPT = uuid4()
CORRELATION = uuid4()


def _facts() -> CommandFacts:
    return CommandFacts(
        scope=SCOPE, attempt_id=ATTEMPT, symbol="fUST", amount=Decimal("200.000005"),
        signal_correlation_id=CORRELATION,
        reference=ReservationRef("decision", CORRELATION, venue_offer_id="m-1"),
        offer_rate=Decimal("0.0001"), is_simulated=False,
    )


def _outcome(kind: str, offer: str | None = None, reason: str | None = None,
             *, event_id=None) -> CommandOutcome:
    return CommandOutcome(kind, offer, reason, 1100, {}, event_id=event_id)  # type: ignore[arg-type]


class _Trace:
    """One ordered record of every bus publication, whatever its type."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        trace = self

        class _Bus(DomainEventBus):
            async def publish(self, event: object) -> None:
                trace.calls.append(("publish", type(event).__name__))
                await super().publish(event)

        self.bus = _Bus()


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "offer", "reason"), [
    ("ack", "m-1", None), ("unknown", None, "timeout"),
    ("rejected", None, "venue_rejected"), ("not_sent", None, "local_pre_transport"),
])
async def test_ledger_effects_only_announce_the_outcome(kind, offer, reason) -> None:
    trace = _Trace()
    effects = LedgerCommandEffects(trace.bus)
    assert effects.new_event_id() is None
    await effects.outcome_recorded(_facts(), _outcome(kind, offer, reason))
    assert trace.calls == [("publish", "CommandOutcomeNotice")]


@pytest.mark.asyncio
async def test_a_failing_bus_subscriber_never_fails_the_outcome() -> None:
    bus = DomainEventBus()

    async def boom(event) -> None:
        raise RuntimeError("subscriber")

    bus.subscribe(CommandOutcomeNotice, boom)
    await LedgerCommandEffects(bus).outcome_recorded(_facts(), _outcome("ack", "m-1"))
