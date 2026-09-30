"""S1-3b1 ledger reads on a clone PostgreSQL database.

Driven only through the ledger's own write paths (``Book`` from the reader
tests). Reads are keyed by attempt_id / venue_offer_id; no CID anywhere.

Mutations (apply one at a time, run this file, revert):

1. ``open_uncertainties`` ignores resolutions:
   ``test_open_unknown_is_listed_until_resolved``.
2. managed offers include terminal mirror rows (drop the present filter):
   ``test_absent_and_terminal_offers_are_not_live``.
3. contradictory provenance picks the first attempt:
   ``test_contradictory_provenance_is_surfaced_and_excluded``.
4. fingerprints omit open UNKNOWN: ``test_fingerprints_hold_until_settled``.
5. fingerprints include rejected attempts: ``test_fingerprints_hold_until_settled``.
6. the facade drops ``accept``: ``tests/modules/ledger/test_facade.py`` and every
   acceptance here.
7. reads load all mirror rows (present filter applied in Python):
   ``test_read_work_does_not_grow_with_history``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import event, text

from bfx_funding_bot.modules.ledger import (
    CancelProvenance,
    LedgerReadUnbounded,
    ManagedOffer,
    ManagedOffers,
    OfferHistory,
    OpenUncertainty,
    ProvenanceConflict,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal import reads
from bfx_funding_bot.modules.ledger.wiring import (
    build_ledger_managed_offers,
    build_ledger_uncertainties,
)

from .test_ledger_basis import _ledger_mappers, _observation, _offer
from .test_ledger_capital_reader import SCOPE, Book
from .test_ledger_capital_reader import book as book
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
UNCERTAINTIES = build_ledger_uncertainties()
OFFERS = build_ledger_managed_offers()


@pytest_asyncio.fixture
async def ledger(book):
    await book.accept()
    return book


async def _open(book: Book, symbol: str | None = None) -> tuple[OpenUncertainty, ...]:
    async with book.factory() as session, session.begin():
        return await UNCERTAINTIES.open_uncertainties(session, book.scope, symbol)


async def _managed(book: Book, symbols: Any = None) -> ManagedOffers:
    async with book.factory() as session, session.begin():
        return await OFFERS.managed_live_offers(session, book.scope, symbols)


async def _cancel(book: Book, venue_offer_id: str) -> CancelProvenance | None:
    async with book.factory() as session, session.begin():
        return await OFFERS.cancel_provenance(session, book.scope, venue_offer_id)


async def _fingerprints(book: Book, symbol: str = "fUST") -> frozenset[Decimal]:
    async with book.factory() as session, session.begin():
        return await OFFERS.fingerprints_in_use(session, book.scope, symbol)


async def _decision(book: Book, attempt_id: UUID, correlation: str) -> str:
    """The attempt's decision id; give its decision a distinct signal correlation."""
    async with book.factory.begin() as session:
        decision_id: str = await session.scalar(
            text(
                "SELECT execution_decision_id FROM submission_attempt_journal WHERE attempt_id=:a"
            ),
            {"a": attempt_id},
        )
        await session.execute(
            text("UPDATE execution_decisions SET signal_correlation_id=:c WHERE decision_id=:d"),
            {"c": correlation, "d": decision_id},
        )
    return decision_id


def _live(*offers: Any, available: str = "800", **kwargs: Any) -> Any:
    return _observation(available, offers=offers, **kwargs)


# --- open uncertainties -------------------------------------------------------


@pytest.mark.asyncio
async def test_open_unknown_is_listed_until_resolved(ledger) -> None:
    unknown = await ledger.attempt("200", outcome="unknown")
    await ledger.attempt("60", outcome="ack", venue_offer_id="o-1")
    await ledger.attempt("70", outcome="rejected")
    expected = (OpenUncertainty("attempt", unknown, "fUST", Decimal("200")),)
    assert await _open(ledger) == expected
    # Carried by a basis as unresolved: still open.
    await ledger.accept()
    assert await _open(ledger) == expected
    # A resolution closes it at once (no new basis needed for this read).
    await ledger.resolve("not_accepted", attempt_id=unknown)
    assert await _open(ledger) == ()
    await ledger.accept()
    await ledger.attempt("50", outcome=None)  # in flight: not an uncertainty
    assert await _open(ledger) == ()


