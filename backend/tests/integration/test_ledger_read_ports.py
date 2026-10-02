"""S1-3e1 ledger consumer read ports on a clone PostgreSQL database.

Ledger-only behaviour; the observables both authorities share are asserted once
per authority in ``tests/integration/contracts``.

Mutations (apply one at a time, run this file and the contracts, revert):

1. the session path skips ``lock_scope``:
   ``test_read_with_a_session_takes_the_scope_lock_and_a_writer_waits``.
2. the locked read accepts a session without the advisory lock:
   ``test_locked_read_refuses_a_session_without_the_scope_lock``.
3. the own-session path reads READ COMMITTED:
   ``test_own_session_read_is_repeatable_read_read_only_and_rolls_back``.
4. ``live`` returns managed offers ignoring conflicts:
   ``test_live_raises_on_contradictory_provenance``.
5. ``count_live`` excludes conflicts: ``test_count_live_counts_contradictory_offers``.
6. ``live_symbols`` managed only: ``test_live_symbols_include_foreign_and_contradictory_offers``
   and the contract ``test_live_symbols_include_manual_offers``.
7. ``fingerprints_in_use`` returns raw amounts / includes None: the contracts
   ``test_fingerprints_of_live_managed_offers_and_an_unresolved_commitment`` and
   ``test_an_amount_without_a_fingerprint_holds_nothing``.
8. the UncertaintyReader drops quarantines: the contract
   ``test_unknown_and_quarantine_are_listed_with_the_authority_kind_codes`` and
   ``test_has_open_asks_the_database_for_one_row``.
9. ``read_policy`` takes the scope lock: ``test_read_policy_takes_no_lock``.
10. the basis token is built from ``basis_id``:
    ``test_basis_token_is_the_query_and_the_clock_revision``.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, text

from bfx_funding_bot.modules.ledger import (
    CapitalAvailable,
    CapitalBlocked,
    CapitalReadRefused,
    ProvenanceConflict,
    encode_basis_token,
    parse_basis_token,
)
from bfx_funding_bot.modules.ledger._internal import capital_reader
from bfx_funding_bot.modules.ledger._internal.clock import holds_scope_lock, lock_scope
from bfx_funding_bot.modules.ledger.wiring import (
    build_capital_authority,
    build_ledger_journal,
    build_managed_offer_reader,
    build_operator_reads,
    build_scope_lock,
    build_uncertainty_reader,
)
from bfx_funding_bot.modules.trading import CapitalScope

from .test_ledger_basis import _observation, _offer
from .test_ledger_capital_reader import SCOPE, Book
from .test_ledger_capital_reader import book as book
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = 100
MAX_AGE = 1000
CAPITAL = CapitalScope(SCOPE.exchange_account_id, SCOPE.deployment_environment, "fUST", "a30")
RR = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
WAIT_S = 0.5


def _authority(book: Book, *, max_age: int = MAX_AGE) -> Any:
    return build_capital_authority(book.factory, max_snapshot_age_ms=max_age)


async def _ready(book: Book) -> None:
    await book.policy("fUST")
    assert await book.accept() == "accepted"


async def _writer_waits(book: Book, holder: Any) -> None:
    """A ledger writer of the scope blocks while ``holder``'s transaction lives."""
    journal = build_ledger_journal()
    async with book.factory.begin() as writer:
        waiting = asyncio.ensure_future(journal.bump_clock(writer, SCOPE))
        try:
            done, _ = await asyncio.wait({waiting}, timeout=WAIT_S)
            assert not done, "a writer ran while the reader held the scope lock"
            await holder.commit()
            await asyncio.wait_for(waiting, timeout=10)
        finally:
            waiting.cancel()


# --- CapitalAuthority -----------------------------------------------------


async def test_locked_read_refuses_a_session_without_the_scope_lock(book) -> None:
    await _ready(book)
    async with book.factory.begin() as session:
        with pytest.raises(CapitalReadRefused, match="advisory lock"):
            await capital_reader.read_capital_locked(
                session, CAPITAL, now_ms=NOW, max_snapshot_age_ms=MAX_AGE
            )
        assert not await holds_scope_lock(session, SCOPE)


async def test_locked_read_accepts_a_read_committed_session_under_the_lock(book) -> None:
    await _ready(book)
    async with book.factory.begin() as session:
        await lock_scope(session, SCOPE)
        assert await holds_scope_lock(session, SCOPE)
        read = await capital_reader.read_capital_locked(
            session, CAPITAL, now_ms=NOW, max_snapshot_age_ms=MAX_AGE
        )
    assert isinstance(read.result.view.budget.spendable, Decimal)  # type: ignore[union-attr]


async def test_locked_read_refuses_a_repeatable_read_session_even_under_the_lock(book) -> None:
    await _ready(book)
    async with book.factory.begin() as session:
        await session.execute(text(RR))
        await lock_scope(session, SCOPE)
        with pytest.raises(CapitalReadRefused, match="READ COMMITTED"):
            await capital_reader.read_capital_locked(
                session, CAPITAL, now_ms=NOW, max_snapshot_age_ms=MAX_AGE
            )


