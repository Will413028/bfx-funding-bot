"""S1-3e5d: what follows a ledger observation cycle, against a fake inner sink and fake ports.

Mutations (apply one at a time, run this file, revert):

1. run the effects before ``inner.run`` returns, or when it raised (``LedgerCycleEffects.run``):
   ``test_effects_run_after_the_cycle_returned``, ``test_a_raised_cycle_runs_no_effect``.
2. trip on a transient reason (drop ``capital_block_trigger``'s None):
   ``test_a_transient_refusal_does_not_trip``.
3. ``observe_clean`` / trip on a cycle that was not accepted (the ``decision == "accepted"``
   guard): ``test_a_cycle_that_was_not_accepted_neither_trips_nor_counts_clean``.
4. ``observe_clean`` despite a trigger-class refusal (the ``tripped`` guard):
   ``test_a_trigger_class_refusal_trips_once_and_is_never_clean``.
5. one observation counted clean twice (``AutomaticProtection.observe_clean`` dedup; here the
   effects' evidence reference): ``test_the_same_observation_is_clean_once``, and
   ``tests/modules/execution/safety/test_protection.py``.
6. NAV ``reserved`` without the foreign offers: ``test_nav_reserved_includes_foreign_offers``.
7. NAV published twice per accepted cycle: ``test_nav_is_published_once_per_accepted_cycle``.
8. a foreign-exposure alert on an UNKNOWN candidate: the facade read
   (``tests/integration/test_ledger_cycle_effects.py``).
9. ``foreign_lending`` alerted again for the same basis:
   ``test_foreign_lending_is_alerted_once_per_basis``.
10. effects wrapping the legacy sink: ``test_the_legacy_sink_is_never_wrapped``.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.execution import ledger_cycle_effects
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.execution.observation_sink import (
    LegacyCycleResult,
    LegacyObservationSink,
)
from bfx_funding_bot.modules.execution.reconcile_monitors import (
    ForeignExposureMonitor,
    QuarantineAgeMonitor,
)
from bfx_funding_bot.modules.execution.safety.protection import (
    FOREIGN_LENDING,
    VENUE_LENT_ABOVE_LEDGER,
    AutomaticProtection,
)
from bfx_funding_bot.modules.ledger import (
    AcceptedConservation,
    AcceptedPositions,
    AcceptedSymbolPosition,
    CapitalBlocked,
    CycleResult,
    ForeignOffer,
    ForeignOffers,
    Scope,
    SymbolConservation,
    UncertaintyView,
    UnknownResolutionNotice,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.observability import alerts
from tests.modules.execution.safety.test_protection import MIN_HALT, Clock, RuleState

SCOPE = Scope(uuid4(), "ci")
OBS = uuid4()
D = Decimal


class _Trace:
    def __init__(self) -> None:
        self.calls: list[str] = []


class _Inner:
    def __init__(self, trace: _Trace, result: CycleResult | Exception) -> None:
        self.trace, self.result = trace, result

    async def run(self, scope: Scope) -> CycleResult:
        self.trace.calls.append("inner_start")
        if isinstance(self.result, Exception):
            raise self.result
        self.trace.calls.append("inner_done")
        return self.result


class _Session:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def begin(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _Factory:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def __call__(self) -> _Session:
        return _Session(self.trace)


class _Capital:
    def __init__(self, trace: _Trace, refusals: dict[tuple[str, str], str] | None = None) -> None:
        self.trace, self.refusals = trace, refusals or {}
        self.reads: list[tuple[str, str]] = []

    async def read(self, scope, *, now_ms, session=None):
        self.trace.calls.append("capital")
        assert session is None  # the cycle's transaction is over: the authority's own session
        self.reads.append((scope.symbol, scope.cell_id))
        reason = self.refusals.get((scope.symbol, scope.cell_id))
        if reason == "RAISE":
            raise RuntimeError("database unavailable")
        return CapitalBlocked(reason) if reason else SimpleNamespace(available=True)


class _Protection:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace
        self.trips: list[tuple[str, str]] = []
        self.clean: list[str | None] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.trace.calls.append("trip")
        self.trips.append((trigger, detail))

    def observe_clean(self, evidence: str | None) -> None:
        self.trace.calls.append("clean")
        self.clean.append(evidence)


class _Reads:
    def __init__(self, trace: _Trace, positions: AcceptedPositions | None,
                 foreign: ForeignOffers | None = None) -> None:
        self.trace, self.positions = trace, positions
        self.foreign = foreign or ForeignOffers((), frozenset())

    async def accepted_positions(self, session, scope):
        self.trace.calls.append("positions")
        return self.positions

    async def foreign_live_offers(self, session, scope):
        self.trace.calls.append("foreign")
        return self.foreign


class _Conservation:
    def __init__(self, trace: _Trace, latest: AcceptedConservation | None) -> None:
        self.trace, self.value = trace, latest

    async def latest(self, session, scope):
        self.trace.calls.append("conservation")
        return self.value


class _Operator:
    def __init__(self, trace: _Trace, views: tuple[UncertaintyView, ...] = ()) -> None:
        self.trace, self.views = trace, views

    async def list_uncertainties(self, session, scope, *, state, limit):
        assert state == "open"
        self.trace.calls.append("uncertainties")
        return self.views


def position(symbol: str = "fUST", **changes) -> AcceptedSymbolPosition:
    values = {"available": D("500"), "offered": D("100"), "foreign_offers": D("30"),
              "credits": D("250"), "n_credits": 2, "n_offers": 3}
    values.update(changes)
    return AcceptedSymbolPosition(symbol, **values)


def positions(*symbols: AcceptedSymbolPosition, observation_id: UUID = OBS) -> AcceptedPositions:
    return AcceptedPositions(observation_id, 1_234, symbols or (position(),))


class Rig:
    def __init__(self, *, result: CycleResult | Exception | None = None,
                 refusals: dict[tuple[str, str], str] | None = None,
                 cells: tuple[tuple[str, str], ...] = (("fUST", "a30"),),
                 book: AcceptedPositions | None | str = "default",
                 foreign: ForeignOffers | None = None,
                 conservation: AcceptedConservation | None = None,
                 views: tuple[UncertaintyView, ...] = (), now: int = 10_000_000,
                 protection=None) -> None:
        self.trace = _Trace()
        self.result = result if result is not None else CycleResult("accepted", OBS)
        self.inner = _Inner(self.trace, self.result)
        self.capital = _Capital(self.trace, refusals)
        self.protection = protection or _Protection(self.trace)
        self.bus = DomainEventBus()
        self.published: list[object] = []

        async def on(event) -> None:
            self.trace.calls.append(f"publish:{type(event).__name__}")
            self.published.append(event)

        self.bus.subscribe(PositionReconciled, on)
        self.bus.subscribe(UnknownResolutionNotice, on)
        self.reads = _Reads(self.trace, positions() if book == "default" else book, foreign)
        self.clock = Clock(now)
        self.sink = LedgerCycleEffects(
            self.inner, scope=SCOPE, account_id="acct-1", session_factory=_Factory(self.trace),
            capital=self.capital, cells=cells, protection=self.protection, bus=self.bus,
            reads=self.reads, conservation=_Conservation(
                self.trace, conservation or verdicts(uuid4(), ("fUST", "conserved"))),
            operator_reads=_Operator(self.trace, views),
            foreign_exposure=ForeignExposureMonitor(), quarantine_age=QuarantineAgeMonitor(),
            foreign_grace_ms=60_000, clock=self.clock,
        )

    async def run(self) -> CycleResult:
        return await self.sink.run(SCOPE)

    @property
    def nav(self) -> list[PositionReconciled]:
        return [e for e in self.published if isinstance(e, PositionReconciled)]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(ledger_cycle_effects.alerts, "emit",
                        lambda event, **fields: captured.append((event, fields)))
    return captured


# ----------------------------------------------------------------------------- ordering


async def test_effects_run_after_the_cycle_returned() -> None:
    rig = Rig()
    result = await rig.run()
    assert result is rig.result  # the cycle's own result, untouched
    calls = rig.trace.calls
    assert calls[:2] == ["inner_start", "inner_done"]
    assert set(calls[2:]) >= {"capital", "clean", "positions", "publish:PositionReconciled"}


@pytest.mark.parametrize("failure", [RuntimeError("venue down"), ValueError("scope")])
async def test_a_raised_cycle_runs_no_effect(failure: Exception) -> None:
    rig = Rig(result=failure)
    with pytest.raises(type(failure)):
        await rig.run()
    assert rig.trace.calls == ["inner_start"]


async def test_an_effect_that_fails_neither_fails_the_cycle_nor_stops_the_others(sent) -> None:
    rig = Rig(refusals={("fUST", "a30"): "RAISE"})
    result = await rig.run()
    assert result is rig.result
    assert rig.protection.clean == [] and rig.protection.trips == []   # not judged: not clean
    assert len(rig.nav) == 1                                           # NAV still published


# ----------------------------------------------------------------------------- protection


@pytest.mark.parametrize("reason", ["attempt_tail_unbounded", "snapshot_query_pending",
                                    "execution_unknown", "policy_disabled"])
async def test_a_transient_refusal_does_not_trip(reason: str) -> None:
    rig = Rig(refusals={("fUST", "a30"): reason})
    await rig.run()
    assert rig.protection.trips == []


@pytest.mark.parametrize(("reason", "trigger"), [
    ("venue_lent_above_ledger", VENUE_LENT_ABOVE_LEDGER),
    ("offer_provenance_conflict", "identity_conflict"),
    ("offer_amount_conflict", "offer_amount_mismatch"),
    ("unclassifiable_commitment", "unclassifiable_commitment"),
])
async def test_a_trigger_class_refusal_trips_once_and_is_never_clean(reason, trigger) -> None:
    rig = Rig(cells=(("fUST", "a30"), ("fUST", "a60"), ("fUSD", "a30")),
              refusals={("fUST", "a30"): reason, ("fUST", "a60"): reason})
    await rig.run()
    assert [t for t, _ in rig.protection.trips] == [trigger]   # one per (symbol, reason)
    assert "fUST/a30" in rig.protection.trips[0][1] and reason in rig.protection.trips[0][1]
    assert rig.protection.clean == []
    assert rig.capital.reads == [("fUST", "a30"), ("fUST", "a60"), ("fUSD", "a30")]


async def test_each_symbol_trips_for_itself() -> None:
    rig = Rig(cells=(("fUST", "a30"), ("fUSD", "a30")),
              refusals={("fUST", "a30"): "venue_lent_above_ledger",
                        ("fUSD", "a30"): "venue_lent_above_ledger"})
    await rig.run()
    assert len(rig.protection.trips) == 2


async def test_an_accepted_cycle_with_no_trigger_is_one_clean_observation() -> None:
    rig = Rig()
    await rig.run()
    assert rig.protection.trips == []
    assert rig.protection.clean == [observation_evidence_ref(OBS)]


@pytest.mark.parametrize("decision", ["fenced", "incomplete_or_unequal", "query_admission_refused"])
async def test_a_cycle_that_was_not_accepted_neither_trips_nor_counts_clean(decision) -> None:
    rig = Rig(result=CycleResult(decision),
              refusals={("fUST", "a30"): "venue_lent_above_ledger"})
    await rig.run()
    assert rig.protection.trips == [] and rig.protection.clean == []
    assert rig.capital.reads == [] and rig.nav == []


async def test_the_same_observation_is_clean_once() -> None:
    """Through the real protection: the auto-resume rule counts distinct observations."""
    clock = Clock(1_000)
    state = RuleState(clock)
    await state.transition("ACTIVE", cause="operator", actor="will", reason="start")
    protection = AutomaticProtection(clock=clock)
    protection.bind(state)
    protection.trip(VENUE_LENT_ABOVE_LEDGER, "unexplained=1")
    await protection.run_pending()
    rig = Rig(protection=protection)
    for _ in range(4):                       # one accepted cycle, reported four times
        clock.now += 60_000
        await rig.run()
    clock.now = state.rows[-1].created_at_ms + MIN_HALT
    assert not await protection.resume_if_cleared()
    for observation in (uuid4(), uuid4()):   # two more, distinct, accepted cycles
        rig.inner.result = CycleResult("accepted", observation)
        rig.reads.positions = positions(observation_id=observation)
        clock.now += 60_000
        await rig.run()
    assert await protection.resume_if_cleared()


# ----------------------------------------------------------------------------- NAV


async def test_nav_reserved_includes_foreign_offers() -> None:
    rig = Rig()
    await rig.run()
    (event,) = rig.nav
    assert (event.symbol, event.account_id) == ("fUST", "acct-1")
    assert (event.available, event.reserved, event.realized) == (D("500"), D("130"), D("250"))
    assert (event.n_offers, event.n_credits, event.occurred_at_ms) == (3, 2, 1_234)


async def test_nav_is_published_once_per_accepted_cycle() -> None:
    rig = Rig(cells=(("fUST", "a30"), ("fUST", "a60")),
              book=positions(position("fUSD"), position("fUST")))
    await rig.run()
    assert [e.symbol for e in rig.nav] == ["fUSD", "fUST"]        # each symbol once, not per cell
    await rig.run()
    assert len(rig.nav) == 4


async def test_nav_of_another_basis_is_not_published() -> None:
    rig = Rig(book=positions(observation_id=uuid4()))
    await rig.run()
    assert rig.nav == []


async def test_a_scope_without_a_basis_publishes_no_nav() -> None:
    rig = Rig(book=None)
    await rig.run()
    assert rig.nav == []


# ----------------------------------------------------------------------------- resolutions


async def test_each_resolution_is_announced_after_the_cycle_returned() -> None:
    first, second = uuid4(), uuid4()
    rig = Rig(result=CycleResult("accepted", OBS, (first, second)))
    await rig.run()
    notices = [e for e in rig.published if isinstance(e, UnknownResolutionNotice)]
    assert notices == [UnknownResolutionNotice(SCOPE, OBS, first),
                       UnknownResolutionNotice(SCOPE, OBS, second)]
    assert rig.trace.calls.index("publish:UnknownResolutionNotice") > rig.trace.calls.index(
        "inner_done")


async def test_a_cycle_without_resolutions_announces_none() -> None:
    rig = Rig()
    await rig.run()
    assert not [e for e in rig.published if isinstance(e, UnknownResolutionNotice)]


# ----------------------------------------------------------------------------- alerts


def verdicts(basis_id: UUID, *symbols: tuple[str, str]) -> AcceptedConservation:
    return AcceptedConservation(basis_id, uuid4(), tuple(
        SymbolConservation(symbol, kind, D("5"), D("5") if kind == "foreign_lending" else D("0"), 0)
        for symbol, kind in symbols
    ))


async def test_foreign_lending_is_alerted_once_per_basis(sent) -> None:
    basis = uuid4()
    rig = Rig(conservation=verdicts(basis, ("fUST", "foreign_lending"), ("fUSD", "conserved")))
    await rig.run()
    await rig.run()
    rig.result = rig.inner.result = CycleResult("fenced")     # reads the same latest basis
    await rig.run()
    assert [(e, f["detail"]) for e, f in sent if e == FOREIGN_LENDING] == [
        (FOREIGN_LENDING, "symbol=fUST lent_unexplained=5 foreign_executed=5")]
    rig.sink._conservation = _Conservation(rig.trace, verdicts(uuid4(), ("fUST", "foreign_lending")))
    await rig.run()
    assert len([1 for e, _ in sent if e == FOREIGN_LENDING]) == 2   # a new basis is news


@pytest.mark.parametrize("kind", ["conserved", "foreign_lending", "baseline"])
async def test_a_verdict_other_than_unexplained_does_not_trip(kind) -> None:
    rig = Rig(conservation=verdicts(uuid4(), ("fUSD", kind)))
    await rig.run()
    assert rig.protection.trips == []
    assert rig.protection.clean == [observation_evidence_ref(OBS)]


async def test_unexplained_lending_on_a_symbol_without_a_cell_trips_once_and_is_not_clean() -> None:
    rig = Rig(cells=(("fUST", "a30"),),
              conservation=verdicts(uuid4(), ("fUST", "conserved"), ("fUSD", "unexplained_lending")))
    await rig.run()
    assert [t for t, _ in rig.protection.trips] == [VENUE_LENT_ABOVE_LEDGER]
    assert "symbol=fUSD" in rig.protection.trips[0][1]
    assert rig.protection.clean == []


async def test_unexplained_lending_on_a_configured_symbol_trips_exactly_once() -> None:
    rig = Rig(cells=(("fUST", "a30"), ("fUST", "a60")),
              conservation=verdicts(uuid4(), ("fUST", "unexplained_lending")),
              refusals={("fUST", "a30"): "venue_lent_above_ledger",
                        ("fUST", "a60"): "venue_lent_above_ledger"})
    await rig.run()
    assert [t for t, _ in rig.protection.trips] == [VENUE_LENT_ABOVE_LEDGER]
    assert rig.protection.clean == []


async def test_a_conservation_verdict_of_another_basis_is_not_judged_and_not_clean() -> None:
    rig = Rig(book=positions(observation_id=uuid4()))
    await rig.run()
    assert rig.protection.trips == [] and rig.protection.clean == []


async def test_unexplained_lending_is_a_trip_not_an_alert(sent) -> None:
    rig = Rig(conservation=verdicts(uuid4(), ("fUST", "unexplained_lending")),
              refusals={("fUST", "a30"): "venue_lent_above_ledger"})
    await rig.run()
    assert [e for e, _ in sent if e == FOREIGN_LENDING] == []
    assert [t for t, _ in rig.protection.trips] == [VENUE_LENT_ABOVE_LEDGER]


def foreign_offer(venue_id: str, *, created: int, unobserved: bool = False) -> ForeignOffer:
    rate = None if unobserved else D("0.0003")
    return ForeignOffer(venue_id, "fUST", D("40"), D("50"), rate, rate is not None, 2, created,
                        "ACTIVE")


async def test_foreign_exposure_is_alerted_once_per_offer_after_its_grace(sent) -> None:
    now = 10_000_000
    found = ForeignOffers(
        (foreign_offer("old", created=now - 60_000), foreign_offer("young", created=now - 59_999),
         foreign_offer("norate", created=0, unobserved=True)),
        frozenset({"old", "young", "norate", "ours"}),
    )
    rig = Rig(foreign=found, now=now)
    await rig.run()
    await rig.run()
    exposures = [f for e, f in sent if e == alerts.FOREIGN_EXPOSURE]
    assert [f["venue_offer_id"] for f in exposures] == ["old", "norate"]
    assert exposures[0]["amount"] == D("40") and exposures[0]["amount_original"] == D("50")
    rig.clock.now += 1
    await rig.run()
    assert [f["venue_offer_id"] for f in sent_exposures(sent)] == ["old", "norate", "young"]


def sent_exposures(sent):
    return [f for e, f in sent if e == alerts.FOREIGN_EXPOSURE]


def uncertainty(opened: int, *, symbol: str = "fUST") -> UncertaintyView:
    return UncertaintyView(uuid4(), "execution_unknown", symbol, "open", D("199.9"), {},
                           attempt_id=uuid4(), opened_at_ms=opened)


async def test_an_open_uncertainty_is_reported_when_aged(sent) -> None:
    now = 10_000_000
    aged, fresh = uncertainty(now - 31 * 60_000), uncertainty(now - 60_000, symbol="fUSD")
    rig = Rig(views=(aged, fresh), now=now)
    await rig.run()
    await rig.run()                       # within the repeat window
    (alert,) = [f for e, f in sent if e == alerts.UNKNOWN_QUARANTINE_AGED]
    assert (alert["symbol"], alert["minutes"], alert["attempt_id"]) == (
        "fUST", 31, str(aged.uncertainty_id))


async def test_alerts_also_run_for_a_cycle_that_was_not_accepted(sent) -> None:
    now = 10_000_000
    rig = Rig(result=CycleResult("fenced"), views=(uncertainty(now - 40 * 60_000),), now=now)
    await rig.run()
    assert [e for e, _ in sent] == [alerts.UNKNOWN_QUARANTINE_AGED]


# ----------------------------------------------------------------------------- the legacy sink


async def test_the_legacy_sink_is_never_wrapped() -> None:
    """The legacy reconcile publishes ``PositionReconciled`` itself: wrapping it doubles it."""
    rig = Rig()
    legacy = LegacyObservationSink(SimpleNamespace(), SCOPE)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ledger"):
        LedgerCycleEffects(
            legacy, scope=SCOPE, account_id="a", session_factory=rig.sink._sf,
            capital=rig.capital, cells=(), protection=rig.protection, bus=rig.bus,
            reads=rig.reads, conservation=rig.sink._conservation,
            operator_reads=rig.sink._operator_reads, foreign_exposure=ForeignExposureMonitor(),
            quarantine_age=QuarantineAgeMonitor(), foreign_grace_ms=0, clock=rig.clock,
        )


async def test_a_legacy_result_through_any_wrapper_is_refused() -> None:
    rig = Rig(result=LegacyCycleResult("accepted", legacy=SimpleNamespace()))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ledger"):
        await rig.run()
    assert rig.nav == [] and rig.protection.clean == []