@pytest.mark.asyncio
async def test_bound_unknown_is_not_open(ledger) -> None:
    unknown = await ledger.attempt("200", outcome="unknown")
    await ledger.resolve("bound_to_venue", attempt_id=unknown, venue_offer_id="o-9")
    assert await _open(ledger) == ()


@pytest.mark.asyncio
async def test_quarantine_is_listed_until_resolved(ledger) -> None:
    quarantine = await ledger.quarantine()
    expected = (OpenUncertainty("quarantine", quarantine, "fUST", Decimal("10")),)
    assert await _open(ledger) == expected
    await ledger.accept()  # now listed by the basis
    assert await _open(ledger) == expected
    await ledger.resolve("manual", quarantine_id=quarantine)
    assert await _open(ledger) == ()


@pytest.mark.asyncio
async def test_symbol_filter(book) -> None:
    await book.accept(_observation(usd=True))
    unknown = await book.attempt("200", outcome="unknown")
    quarantine = await book.quarantine("fUSD")
    usd_unknown = await book.attempt("5", outcome="unknown", symbol="fUSD")
    assert await _open(book, "fUST") == (
        OpenUncertainty("attempt", unknown, "fUST", Decimal("200")),
    )
    assert await _open(book, "fUSD") == (
        OpenUncertainty("attempt", usd_unknown, "fUSD", Decimal("5")),
        OpenUncertainty("quarantine", quarantine, "fUSD", Decimal("10")),
    )
    assert {item.subject_id for item in await _open(book)} == {unknown, quarantine, usd_unknown}
    assert await _open(book, "fEUR") == ()


@pytest.mark.asyncio
async def test_scopes_are_isolated(ledger) -> None:
    other = Book(ledger.factory, Scope(SCOPE.exchange_account_id, "ci2"))
    await other.accept()
    await ledger.attempt("200", outcome="unknown")
    await ledger.quarantine()
    await ledger.attempt("300", venue_offer_id="shared-id")
    await ledger.accept(_live(_offer("shared-id", "300"), available="700"))
    await other.accept(_live(_offer("shared-id", "300"), available="700"))
    assert await _open(other) == ()
    assert len(await _open(ledger)) == 2
    # The other scope sees the same venue id as foreign: provenance is per scope.
    assert await _managed(other) == ManagedOffers((), ())
    assert await _fingerprints(other) == frozenset()
    assert [o.venue_offer_id for o in (await _managed(ledger)).offers] == ["shared-id"]


@pytest.mark.asyncio
async def test_tail_longer_than_the_cap_fails_closed(ledger, monkeypatch) -> None:
    monkeypatch.setattr(reads, "MAX_TAIL_ATTEMPTS", 2)
    for _ in range(2):
        await ledger.attempt("10", outcome="rejected")
    assert await _open(ledger) == ()
    await ledger.attempt("10", outcome="rejected")
    with pytest.raises(LedgerReadUnbounded):
        await _open(ledger)
    with pytest.raises(LedgerReadUnbounded):
        await _fingerprints(ledger)


# --- managed live offers and cancel provenance --------------------------------