async def test_the_lock_of_another_scope_does_not_count(book) -> None:
    await _ready(book)
    other = type(SCOPE)(book.scope.exchange_account_id, "other-env")
    async with book.factory.begin() as session:
        await lock_scope(session, other)
        assert not await holds_scope_lock(session, SCOPE)
        with pytest.raises(CapitalReadRefused):
            await capital_reader.read_capital_locked(
                session, CAPITAL, now_ms=NOW, max_snapshot_age_ms=MAX_AGE
            )


async def test_read_with_a_session_takes_the_scope_lock_and_a_writer_waits(book) -> None:
    await _ready(book)
    authority = _authority(book)
    session = book.factory()
    try:
        await session.begin()
        assert not await holds_scope_lock(session, SCOPE)
        read = await authority.read(CAPITAL, now_ms=NOW, session=session)
        assert isinstance(read, CapitalAvailable)
        assert await holds_scope_lock(session, SCOPE)
        await _writer_waits(book, session)
    finally:
        await session.close()


async def test_read_with_a_repeatable_read_caller_session_is_refused(book) -> None:
    await _ready(book)
    async with book.factory.begin() as session:
        await session.execute(text(RR))
        with pytest.raises(CapitalReadRefused):
            await _authority(book).read(CAPITAL, now_ms=NOW, session=session)


async def test_own_session_read_is_repeatable_read_read_only_and_rolls_back(
    book, monkeypatch
) -> None:
    await _ready(book)
    seen: list[Any] = []
    real = capital_reader.read_capital

    async def spy(session, scope, **kwargs):
        row = (
            await session.execute(
                text(
                    "SELECT current_setting('transaction_isolation'), "
                    "current_setting('transaction_read_only')"
                )
            )
        ).one()
        seen.append((tuple(row), await holds_scope_lock(session, SCOPE)))
        return await real(session, scope, **kwargs)

    monkeypatch.setattr(capital_reader, "read_capital", spy)
    read = await _authority(book).read(CAPITAL, now_ms=NOW)
    assert isinstance(read, CapitalAvailable)
    assert seen == [(("repeatable read", "on"), False)]  # a snapshot, and no lock taken
    async with book.factory() as probe:
        idle = await probe.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'idle in transaction'"
            )
        )
    assert idle == 0  # rolled back, not left open


async def test_own_session_read_writes_nothing(book) -> None:
    await _ready(book)
    async with book.factory() as probe:
        before = await probe.scalar(text("SELECT revision FROM capital_command_clock"))
    await _authority(book).read(CAPITAL, now_ms=NOW)
    async with book.factory() as probe:
        assert await probe.scalar(text("SELECT revision FROM capital_command_clock")) == before


async def test_read_policy_takes_no_lock(book) -> None:
    await book.policy("fUST")
    authority = _authority(book)
    async with book.factory() as holder, book.factory() as reader:
        await holder.begin()
        await lock_scope(holder, SCOPE)
        await reader.begin()
        # Not blocked by the holder, and holds nothing itself.
        applied = await asyncio.wait_for(authority.read_policy(reader, SCOPE, "fUST"), timeout=5)
        assert not isinstance(applied, CapitalBlocked) and applied.revision == 1
        assert not await holds_scope_lock(reader, SCOPE)
        missing = await authority.read_policy(reader, SCOPE, "fUSD")
        assert isinstance(missing, CapitalBlocked) and missing.reason == "policy_unavailable"
        await reader.rollback()
        await holder.rollback()


async def test_basis_token_is_the_query_and_the_clock_revision(book) -> None:
    await _ready(book)
    authority = _authority(book)
    read = await authority.read(CAPITAL, now_ms=NOW)
    assert isinstance(read, CapitalAvailable)
    raw = await book.read()
    assert raw.query_id is not None and raw.clock_revision is not None
    assert raw.basis_id is not None and raw.basis_id != raw.query_id
    assert read.basis_token == encode_basis_token(raw.query_id, raw.clock_revision)
    assert parse_basis_token(read.basis_token) == (raw.query_id, raw.clock_revision)
    await book.attempt("200", outcome="ack", venue_offer_id="gone")  # a command bumps the clock
    moved = await authority.read(CAPITAL, now_ms=NOW)
    assert isinstance(moved, CapitalAvailable)
    assert parse_basis_token(moved.basis_token) == (raw.query_id, raw.clock_revision + 2)
    assert moved.basis_token != read.basis_token


async def test_blocked_keeps_the_reason_and_evidence(book) -> None:
    await _ready(book)
    query = await book.begin(started=50)
    read = await _authority(book).read(CAPITAL, now_ms=NOW)
    assert read == CapitalBlocked("snapshot_query_pending", (("query", str(query.query_id)),))


