"""S1-3e3: the automatic UNKNOWN resolver inside the ledger observation cycle (PostgreSQL).

A fake ``VenueObservation`` feeds the real cycle (``build_observation_sink``): close
dangling -> committed query -> observe -> accept -> resolve, all through the ledger's own
write paths. Mutation map (pre-flight §F, numbers in the test docstrings) is checked by
``test_resolver_*`` / ``test_g1_*`` / ``test_g2_*`` below.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from bfx_funding_bot.modules.ledger import (
    RUNTIME_GRACE_MS,
    Attempt,
    Coverage,
    CycleResult,
    Observation,
    ObservationWindow,
    Offer,
    OfferHistory,
    Outcome,
    Quarantine,
    QuarantineMember,
    Resolution,
    ResolutionRejected,
    Scope,
    Wallet,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger._internal import resolver
from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload, record_attempt
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisRow,
    CapitalCommandClockRow,
    ExecutionResolutionJournalRow,
    LedgerObservationRow,
    SubmissionAttemptJournalRow,
)
from bfx_funding_bot.modules.ledger.wiring import build_ledger_journal, build_observation_sink

from .test_ledger_basis import _observation, _offer
from .test_ledger_capital_reader import (
    Book,
    _reason,
    _view,
    book,  # noqa: F401 - fixture re-export
)
from .test_ledger_schema_roles import _A, ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
SCOPE = Scope(UUID(_A), "ci")
JOURNAL = build_ledger_journal()
ATTEMPT_POLICY = UUID("00000000-0000-0000-0000-00000000b001")
STARTED = 1_000_000
UNKNOWN_AT = 1_050_000
CYCLE_1 = 1_200_000
CYCLE_2 = 1_300_000
FINGERPRINT = "100.0123"
WALLETS = (Wallet("funding", "UST", Decimal("1000"), Decimal("1000"), "fUST"),)


class Clock:
    def __init__(self, now: int = CYCLE_1) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def venue_offer(venue_id: str = "V1", *, amount: str = FINGERPRINT, created: int = STARTED + 200,
                **changes: Any) -> Offer:
    base = Offer(
        venue_id, "fUST", Decimal(amount), Decimal(amount), Decimal("0.0003"), True, 2, "LIMIT", 0,
        "active", created, created, {},
    )
    return replace(base, **changes)


class FakeVenue:
    """Complete account state; history range and symbols as the real adapter declares them."""

    def __init__(self, clock: Clock, *, offers: tuple[Offer, ...] = (),
                 past: tuple[OfferHistory, ...] = (), symbols: frozenset[str] | None = None,
                 history_complete: bool = True, on_observe: Any = None) -> None:
        self.clock = clock
        self.offers = offers
        self.past = past
        self.symbols = symbols
        self.history_complete = history_complete
        self.on_observe = on_observe
        self.windows: list[ObservationWindow] = []

    async def observe(self, scope: Scope, started: int, window: ObservationWindow):
        self.windows.append(window)
        if self.on_observe is not None:
            await self.on_observe()
        start = window.history_start_ms if window.history_start_ms is not None else started - 60_000
        end = started + 50
        created = [item.offer.mts_created for item in self.past]
        coverage = Coverage(
            wallets_complete=True, offers_complete=True, credits_complete=True,
            loans_complete=True, offer_history_complete=self.history_complete,
            credit_history_complete=True, wallet_pages=1, offer_pages=1, credit_pages=1,
            loan_pages=1, offer_history_pages=1, credit_history_pages=1,
            history_requested_start_ms=start, history_requested_end_ms=end,
            history_oldest_mts_created=min(created) if created else None,
            history_newest_mts_created=max(created) if created else None,
            trades_complete=True, trades_requested_start_ms=start, trades_requested_end_ms=end,
            history_symbols=(
                self.symbols if self.symbols is not None
                else frozenset({"fUST"}) | window.anchor_symbols
            ),
        )
        first = Observation(WALLETS, self.offers, (), coverage, started + 100, self.past)
        confirmation = Observation(WALLETS, self.offers, (), coverage, started + 300)
        self.clock.now = started + 400
        return first, confirmation, started + 200


def cycle(book_: Book, venue: FakeVenue, clock: Clock):
    return build_observation_sink(book_.factory, venue, now_ms=clock, grace_ms=RUNTIME_GRACE_MS)


async def run_cycle(book_: Book, venue: FakeVenue, clock: Clock, at: int) -> CycleResult:
    clock.now = at
    return await cycle(book_, venue, clock).run(SCOPE)


async def seed_unknown(
    book_: Book, amount: str = FINGERPRINT, *, symbol: str = "fUST", started: int = STARTED,
    unknown_at: int | None = UNKNOWN_AT, payload: dict[str, Any] | None = None,
    digest: str | None = None, ack_offer: str | None = None,
) -> UUID:
    """An attempt as ``authorize_command`` records it, then an UNKNOWN (or ack) outcome."""
    assert book_.basis_id is not None
    book_.decisions += 1
    decision_id = f"resolver-{book_.decisions}-{uuid4()}"
    attempt_id = uuid4()
    body = payload if payload is not None else {
        "type": "LIMIT", "symbol": symbol, "amount": amount, "rate": "0.0003", "period": 2,
        "flags": 0,
    }
    async with book_.factory.begin() as session:
        await session.execute(
            text(
                "INSERT INTO execution_decisions(decision_id, account_id, exchange_account_id, "
                "deployment_environment, reconcile_id, cell_id, symbol, signal_correlation_id, "
                "outcome, signal_rate, amount_usdt, duration_days, model_evidence, safety_result, "
                "execution_policy, service_version, config_hash, occurred_at_ms, recorded_at_ms) "
                "VALUES (:d, 'account', :a, 'ci', 'r', 'a30', :s, :d, 'submitted', 0, :amount, 2, "
                "'{}', '{}', 'policy', 'test', 'hash', 0, 0)"
            ),
            {"d": decision_id, "a": _A, "s": symbol, "amount": amount},
        )
        attempt = Attempt(attempt_id, decision_id, symbol, "a30", body, book_.basis_id,
                          ATTEMPT_POLICY, {}, started)
        if digest is None:
            await record_attempt(session, SCOPE, attempt)
        else:
            latest = await session.scalar(select(func.max(SubmissionAttemptJournalRow.attempt_seq)))
            session.add(SubmissionAttemptJournalRow(
                attempt_id=attempt_id, execution_decision_id=decision_id,
                exchange_account_id=SCOPE.exchange_account_id, deployment_environment="ci",
                symbol=symbol, cell_id="a30", attempt_seq=(latest or 0) + 1,
                normalized_payload=body, payload_sha256=digest, basis_id=book_.basis_id,
                policy_revision_id=ATTEMPT_POLICY, authorization_evidence={}, started_at_ms=started,
            ))
            await session.flush()
            await JOURNAL.bump_clock(session, SCOPE)
    if ack_offer is not None:
        outcome = Outcome(attempt_id, "ack", ack_offer, None, started + 10_000, {})
    elif unknown_at is not None:
        outcome = Outcome(attempt_id, "unknown", None, "test", unknown_at, {})
    else:
        return attempt_id
    async with book_.factory.begin() as session:
        await JOURNAL.record_outcome(session, SCOPE, outcome)
    return attempt_id


async def resolutions(book_: Book) -> list[ExecutionResolutionJournalRow]:
    async with book_.factory.begin() as session:
        return list(await session.scalars(select(ExecutionResolutionJournalRow)))


async def clock_revision(book_: Book) -> int:
    async with book_.factory.begin() as session:
        revision = await session.scalar(select(CapitalCommandClockRow.revision))
    assert revision is not None
    return revision


async def accepted_count(book_: Book) -> int:
    async with book_.factory.begin() as session:
        return int(await session.scalar(
            select(func.count()).select_from(LedgerObservationRow).where(
                LedgerObservationRow.accepted.is_(True))
        ) or 0)


async def classification(book_: Book, observation_id: UUID | None, attempt_id: UUID) -> str:
    async with book_.factory.begin() as session:
        basis = await session.scalar(select(AcceptedCapitalBasisRow.id).where(
            AcceptedCapitalBasisRow.observation_id == observation_id))
        value = await session.scalar(select(AcceptedCapitalBasisAttemptRow.classification).where(
            AcceptedCapitalBasisAttemptRow.basis_id == basis,
            AcceptedCapitalBasisAttemptRow.attempt_id == attempt_id))
    assert value is not None
    return value


@pytest.fixture
def alert_spy(monkeypatch):
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(resolver.alerts, "emit",
                        lambda event, **fields: seen.append((event, fields)))
    return seen


async def start(book_: Book, at: int = 900_000) -> None:
    """Policy plus the first accepted observation (its query began at ``at``)."""
    await book_.policy("fUST")
    assert await book_.accept(started=at, finished=at + 1, confirmed=at + 3) == "accepted"


# --- the happy paths ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_unknown_is_not_accepted_then_capital_frees_on_the_next_accept(book) -> None:  # noqa: F811
    """Mutation 12: the resolution names the observation just accepted (not the previous one)."""
    await start(book)
    previous = book.observation_id
    unknown = await seed_unknown(book)
    before = await clock_revision(book)
    blocked = await book.read(now=CYCLE_1, max_age=10**9)
    assert _reason(blocked) == "execution_unknown"

    clock = Clock()
    result = await run_cycle(book, FakeVenue(clock), clock, CYCLE_1)

    assert result.decision == "accepted" and result.observation_id not in (None, previous)
    (stored,) = await resolutions(book)
    assert result.resolutions == (stored.id,)
    assert (stored.attempt_id, stored.action, stored.venue_offer_id) == (unknown, "not_accepted", None)
    assert (stored.actor_kind, stored.actor_id) == ("system", "system:reconcile")
    assert stored.reason == "fingerprint_absent_from_complete_history"
    assert stored.observation_id == result.observation_id != previous
    assert stored.candidate_count == 0 and stored.resolved_at_ms == CYCLE_1 + 400
    assert stored.evidence == {
        "evidence_ref": observation_evidence_ref(result.observation_id),
        "query_started_at_ms": CYCLE_1, "query_finished_at_ms": CYCLE_1 + 100,
        "match_kind": "zero_match", "candidate_count": 0,
    }
    # The resolution bumped the clock once past the revision the basis was written at,
    # so tokens issued against that basis are stale (capital_snapshot_changed).
    assert await clock_revision(book) > before
    async with book.factory.begin() as session:
        basis_revision = await session.scalar(select(AcceptedCapitalBasisRow.accept_revision).where(
            AcceptedCapitalBasisRow.observation_id == result.observation_id))
    assert await clock_revision(book) == basis_revision + 1

    # The basis written before the resolution still lists it: blocked until the next accept.
    read = await book.read(now=CYCLE_2, max_age=10**9)
    assert _reason(read) == "execution_unknown"
    assert read.result.evidence == (("basis", "unresolved"),)  # type: ignore[union-attr]

    again = await run_cycle(book, FakeVenue(clock), clock, CYCLE_2)
    assert again.decision == "accepted" and again.resolutions == ()
    assert await classification(book, again.observation_id, unknown) == "settled"
    view = _view(await book.read(now=CYCLE_2 + 500, max_age=10**9))
    assert view.snapshot.unreflected_commitments == 0
    assert len(await resolutions(book)) == 1


@pytest.mark.asyncio
async def test_exact_match_binds_and_the_offer_is_reflected(book) -> None:  # noqa: F811
    await start(book)
    unknown = await seed_unknown(book)
    clock = Clock()
    venue = FakeVenue(clock, offers=(venue_offer("V1"),))
    result = await run_cycle(book, venue, clock, CYCLE_1)
    (stored,) = await resolutions(book)
    assert (stored.action, stored.venue_offer_id, stored.reason, stored.candidate_count) == (
        "bound_to_venue", "V1", "exact_fingerprint_match", 1)
    assert stored.evidence["venue_status"] == "active" and stored.evidence["venue_offer_id"] == "V1"
    assert stored.evidence["match_kind"] == "exact_match"
    assert result.resolutions == (stored.id,)
    assert _reason(await book.read(now=CYCLE_2, max_age=10**9)) == "execution_unknown"
    again = await run_cycle(book, FakeVenue(clock, offers=(venue_offer("V1"),)), clock, CYCLE_2)
    assert again.resolutions == ()
    assert await classification(book, again.observation_id, unknown) == "reflected"


@pytest.mark.asyncio
async def test_a_terminal_history_offer_binds_with_its_terminal_status(book) -> None:  # noqa: F811
    await start(book)
    await seed_unknown(book)
    clock = Clock()
    past = (OfferHistory(venue_offer("H1", status="active"), "executed", STARTED + 300),)
    await run_cycle(book, FakeVenue(clock, past=past), clock, CYCLE_1)
    (stored,) = await resolutions(book)
    assert (stored.action, stored.venue_offer_id) == ("bound_to_venue", "H1")
    assert stored.evidence["venue_status"] == "executed"


@pytest.mark.asyncio
async def test_prod_sequences_9502_9539_and_9849_9859_resolve_not_accepted_in_a_row(book) -> None:  # noqa: F811
    """Synthetic stand-ins (labels only): two consecutive fUST LIMIT UNKNOWNs, history complete.

    The legacy store resolved each as ``zero_match`` (not accepted); the ledger resolves
    them NOT_ACCEPTED too, and reads ``execution_unknown`` until the next acceptance. The
    composed processes run both sequences in test_unknown_sequences_e2e.py.
    """
    await start(book)
    clock = Clock()
    for index, amount in enumerate(("12.340001", "12.340002")):
        base = index * 1_000_000
        started, unknown_at = STARTED + base, UNKNOWN_AT + base
        cycle_at, next_at = CYCLE_1 + base, CYCLE_2 + base
        await seed_unknown(book, amount, started=started, unknown_at=unknown_at)
        assert _reason(await book.read(now=cycle_at, max_age=10**9)) == "execution_unknown"
        assert cycle_at >= started + 120_000 and cycle_at > unknown_at

        result = await run_cycle(book, FakeVenue(clock), clock, cycle_at)
        assert len(result.resolutions) == 1
        stored = [r for r in await resolutions(book) if r.id == result.resolutions[0]]
        assert [(r.action, r.actor_id) for r in stored] == [("not_accepted", "system:reconcile")]
        assert _reason(await book.read(now=next_at, max_age=10**9)) == "execution_unknown"
        again = await run_cycle(book, FakeVenue(clock), clock, next_at)
        assert again.resolutions == ()
        view = _view(await book.read(now=next_at + 500, max_age=10**9))
        assert view.snapshot.unreflected_commitments == 0
    assert len(await resolutions(book)) == 2


# --- what must stay open ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_skips_fenced_and_incomplete_observations(book, alert_spy) -> None:  # noqa: F811
    """Mutation 11: resolving on a fenced / incomplete observation."""
    await start(book)
    await seed_unknown(book)
    clock = Clock()

    async def move_the_clock() -> None:
        async with book.factory.begin() as session:
            await JOURNAL.bump_clock(session, SCOPE)

    fenced = await run_cycle(book, FakeVenue(clock, on_observe=move_the_clock), clock, CYCLE_1)
    assert fenced.decision == "fenced" and fenced.resolutions == ()
    incomplete = await run_cycle(
        book, FakeVenue(clock, history_complete=False), clock, CYCLE_2)
    assert incomplete.decision == "incomplete_or_unequal" and incomplete.resolutions == ()
    assert await resolutions(book) == [] and alert_spy == []  # the resolver never even tried


@pytest.mark.asyncio
async def test_resolver_leaves_open_a_candidate_some_attempt_already_owns(book) -> None:  # noqa: F811
    """Mutation 3: attribution (provenance) check removed."""
    await start(book)
    await seed_unknown(book)
    await seed_unknown(book, "77", ack_offer="V1")  # V1 is another attempt's offer
    clock = Clock()
    result = await run_cycle(book, FakeVenue(clock, offers=(venue_offer("V1"),)), clock, CYCLE_1)
    assert result.decision == "accepted" and result.resolutions == ()
    assert await resolutions(book) == []


@pytest.mark.asyncio
async def test_resolver_leaves_open_a_candidate_held_by_an_unresolved_quarantine(book) -> None:  # noqa: F811
    """Mutation 4: unresolved-quarantine member exclusion removed."""
    await book.policy("fUST")
    # V1 is already on the venue (foreign) in the first accepted observation, then an
    # unresolved quarantine takes it as a member; only afterwards does the UNKNOWN appear.
    held = _observation(offers=(_offer("V1", FINGERPRINT, created=STARTED + 200),))
    assert await book.accept(held, started=900_000, finished=900_001, confirmed=900_003) == "accepted"
    quarantine = uuid4()
    async with book.factory.begin() as session:
        await JOURNAL.open_quarantine(
            session, SCOPE, Quarantine(quarantine, "fUST", Decimal(FINGERPRINT), 10, {}))
        await JOURNAL.add_quarantine_member(session, SCOPE, QuarantineMember(
            quarantine, "offer", "V1", book.observation_id, Decimal(FINGERPRINT)))  # type: ignore[arg-type]
    await seed_unknown(book)
    clock = Clock()
    result = await run_cycle(book, FakeVenue(clock, offers=(venue_offer("V1"),)), clock, CYCLE_1)
    assert result.decision == "accepted" and result.resolutions == ()
    assert await resolutions(book) == []


@pytest.mark.asyncio
async def test_resolver_two_unknowns_sharing_a_candidate_both_stay_open(book) -> None:  # noqa: F811
    """Mutation 17: the second UNKNOWN must not bind the offer the first one matches."""
    await start(book)
    first = await seed_unknown(book)
    second = await seed_unknown(book)  # same terms: both match the one venue offer
    control = await seed_unknown(book, "55.5")  # nothing like it at the venue
    clock = Clock()
    result = await run_cycle(book, FakeVenue(clock, offers=(venue_offer("V1"),)), clock, CYCLE_1)
    stored = await resolutions(book)
    assert [(r.attempt_id, r.action) for r in stored] == [(control, "not_accepted")]
    assert first != second and result.resolutions == (stored[0].id,)


@pytest.mark.asyncio
async def test_resolver_multiple_matches_bind_nothing(book) -> None:  # noqa: F811
    """Mutation 9: ``multiple_match`` binding the first candidate."""
    await start(book)
    await seed_unknown(book)
    clock = Clock()
    offers = (venue_offer("V1"), venue_offer("V2"))
    result = await run_cycle(book, FakeVenue(clock, offers=offers), clock, CYCLE_1)
    assert result.decision == "accepted" and await resolutions(book) == []


@pytest.mark.asyncio
async def test_resolver_near_miss_blocks_the_absence_proof(book) -> None:  # noqa: F811
    """Mutation 2: ``amount_seen_since_start`` guard removed."""
    await start(book)
    await seed_unknown(book)
    clock = Clock()
    near = venue_offer("N1", rate=Decimal("0.0004"))  # same amount, another rate
    await run_cycle(book, FakeVenue(clock, offers=(near,)), clock, CYCLE_1)
    assert await resolutions(book) == []


@pytest.mark.asyncio
async def test_resolver_waits_for_the_settle_window(book) -> None:  # noqa: F811
    """Mutation 1: settle ``<`` -> ``<=`` / removed (the exact boundary is in the pure test)."""
    await start(book)
    await seed_unknown(book, started=CYCLE_1 - 119_999, unknown_at=CYCLE_1 - 119_000)
    clock = Clock()
    await run_cycle(book, FakeVenue(clock), clock, CYCLE_1)
    assert await resolutions(book) == []
    await run_cycle(book, FakeVenue(clock), clock, CYCLE_2)
    assert len(await resolutions(book)) == 1


@pytest.mark.asyncio
async def test_resolver_ignores_attempts_it_cannot_trust_as_subjects(book) -> None:  # noqa: F811
    """Mutation 14: payload digest recheck removed; seeded partial payloads stay open."""
    await start(book)
    forged = await seed_unknown(book, digest="0" * 64)
    seeded = await seed_unknown(book, "9.9", payload={"amount": "9.9", "symbol": "fUST"})
    genuine = await seed_unknown(book, "55.5")
    clock = Clock()
    await run_cycle(book, FakeVenue(clock), clock, CYCLE_1)
    assert [r.attempt_id for r in await resolutions(book)] == [genuine]
    assert forged != seeded
    assert canonical_payload({"a": 1})  # the digest scheme is the journal's own


# --- G1 / G2 ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g1_a_symbol_without_declared_history_is_not_falsely_not_accepted(book) -> None:  # noqa: F811
    """G1 / mutation 13: complete history with zero rows for an unfetched symbol proves nothing."""
    await start(book)
    await seed_unknown(book, symbol="fUSD")
    clock = Clock()
    # The old adapter fetched wallets + credits only: fUSD has neither, so no history was asked.
    await run_cycle(book, FakeVenue(clock, symbols=frozenset({"fUST"})), clock, CYCLE_1)
    assert await resolutions(book) == []
    # The extended adapter adds the window's anchor symbols; the same state now resolves.
    venue = FakeVenue(clock)
    await run_cycle(book, venue, clock, CYCLE_2)
    assert venue.windows[0].anchor_symbols == frozenset({"fUSD"})
    assert [r.action for r in await resolutions(book)] == ["not_accepted"]


@pytest.mark.asyncio
async def test_g2_the_observation_must_begin_after_the_unknown_not_the_submit(book) -> None:  # noqa: F811
    """G2: ``record_resolution`` opens an attempt at its UNKNOWN outcome, strictly."""
    await start(book, at=1)  # the accepted query began at 1
    at_opening = await seed_unknown(book, started=0, unknown_at=1)
    later = await seed_unknown(book, "5.5", started=0, unknown_at=0)

    def resolution(attempt_id: UUID) -> Resolution:
        assert book.observation_id is not None
        return Resolution(uuid4(), "fUST", "not_accepted", None, book.observation_id, "system",
                          "system:reconcile", 10, "r", {}, attempt_id=attempt_id)

    async with book.factory.begin() as session:
        with pytest.raises(ResolutionRejected, match="stale"):
            await JOURNAL.record_resolution(session, SCOPE, resolution(at_opening))
    async with book.factory.begin() as session:
        await JOURNAL.record_resolution(session, SCOPE, resolution(later))


@pytest.mark.asyncio
async def test_g2_a_dangling_attempt_closed_in_this_cycle_is_judged_by_the_next(book) -> None:  # noqa: F811
    await start(book)
    dangling = await seed_unknown(book, unknown_at=None)  # crashed mid-flight: no outcome
    clock = Clock()
    first = await run_cycle(book, FakeVenue(clock), clock, CYCLE_1)
    assert first.decision == "accepted" and first.resolutions == ()  # UNKNOWN == query start
    second = await run_cycle(book, FakeVenue(clock), clock, CYCLE_2)
    assert [r.attempt_id for r in await resolutions(book)] == [dangling]
    assert len(second.resolutions) == 1


# --- failure policy -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_journal_refusal_rolls_back_only_that_resolution(book, alert_spy, monkeypatch) -> None:  # noqa: F811
    """Mutation 15 (a): ``ResolutionRejected`` must not take the acceptance with it."""
    await start(book)
    await seed_unknown(book)
    before = await accepted_count(book)

    async def refuse(session, scope, resolution):
        raise ResolutionRejected("observation is stale")

    monkeypatch.setattr(resolver, "record_resolution", refuse)
    clock = Clock()
    result = await run_cycle(book, FakeVenue(clock), clock, CYCLE_1)
    assert result.decision == "accepted" and result.resolutions == ()
    assert await accepted_count(book) == before + 1
    assert await resolutions(book) == []
    assert [event for event, _ in alert_spy] == [resolver.RESOLUTION_REJECTED_ALERT]


@pytest.mark.asyncio
async def test_any_other_error_propagates_and_rolls_the_acceptance_back(book, monkeypatch) -> None:  # noqa: F811
    """Mutation 15 (b): swallowing every exception hides a bug and keeps a half cycle."""
    await start(book)
    await seed_unknown(book)
    before = await accepted_count(book)

    async def boom(session, scope, resolution):
        raise RuntimeError("bug")

    monkeypatch.setattr(resolver, "record_resolution", boom)
    clock = Clock()
    with pytest.raises(RuntimeError, match="bug"):
        await run_cycle(book, FakeVenue(clock), clock, CYCLE_1)
    assert await accepted_count(book) == before


@pytest.mark.asyncio
async def test_a_rejected_resolution_does_not_poison_the_others(book, alert_spy, monkeypatch) -> None:  # noqa: F811
    await start(book)
    refused = await seed_unknown(book)
    kept = await seed_unknown(book, "55.5")
    real = resolver.record_resolution

    async def refuse_first(session, scope, resolution):
        if resolution.attempt_id == refused:
            raise ResolutionRejected("no")
        await real(session, scope, resolution)

    monkeypatch.setattr(resolver, "record_resolution", refuse_first)
    clock = Clock()
    result = await run_cycle(book, FakeVenue(clock), clock, CYCLE_1)
    assert [r.attempt_id for r in await resolutions(book)] == [kept]
    assert len(result.resolutions) == 1 and len(alert_spy) == 1