@pytest.mark.asyncio
async def test_ack_provenance_joins_attempt_and_decision(ledger) -> None:
    attempt = await ledger.attempt("200", venue_offer_id="offer-1", cell="a30")
    decision = await _decision(ledger, attempt, "corr-1")
    # Acked but not in an accepted snapshot yet: not live.
    assert await _managed(ledger) == ManagedOffers((), ())
    assert await _cancel(ledger, "offer-1") == CancelProvenance(
        "offer-1", "fUST", attempt, decision, "a30", "corr-1"
    )
    await ledger.accept(_live(_offer("offer-1", "200", "150")))
    offer = ManagedOffer("offer-1", "fUST", Decimal("150"), attempt, decision, "a30", "corr-1")
    assert await _managed(ledger) == ManagedOffers((offer,), ())
    assert await _cancel(ledger, "offer-1") == CancelProvenance(
        "offer-1", "fUST", attempt, decision, "a30", "corr-1"
    )


@pytest.mark.asyncio
async def test_bound_provenance(ledger) -> None:
    unknown = await ledger.attempt("200", outcome="unknown", cell="b7")
    await ledger.resolve("bound_to_venue", attempt_id=unknown, venue_offer_id="offer-2")
    decision = await _decision(ledger, unknown, "corr-2")
    # Bound but not yet in an accepted snapshot: still cancellable (legacy claims it).
    early = await _cancel(ledger, "offer-2")
    assert early is not None and early.attempt_id == unknown
    await ledger.accept(_live(_offer("offer-2", "200")))
    assert await _managed(ledger) == ManagedOffers(
        (ManagedOffer("offer-2", "fUST", Decimal("200"), unknown, decision, "b7", "corr-2"),), ()
    )
    cancel = await _cancel(ledger, "offer-2")
    assert cancel is not None and cancel.attempt_id == unknown


@pytest.mark.asyncio
async def test_foreign_offer_is_excluded(ledger) -> None:
    await ledger.accept(_live(_offer("manual", "300"), available="700"))
    assert await _managed(ledger) == ManagedOffers((), ())
    assert await _cancel(ledger, "manual") is None
    assert await _cancel(ledger, "never-seen") is None


@pytest.mark.asyncio
async def test_absent_and_terminal_offers_are_not_live(ledger) -> None:
    gone = await ledger.attempt("200", venue_offer_id="gone")
    done = await ledger.attempt("100", venue_offer_id="done")
    await ledger.accept(_live(_offer("gone", "200"), _offer("done", "100"), available="700"))
    assert {o.attempt_id for o in (await _managed(ledger)).offers} == {gone, done}
    # "gone" disappears without evidence; "done" has terminal history.
    await ledger.accept(
        _live(available="1000", history=(OfferHistory(_offer("done", "100"), "canceled", 1500),))
    )
    assert await _managed(ledger) == ManagedOffers((), ())
    assert (await _cancel(ledger, "gone")).attempt_id == gone
    assert await _cancel(ledger, "done") is None
    async with ledger.factory.begin() as session:
        terminal = await session.scalar(
            text("SELECT terminal_kind FROM venue_offer_mirror WHERE venue_offer_id='done'")
        )
    assert terminal == "canceled"


@pytest.mark.asyncio
async def test_contradictory_provenance_is_surfaced_and_excluded(ledger) -> None:
    await ledger.attempt("200", venue_offer_id="X")
    bound = await ledger.attempt("200", outcome="unknown")
    await ledger.resolve("bound_to_venue", attempt_id=bound, venue_offer_id="X")
    mine = await ledger.attempt("100", venue_offer_id="Y")
    await ledger.accept(_live(_offer("X", "200"), _offer("Y", "100"), available="700"))
    managed = await _managed(ledger)
    assert [o.attempt_id for o in managed.offers] == [mine]
    assert managed.provenance_conflicts == ("X",)
    with pytest.raises(ProvenanceConflict) as raised:
        await _cancel(ledger, "X")
    assert raised.value.venue_offer_id == "X"


@pytest.mark.asyncio
async def test_symbol_mismatch_is_a_conflict(book) -> None:
    await book.accept(_observation(usd=True))
    await book.attempt("5", venue_offer_id="Z", symbol="fUSD")
    await book.accept(_observation("800", offers=(_offer("Z", "5"),), usd=True))  # fUST offer
    assert await _managed(book) == ManagedOffers((), ("Z",))