async def test_the_injected_age_bound_is_the_freshness_bound(book) -> None:
    await book.policy("fUST")
    await book.accept(started=10, finished=12, confirmed=14)
    assert isinstance(await _authority(book, max_age=100).read(CAPITAL, now_ms=110), CapitalAvailable)
    stale = await _authority(book, max_age=99).read(CAPITAL, now_ms=110)
    assert isinstance(stale, CapitalBlocked) and stale.reason == "snapshot_stale"


# --- ManagedOfferReader -----------------------------------------------------


async def _conflict_scenario(book: Book) -> None:
    """Managed Y (fUST), contradictory X (fUST), a foreign fUSD offer."""
    await book.accept(_observation(usd=True))
    await book.attempt("200", venue_offer_id="X")
    bound = await book.attempt("200", outcome="unknown")
    await book.resolve("bound_to_venue", attempt_id=bound, venue_offer_id="X")
    await book.attempt("100", venue_offer_id="Y")
    await book.accept(
        _observation(
            "700", usd=True,
            offers=(_offer("X", "200"), _offer("Y", "100"), _offer("F", "5", symbol="fUSD")),
        )
    )


async def test_live_raises_on_contradictory_provenance(book) -> None:
    await _conflict_scenario(book)
    offers = build_managed_offer_reader()
    async with book.factory() as session:
        with pytest.raises(ProvenanceConflict) as raised:
            await offers.live(session, SCOPE)
        assert raised.value.venue_offer_id == "X"
        with pytest.raises(ProvenanceConflict):
            await offers.live(session, SCOPE, ["fUST"])
        # The requested symbols decide: fUSD has no conflict and no managed offer.
        assert await offers.live(session, SCOPE, ["fUSD"]) == ()


async def test_count_live_counts_contradictory_offers(book) -> None:
    await _conflict_scenario(book)
    async with book.factory() as session:
        # Y managed + X contradictory; the foreign fUSD offer never counts.
        assert await build_managed_offer_reader().count_live(session, SCOPE, "fUST") == 2
        assert await build_managed_offer_reader().count_live(session, SCOPE, "fUSD") == 0


async def test_live_symbols_include_foreign_and_contradictory_offers(book) -> None:
    await _conflict_scenario(book)
    async with book.factory() as session:
        assert await build_managed_offer_reader().live_symbols(session, SCOPE) == {"fUST", "fUSD"}
    await book.accept(_observation("1000", usd=True), started=60, finished=61, confirmed=62)
    async with book.factory() as session:  # all gone from the venue
        assert await build_managed_offer_reader().live_symbols(session, SCOPE) == frozenset()


async def test_contradictory_offers_hold_their_fingerprint(book) -> None:
    await _conflict_scenario(book)
    async with book.factory() as session:
        held = await build_managed_offer_reader().fingerprints_in_use(session, SCOPE, "fUST")
    assert held == frozenset()  # 200 / 100 carry no fingerprint: nothing to hold, nothing raised
    await book.attempt("300.00000321", outcome="unknown")
    async with book.factory() as session:
        held = await build_managed_offer_reader().fingerprints_in_use(session, SCOPE, "fUST")
    assert held == frozenset({321})


# --- UncertaintyReader ------------------------------------------------------


async def test_kinds_are_the_ones_the_operator_reads_emit(book) -> None:
    await book.accept()
    await book.attempt("200", outcome="unknown")
    await book.quarantine("fUST")
    records = await build_uncertainty_reader(book.factory).list_open(None, SCOPE)
    async with book.factory() as session:
        views = await build_operator_reads().list_uncertainties(
            session, SCOPE, state="open", limit=10
        )
    assert sorted(r.kind for r in records) == sorted(v.kind for v in views)
    assert {r.kind for r in records} == {"submit_outcome_unknown", "unattributed_venue_offer"}


async def test_has_open_asks_the_database_for_one_row(book) -> None:
    await book.accept()
    for _ in range(3):
        await book.quarantine("fUSD")
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = book.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert await build_uncertainty_reader(book.factory).has_open(None, SCOPE, "fUSD")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    openings = [s for s in statements if "FROM quarantine_opening" in s]
    assert openings and all("LIMIT" in s for s in openings), openings


async def test_own_session_uncertainty_reads_roll_back(book) -> None:
    await book.accept()
    await book.quarantine("fUST")
    reader = build_uncertainty_reader(book.factory)
    assert await reader.has_open(None, SCOPE, "fUST")
    assert len(await reader.list_open(None, SCOPE)) == 1
    async with book.factory() as probe:
        idle = await probe.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'idle in transaction'"
            )
        )
    assert idle == 0


# --- ScopeLock --------------------------------------------------------------


async def test_scope_lock_is_the_ledger_writers_lock(book) -> None:
    await book.accept()
    lock = build_scope_lock()
    session = book.factory()
    try:
        await session.begin()
        await lock.lock(session, SCOPE)
        assert await holds_scope_lock(session, SCOPE)
        await _writer_waits(book, session)
    finally:
        await session.close()
