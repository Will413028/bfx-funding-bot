"""S1-3e5d on migrated PostgreSQL: the facade reads the cycle effects use, and the effects around
the real ledger cycle (``build_observation_sink``) with a fake venue.

Mutations (apply one at a time, run this file, revert):

* the positions read drops the basis's foreign offers (``cycle_reads.accepted_positions``):
  ``test_accepted_positions``, ``test_effects_around_the_real_cycle``.
* the foreign-offer read keeps a candidate of an open UNKNOWN (``cycle_reads.foreign_live_offers``):
  ``test_a_candidate_of_an_open_unknown_is_not_foreign``, ``test_effects_around_the_real_cycle``.
* the foreign-offer read lists a provenance conflict or a managed offer:
  ``test_foreign_live_offers``.
* effects before the cycle's commit, or twice per cycle: ``test_effects_around_the_real_cycle``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.execution.reconcile_monitors import (
    ForeignExposureMonitor,
    QuarantineAgeMonitor,
)
from bfx_funding_bot.modules.ledger import (
    RUNTIME_GRACE_MS,
    ForeignOffer,
    UnknownResolutionNotice,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_capital_authority,
    build_ledger_conservation_reader,
    build_ledger_cycle_reads,
    build_observation_sink,
    build_operator_reads,
)
from bfx_funding_bot.modules.observability import alerts

from .test_ledger_basis import _credit, _observation, _offer
from .test_ledger_capital_reader import book  # noqa: F401 - fixture re-export
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export
from .test_ledger_unknown_resolver_pg import (
    CYCLE_1,
    SCOPE,
    Clock,
    FakeVenue,
    seed_unknown,
    start,
    venue_offer,
)

pytestmark = pytest.mark.integration
READS = build_ledger_cycle_reads()
D = Decimal


async def seed_mixed_book(book_: Any) -> None:
    await book_.accept(_observation(usd=True))
    await book_.attempt("200", venue_offer_id="managed")
    await book_.attempt("5", venue_offer_id="mixed-up", symbol="fUSD")
    await book_.accept(_observation(
        "700", usd=True,
        offers=(
            _offer("managed", "200", "150"), _offer("manual", "300"),
            _offer("mixed-up", "5"),   # placed as fUSD, mirrored as fUST: contradictory
        ),
        credits=(_credit("c1", "40", opening=101_100),),
    ))


@pytest.mark.asyncio
async def test_accepted_positions(book) -> None:  # noqa: F811
    await seed_mixed_book(book)
    async with book.factory() as session, session.begin():
        positions = await READS.accepted_positions(session, SCOPE)
        operator = await build_operator_reads().list_positions(session, SCOPE)
    assert positions is not None and positions.observation_id == book.observation_id
    by_symbol = {p.symbol: p for p in positions.symbols}
    ust = by_symbol["fUST"]
    # Managed remaining 150 is offered; the manual 300 and the contradictory 5 are not ours.
    assert (ust.available, ust.offered, ust.credits, ust.n_credits) == (D("700"), D("150"),
                                                                       D("40"), 1)
    assert ust.foreign_offers == D("300")
    assert ust.n_offers == 3
    usd = by_symbol["fUSD"]
    assert (usd.available, usd.offered, usd.foreign_offers, usd.n_offers) == (D("5"), 0, 0, 0)
    # The operator console's view of the same basis agrees on what they share.
    shared = {v.symbol: (v.available, v.offered, v.lent, v.n_credits) for v in operator}
    assert shared == {p.symbol: (p.available, p.offered, p.credits, p.n_credits)
                      for p in positions.symbols}
    assert positions.accepted_at_ms == operator[0].last_reconciled_at_ms


@pytest.mark.asyncio
async def test_a_scope_without_a_basis_has_no_positions(book) -> None:  # noqa: F811
    async with book.factory() as session, session.begin():
        assert await READS.accepted_positions(session, SCOPE) is None
        found = await READS.foreign_live_offers(session, SCOPE)
    assert found.offers == () and found.live_offer_ids == frozenset()


@pytest.mark.asyncio
async def test_foreign_live_offers(book) -> None:  # noqa: F811
    await seed_mixed_book(book)
    async with book.factory() as session, session.begin():
        found = await READS.foreign_live_offers(session, SCOPE)
    assert found.offers == (ForeignOffer(
        "manual", "fUST", D("300"), D("300"), D("0.0001"), True, 2, 101_000, "active"),)
    # Every live offer is named, so the monitor forgets only offers that left the book.
    assert found.live_offer_ids == {"managed", "manual", "mixed-up"}


@pytest.mark.asyncio
async def test_a_candidate_of_an_open_unknown_is_not_foreign(book) -> None:  # noqa: F811
    await start(book)
    await seed_unknown(book)
    await seed_unknown(book)  # two UNKNOWNs share the one matching offer: both stay open
    clock = Clock()
    venue = FakeVenue(clock, offers=(venue_offer("V1"), venue_offer("V2", amount="77")))
    clock.now = CYCLE_1
    result = await build_observation_sink(book.factory, venue, now_ms=clock, grace_ms=RUNTIME_GRACE_MS).run(SCOPE)
    assert result.decision == "accepted" and result.resolutions == ()
    async with book.factory() as session, session.begin():
        found = await READS.foreign_live_offers(session, SCOPE)
    assert [offer.venue_offer_id for offer in found.offers] == ["V2"]
    assert found.live_offer_ids == {"V1", "V2"}


class _Protection:
    def __init__(self) -> None:
        self.trips: list[tuple[str, str]] = []
        self.clean: list[str | None] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.trips.append((trigger, detail))

    def observe_clean(self, evidence: str | None) -> None:
        self.clean.append(evidence)


@pytest.mark.asyncio
async def test_effects_around_the_real_cycle(book, monkeypatch) -> None:  # noqa: F811
    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: sent.append((event, fields)))
    await start(book)
    await seed_unknown(book)
    await seed_unknown(book)               # shares V1: both stay open, V1 is their candidate
    await seed_unknown(book, "55.5")   # nothing at the venue: resolved not_accepted
    clock = Clock()
    venue = FakeVenue(clock, offers=(venue_offer("V1"), venue_offer("V2", amount="77")))
    published: list[object] = []
    bus = DomainEventBus()

    async def on(event: object) -> None:
        published.append(event)

    bus.subscribe(PositionReconciled, on)
    bus.subscribe(UnknownResolutionNotice, on)
    protection = _Protection()
    sink = LedgerCycleEffects(
        build_observation_sink(book.factory, venue, now_ms=clock, grace_ms=RUNTIME_GRACE_MS), scope=SCOPE,
        account_id="acct", session_factory=book.factory,
        capital=build_capital_authority(book.factory, max_snapshot_age_ms=10**9),
        cells=(("fUST", "a30"),), protection=protection, bus=bus, reads=READS,
        conservation=build_ledger_conservation_reader(), operator_reads=build_operator_reads(),
        foreign_exposure=ForeignExposureMonitor(), quarantine_age=QuarantineAgeMonitor(),
        foreign_grace_ms=0, clock=lambda: 5_000_000,
    )
    clock.now = CYCLE_1
    result = await sink.run(SCOPE)
    assert result.decision == "accepted" and len(result.resolutions) == 1

    # Protection: the open UNKNOWNs block spending, which is transient, so nothing trips and
    # the accepted observation is one clean observation named by its ledger reference.
    assert protection.trips == []
    assert protection.clean == [observation_evidence_ref(result.observation_id)]
    # NAV: one signal for the symbol, from the basis; both venue offers are the account's.
    (nav,) = [e for e in published if isinstance(e, PositionReconciled)]
    assert (nav.symbol, nav.account_id, nav.n_offers) == ("fUST", "acct", 2)
    assert nav.reserved == D("100.0123") + D("77") and nav.available == D("1000")
    # The resolution is announced once, after commit.
    assert [e for e in published if isinstance(e, UnknownResolutionNotice)] == [
        UnknownResolutionNotice(SCOPE, result.observation_id, result.resolutions[0])]
    # Foreign exposure: V2 only; V1 may be the offer one open UNKNOWN sent.
    assert [f["venue_offer_id"] for e, f in sent if e == alerts.FOREIGN_EXPOSURE] == ["V2"]
    # Quarantine age: the two open UNKNOWNs began an hour earlier by the effects' clock.
    assert [f["symbol"] for e, f in sent if e == alerts.UNKNOWN_QUARANTINE_AGED] == ["fUST"] * 2
    assert {e for e, _ in sent} == {alerts.FOREIGN_EXPOSURE, alerts.UNKNOWN_QUARANTINE_AGED}

    await sink.run(SCOPE)                  # the next cycle: V2 is not alerted twice
    assert [f["venue_offer_id"] for e, f in sent if e == alerts.FOREIGN_EXPOSURE] == ["V2"]
    assert len([e for e, _ in sent if e == alerts.UNKNOWN_QUARANTINE_AGED]) == 2   # repeat window
    assert len([e for e in published if isinstance(e, PositionReconciled)]) == 2