@pytest.mark.asyncio
async def test_symbols_filter(book) -> None:
    await book.accept(_observation(usd=True))
    ust = await book.attempt("200", venue_offer_id="u-1")
    usd = await book.attempt("5", venue_offer_id="d-1", symbol="fUSD")
    await book.accept(
        _observation(
            "800", offers=(_offer("u-1", "200"), _offer("d-1", "5", symbol="fUSD")), usd=True
        )
    )
    assert [o.attempt_id for o in (await _managed(book, ["fUSD"])).offers] == [usd]
    assert [o.attempt_id for o in (await _managed(book, {"fUST"})).offers] == [ust]
    assert len((await _managed(book)).offers) == 2
    assert await _managed(book, []) == ManagedOffers((), ())


# --- fingerprints -------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprints_hold_until_settled(ledger) -> None:
    await ledger.attempt("10.00000002", outcome="unknown")
    await ledger.attempt("10.00000003", outcome="rejected")
    await ledger.attempt("10.00000004", outcome="not_sent")
    released = await ledger.attempt("10.00000005", outcome="unknown")
    await ledger.resolve("not_accepted", attempt_id=released)
    await ledger.attempt("10.00000006", venue_offer_id="acked")  # not reflected yet
    await ledger.attempt("10.00000007", outcome="unknown", symbol="fUSD")
    expected = {Decimal("10.00000002"), Decimal("10.00000006")}
    assert await _fingerprints(ledger) == expected
    # Carried as basis-unresolved (open UNKNOWN, unaccounted ack): still held.
    await ledger.accept()
    assert await _fingerprints(ledger) == expected
    # No outcome yet (a query cannot begin while it is in flight): held.
    await ledger.attempt("10.00000001", outcome=None)
    assert await _fingerprints(ledger) == expected | {Decimal("10.00000001")}


@pytest.mark.asyncio
async def test_fingerprints_follow_live_managed_offers(ledger) -> None:
    await ledger.attempt("200.00001234", venue_offer_id="offer-1")
    await ledger.attempt("300.00004321", venue_offer_id="offer-2")
    held = {Decimal("200.00001234"), Decimal("300.00004321")}
    assert await _fingerprints(ledger) == held
    # Reflected and live (one partially filled): held through the mirror.
    await ledger.accept(
        _live(
            _offer("offer-1", "200.00001234", "50"),
            _offer("offer-2", "300.00004321"),
            _offer("manual", "7.00000009"),  # foreign: never held
            available="500",
        )
    )
    assert await _fingerprints(ledger) == held
    # offer-1 terminal, offer-2 gone: both released.
    await ledger.accept(
        _live(
            available="1000",
            history=(OfferHistory(_offer("offer-1", "200.00001234", "0"), "executed", 1500),),
        )
    )
    assert await _fingerprints(ledger) == frozenset()


@pytest.mark.asyncio
async def test_open_unknown_releases_after_not_accepted(ledger) -> None:
    unknown = await ledger.attempt("10.00000002", outcome="unknown")
    await ledger.accept()
    assert await _fingerprints(ledger) == {Decimal("10.00000002")}
    await ledger.resolve("not_accepted", attempt_id=unknown)
    assert await _fingerprints(ledger) == frozenset()


@pytest.mark.asyncio
async def test_conflicting_offer_keeps_its_amount(ledger) -> None:
    await ledger.attempt("200.00000011", venue_offer_id="X", outcome="ack")
    bound = await ledger.attempt("200.00000011", outcome="unknown")
    await ledger.resolve("bound_to_venue", attempt_id=bound, venue_offer_id="X")
    await ledger.accept(_live(_offer("X", "200.00000011")))
    assert Decimal("200.00000011") in await _fingerprints(ledger)


# --- bounded work -------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_work_does_not_grow_with_history(ledger) -> None:
    """Terminal mirrors, resolved uncertainties and old attempts are not loaded."""
    classes = {mapper.class_ for mapper in _ledger_mappers()}
    live = await ledger.attempt("100", venue_offer_id="live")
    offers = [_offer("live", "100")]
    await ledger.accept(_live(*offers, available="900"))
    rounds = 0

    async def history(n: int) -> None:
        nonlocal rounds
        for _ in range(n):
            rounds += 1
            venue_id = f"old-{rounds}"
            await ledger.attempt("5", venue_offer_id=venue_id)
            await ledger.accept(_live(*offers, _offer(venue_id, "5"), available="895"))
            await ledger.accept(
                _live(
                    *offers,
                    available="900",
                    history=(OfferHistory(_offer(venue_id, "5"), "canceled", 1500),),
                )
            )
            unknown = await ledger.attempt("6", outcome="unknown")
            await ledger.resolve("not_accepted", attempt_id=unknown)
            quarantine = await ledger.quarantine()
            await ledger.resolve("manual", quarantine_id=quarantine)
            await ledger.attempt("7", outcome="rejected")
            await ledger.accept(_live(*offers, available="900"))

    async def measured() -> int:
        loads = 0

        def loaded(_target: Any, _context: Any) -> None:
            nonlocal loads
            loads += 1

        for cls in classes:
            event.listen(cls, "load", loaded)
        try:
            assert await _open(ledger) == ()
            assert [o.attempt_id for o in (await _managed(ledger)).offers] == [live]
            assert (await _cancel(ledger, "live")) is not None
            assert await _fingerprints(ledger) == {Decimal("100")}
        finally:
            for cls in classes:
                event.remove(cls, "load", loaded)
        return loads

    await history(1)
    small = await measured()
    await history(12)
    large = await measured()
    async with ledger.factory.begin() as session:
        mirrors = await session.scalar(text("SELECT count(*) FROM venue_offer_mirror"))
    assert mirrors == 14
    assert small > 0
    assert large == small


@pytest.mark.asyncio
async def test_read_plans_use_the_bounding_indexes(ledger) -> None:
    """Load counts miss rows scanned and filtered; pin the bounding index paths.

    With sequential scans disabled (tiny tables otherwise prefer them), the live
    mirror must come from the partial ``ix_venue_offer_mirror_live`` and the
    latest basis must walk ``ix_ledger_observation_query_scope_revision`` without
    sorting every basis of the scope.
    """
    await ledger.attempt("100", venue_offer_id="live")
    await ledger.accept(_live(_offer("live", "100"), available="900"))
    for _ in range(3):
        await ledger.accept(_live(_offer("live", "100"), available="900"))
    engine = ledger.factory.kw["bind"]
    captured: list[tuple[str, Any]] = []

    def grab(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", grab)
    try:
        await _open(ledger)
        await _managed(ledger)
        await _cancel(ledger, "live")
        await _fingerprints(ledger)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", grab)
    plans: dict[str, str] = {}
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SET enable_seqscan = off")
        for statement, parameters in captured:
            rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
            plans[statement] = "\n".join(line for (line,) in rows.all())
    mirror = [plan for sql, plan in plans.items() if "FROM venue_offer_mirror" in sql]
    basis = [plan for sql, plan in plans.items() if "accepted_capital_basis.id" in sql]
    # Cancel provenance adds one PK read of the offer's terminal evidence.
    assert len(mirror) == 4 and len(basis) == 1
    # The live index, or (one offer) the PK including venue_offer_id.
    assert all(
        "ix_venue_offer_mirror_live" in plan
        or "(venue_offer_id = 'live'::text)" in plan.split("Filter")[0]
        for plan in mirror
    ), mirror
    assert sum("ix_venue_offer_mirror_live" in plan for plan in mirror) >= 2, mirror
    assert "ix_ledger_observation_query_scope_revision" in basis[0], basis[0]
    assert "Sort" not in basis[0], basis[0]
