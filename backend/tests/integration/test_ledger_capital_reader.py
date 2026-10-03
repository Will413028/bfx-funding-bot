"""S1-2c-2 ledger capital reader on a clone PostgreSQL database.

Each case mirrors a legacy read-path test in ``test_capital_repository.py``
(cited per test) through the journals, the observation store, the basis and
``trading.derive_capital``.

Mutations (apply one at a time, run this file, revert):

1. drop the high-water filter on the tail: ``test_read_work_does_not_grow_with_history``,
   ``test_attempts_of_older_bases_are_not_charged_again``.
2. pick the basis by ``accepted_at_ms``: ``test_basis_is_the_latest_querys_not_the_latest_accepted``.
3. ignore ``accepted_capital_basis_symbol.block``: ``test_symbol_block_and_scope_block``.
4. ignore ``accepted_capital_basis.scope_block``: ``test_symbol_block_and_scope_block``.
5. a later resolution clears a basis-unresolved quarantine:
   ``test_quarantine_resolved_after_its_basis_blocks_until_a_new_basis``.
6. UNKNOWN blocks the whole scope: ``test_unknown_blocks_only_its_symbol``.
7. missing policy blocks the whole scope: ``test_missing_policy_blocks_only_its_symbol``.
8. freshness ``<=`` -> ``<``: ``test_freshness_boundary``.
9. skip quarantines opened after the basis:
   ``test_quarantine_opened_after_the_basis_blocks_its_symbol``.
10. skip the isolation check: ``test_transaction_must_be_repeatable_read_read_only``.
11. rejected/not_sent counted as pending: ``test_outcomes_release_or_hold_commitments``.
12. ignore a not_accepted resolution: ``test_tail_unknown_resolution_releases_or_holds``.
13. parse the policy without its digest check: ``test_policy_faults_are_scoped_to_their_symbol``.
14. open a quarantine without bumping the clock:
    ``test_quarantine_opened_after_the_basis_blocks_its_symbol``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.modules.ledger import (
    Attempt,
    CapitalReadRefused,
    Credit,
    LedgerCapitalRead,
    Observation,
    OfferHistory,
    Outcome,
    Quarantine,
    QueryHandle,
    Resolution,
    Scope,
    encode_basis_token,
    parse_basis_token,
)
from bfx_funding_bot.modules.ledger._internal import capital_reader
from bfx_funding_bot.modules.ledger._internal.journal import record_attempt
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisRow,
    CapitalPolicyRevisionRow,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_ledger_capital_reader,
    build_ledger_journal,
    build_ledger_observations,
)
from bfx_funding_bot.modules.trading import (
    Available,
    Blocked,
    CapitalPolicy,
    CapitalScope,
    policy_digest,
    policy_payload,
    policy_schema_version,
)

from .test_ledger_basis import (
    _credit,
    _engine,
    _ledger_mappers,
    _observation,
    _offer,
    _trade,
)
from .test_ledger_schema_roles import _A, ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
SCOPE = Scope(UUID(_A), "ci")
JOURNAL = build_ledger_journal()
READER = build_ledger_capital_reader()
OBSERVATIONS = build_ledger_observations()
_ATTEMPT_POLICY = "00000000-0000-0000-0000-00000000b001"


@pytest.mark.asyncio
async def test_reader_token_tracks_query_and_current_clock_including_tail(book) -> None:
    await book.policy("fUST")
    assert await book.accept() == "accepted"
    before = await book.read()
    assert before.basis_id == book.basis_id
    assert before.query_id is not None and before.clock_revision == 0
    token = encode_basis_token(before.query_id, before.clock_revision)
    assert parse_basis_token(token) == (before.query_id, 0)
    await book.attempt("200", outcome="ack", venue_offer_id="gone")
    after = await book.read()
    assert after.basis_id == before.basis_id and after.query_id == before.query_id
    assert after.clock_revision == 2
    assert _view(after).snapshot.unreflected_commitments == Decimal("200")
    assert encode_basis_token(after.query_id, after.clock_revision) != token
    query = await book.begin()
    pending = await book.read()
    assert pending.query_id == query.query_id and pending.basis_id is None
    assert pending.clock_revision == 2
    assert _reason(pending) == "snapshot_query_pending"


@pytest.mark.asyncio
async def test_reader_without_basis_reports_available_identity(book) -> None:
    read = await book.read()
    assert read.basis_id is None and read.query_id is None and read.clock_revision is None
    query = await book.begin()
    read = await book.read()
    assert read.query_id == query.query_id and read.clock_revision is None


class Book:
    """One scope's ledger, driven only through the ledger's own write paths."""

    def __init__(self, factory: Any, scope: Scope = SCOPE) -> None:
        self.factory = factory
        self.scope = scope
        self.basis_id: UUID | None = None
        self.observation_id: UUID | None = None
        self.revisions: dict[str, int] = {}
        self.decisions = 0

    async def begin(self, started: int = 1) -> QueryHandle:
        async with self.factory.begin() as session:
            handle: QueryHandle = await JOURNAL.begin_query(session, self.scope, started)
        return handle

    async def accept(
        self,
        observation: Observation | None = None,
        *,
        handle: QueryHandle | None = None,
        started: int = 1,
        finished: int = 2,
        confirmed: int = 4,
    ) -> str:
        handle = handle or await self.begin(started)
        first = replace(observation or _observation(), finished_at_ms=finished)
        confirmation = replace(
            first, finished_at_ms=confirmed, offer_history=(), credit_history=(), trades=()
        )
        async with self.factory.begin() as session:
            result = await OBSERVATIONS.accept(
                session, self.scope, handle, first, confirmation, finished
            )
        if result.decision == "accepted":
            self.observation_id = result.observation_id
            async with self.factory.begin() as session:
                self.basis_id = await session.scalar(
                    select(AcceptedCapitalBasisRow.id).where(
                        AcceptedCapitalBasisRow.observation_id == result.observation_id
                    )
                )
        return result.decision

    async def attempt(
        self,
        amount: str,
        *,
        outcome: str | None = "ack",
        venue_offer_id: str | None = None,
        cell: str = "a30",
        symbol: str = "fUST",
    ) -> UUID:
        assert self.basis_id is not None
        self.decisions += 1
        decision_id = f"reader-{self.scope.deployment_environment}-{self.decisions}-{uuid4()}"
        attempt_id = uuid4()
        async with self.factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO execution_decisions(decision_id, account_id, "
                    "exchange_account_id, deployment_environment, reconcile_id, cell_id, "
                    "symbol, signal_correlation_id, outcome, signal_rate, amount_usdt, "
                    "duration_days, model_evidence, safety_result, execution_policy, "
                    "service_version, config_hash, occurred_at_ms, recorded_at_ms) VALUES "
                    "(:d, 'account', :a, :e, 'r', :cell, :symbol, :d, 'submitted', 0, "
                    ":amount, 2, '{}', '{}', 'policy', 'test', 'hash', 0, 0)"
                ),
                {
                    "d": decision_id,
                    "a": str(self.scope.exchange_account_id),
                    "e": self.scope.deployment_environment,
                    "cell": cell,
                    "symbol": symbol,
                    "amount": amount,
                },
            )
            await record_attempt(
                session,
                self.scope,
                Attempt(
                    attempt_id,
                    decision_id,
                    symbol,
                    cell,
                    {"amount": amount, "symbol": symbol},
                    self.basis_id,
                    UUID(_ATTEMPT_POLICY),
                    {},
                    0,
                ),
            )
        if outcome is not None:
            async with self.factory.begin() as session:
                await JOURNAL.record_outcome(
                    session,
                    self.scope,
                    Outcome(
                        attempt_id,
                        outcome,  # type: ignore[arg-type]
                        venue_offer_id if outcome == "ack" else None,
                        None if outcome == "ack" else "test",
                        0,
                        {},
                    ),
                )
        return attempt_id

    async def resolve(
        self,
        action: str,
        *,
        attempt_id: UUID | None = None,
        quarantine_id: UUID | None = None,
        venue_offer_id: str | None = None,
        symbol: str = "fUST",
    ) -> None:
        assert self.observation_id is not None
        async with self.factory.begin() as session:
            await JOURNAL.record_resolution(
                session,
                self.scope,
                Resolution(
                    uuid4(),
                    symbol,
                    action,  # type: ignore[arg-type]
                    venue_offer_id,
                    self.observation_id,
                    "system",
                    "test",
                    10,
                    "test",
                    {},
                    attempt_id=attempt_id,
                    quarantine_id=quarantine_id,
                ),
            )

    async def quarantine(self, symbol: str = "fUST") -> UUID:
        quarantine_id = uuid4()
        async with self.factory.begin() as session:
            await JOURNAL.open_quarantine(
                session,
                self.scope,
                Quarantine(quarantine_id, symbol, Decimal("10"), 0, {"reason": "test"}),
            )
        return quarantine_id

    async def policy(
        self,
        symbol: str,
        *,
        enabled: bool = True,
        reserve: str = "100",
        fraction: str = "1",
        digest: str | None = None,
        owner: UUID | None = None,
    ) -> UUID:
        """A revision (``owner``'s scope, default this one) and this scope's head at it."""
        policy = CapitalPolicy(
            enabled=enabled, reserve_amount=Decimal(reserve), max_cell_fraction=Decimal(fraction)
        )
        payload = policy_payload(policy)
        revision = self.revisions.get(symbol, 0) + 1
        self.revisions[symbol] = revision
        revision_id = uuid4()
        params = {
            "id": revision_id,
            "a": str(self.scope.exchange_account_id),
            "o": str(owner or self.scope.exchange_account_id),
            "e": self.scope.deployment_environment,
            "s": symbol,
            "r": revision,
            "v": policy_schema_version(policy),
            "p": json.dumps(payload),
            "d": digest or policy_digest(payload),
        }
        async with self.factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO capital_policy_revisions(id, exchange_account_id, "
                    "deployment_environment, symbol, revision, schema_version, policy, digest, "
                    "source) VALUES (:id, :o, :e, :s, :r, :v, CAST(:p AS jsonb), :d, '{}')"
                ),
                params,
            )
            await session.execute(
                text(
                    "INSERT INTO capital_policy_heads(exchange_account_id, "
                    "deployment_environment, symbol, revision_id, revision) "
                    "VALUES (:a, :e, :s, :id, :r) ON CONFLICT (exchange_account_id, "
                    "deployment_environment, symbol) DO UPDATE SET "
                    "revision_id = EXCLUDED.revision_id, revision = EXCLUDED.revision"
                ),
                params,
            )
        return revision_id

    async def read(
        self,
        symbol: str = "fUST",
        cell: str = "a30",
        *,
        now: int = 100,
        max_age: int = 1000,
        mode: str = "ISOLATION LEVEL REPEATABLE READ READ ONLY",
        account: UUID | None = None,
    ) -> LedgerCapitalRead:
        scope = CapitalScope(
            account or self.scope.exchange_account_id,
            self.scope.deployment_environment,
            symbol,
            cell,
        )
        async with self.factory() as session, session.begin():
            await session.execute(text(f"SET TRANSACTION {mode}"))
            read: LedgerCapitalRead = await READER.read_capital(
                session, scope, now_ms=now, max_snapshot_age_ms=max_age
            )
        return read


def _view(read: LedgerCapitalRead) -> Any:
    assert isinstance(read.result, Available), read.result
    return read.result.view


def _reason(read: LedgerCapitalRead) -> str:
    assert isinstance(read.result, Blocked), read.result
    return read.result.reason


@pytest_asyncio.fixture
async def book(ledger_db):  # noqa: F811
    with ledger_db.begin() as conn:
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','ledger')"),
            {"id": _A},
        )
        # Attempts must name a policy revision; the reader never reads this one.
        conn.execute(
            text(
                "INSERT INTO capital_policy_revisions(id, exchange_account_id, "
                "deployment_environment, symbol, revision, schema_version, policy, digest, "
                "source) VALUES (:p, :a, 'ci', 'fATTEMPT', 1, 1, '{}', 'p', '{}')"
            ),
            {"p": _ATTEMPT_POLICY, "a": _A},
        )
    engine = _engine(ledger_db)
    try:
        yield Book(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_policy_blocks_only_its_symbol(book) -> None:
    """Legacy 120-133 and 664-677: no head is ``policy_unavailable`` for that
    symbol only (and for another scope); policy is read here, not at acceptance."""
    await book.accept(_observation(usd=True))
    assert _reason(await book.read("fUST")) == "policy_unavailable"
    await book.policy("fUST")
    view = _view(await book.read("fUST"))
    assert view.applied.revision == 1
    assert _reason(await book.read("fUSD")) == "policy_unavailable"
    assert _reason(await book.read("fUST", account=uuid4())) == "policy_unavailable"


@pytest.mark.asyncio
async def test_disabled_policy_reads_available_with_policy_disabled(book) -> None:
    """Legacy 1546-1576 (tail): a disabled symbol is a valid zero budget, not a block."""
    await book.policy("fUSD", enabled=False)
    await book.accept(_observation(usd=True))
    view = _view(await book.read("fUSD"))
    assert view.budget.reason == "policy_disabled"
    assert view.budget.max_new_offer == 0
    assert view.snapshot.available_amount == Decimal("5")


@pytest.mark.asyncio
async def test_reflected_commitment_is_not_charged_twice(book) -> None:
    """Legacy 136-165."""
    await book.policy("fUST")
    await book.accept()
    await book.attempt("200", venue_offer_id="offer-1")
    before = _view(await book.read())
    assert before.snapshot.unreflected_commitments == Decimal("200")
    assert before.budget.spendable == Decimal("700")
    await book.accept(_observation("800", offers=(_offer("offer-1", "200"),)))
    after = _view(await book.read())
    assert after.snapshot.unreflected_commitments == 0
    assert after.budget.spendable == Decimal("700")
    assert after.snapshot.total_capital == Decimal("1000")
    assert after.snapshot.cell_exposure == Decimal("200")


@pytest.mark.asyncio
async def test_u_counts_once_in_total_and_in_no_cell(book) -> None:
    """Legacy 214-232."""
    await book.policy("fUST", reserve="0", fraction="0.70")
    await book.accept(_observation("700", credits=(_credit("c1", "300", opening=101_100),)))
    for cell in ("a30", "p2"):
        view = _view(await book.read(cell=cell))
        assert view.snapshot.total_capital == Decimal("1000")
        assert view.snapshot.cell_exposure == 0
        assert view.budget.cell_headroom == Decimal("700")
        assert view.budget.max_new_offer == Decimal("700")
        assert view.unattributed_credit_exposure == Decimal("300")


@pytest.mark.asyncio
async def test_trade_attributed_credit_is_its_cells_exposure(book) -> None:
    """Legacy 297-321."""
    await book.policy("fUST", reserve="0", fraction="0.70")
    await book.accept()
    await book.attempt("200", venue_offer_id="101")
    await book.accept(_observation("800", offers=(_offer("101", "200"),)))
    await book.accept(
        _observation(
            "800", credits=(_credit("c1", "200", opening=101_150),), trades=(_trade("101", "200"),)
        )
    )
    a30, p2 = _view(await book.read()), _view(await book.read(cell="p2"))
    assert a30.snapshot.total_capital == p2.snapshot.total_capital == Decimal("1000")
    assert a30.unattributed_credit_exposure == 0
    assert (a30.snapshot.cell_exposure, a30.budget.cell_headroom) == (
        Decimal("200"),
        Decimal("500"),
    )
    assert (p2.snapshot.cell_exposure, p2.budget.cell_headroom) == (0, Decimal("700"))


@pytest.mark.asyncio
async def test_loan_turning_into_split_credits_stays_in_its_cells_exposure(book) -> None:
    """Legacy 418-469: recent fill, then carry by (symbol, period, opening)."""
    await book.policy("fUST", reserve="0", fraction="0.70")
    trade = Decimal("391.4117332")
    cash = str(Decimal("1000") - trade)
    opened = 101_150
    await book.accept()
    await book.attempt(str(trade), venue_offer_id="101")
    await book.accept(_observation(cash, offers=(_offer("101", str(trade)),)))
    one = Decimal("161.24782943")

    def lent(venue_id: str, amount: Decimal | str, created: int, kind: str = "credit") -> Credit:
        return _credit(venue_id, str(amount), kind=kind, created=created, opening=opened)

    stages = [
        (lent("60709535", trade, opened, "loan"),),
        (lent("463464628", one, 101_160), lent("60709642", trade - one, 101_160, "loan")),
        (
            lent("463464628", one, 101_160),
            lent("463464629", one, 101_170),
            lent("463464632", "68.91607434", 101_180),
        ),
    ]
    for credits in stages:
        await book.accept(_observation(cash, credits=credits))
        a30, p2 = _view(await book.read()), _view(await book.read(cell="p2"))
        assert a30.snapshot.cell_exposure == trade
        assert a30.unattributed_credit_exposure == p2.snapshot.cell_exposure == 0


@pytest.mark.asyncio
async def test_fenced_observation_leaves_the_read_pending(book) -> None:
    """Legacy 490-514: a command during the fetch fences the query; the old basis
    is superseded, never read."""
    await book.policy("fUST")
    await book.accept()
    handle = await book.begin(started=5)
    await book.attempt("200", outcome="rejected")
    assert await book.accept(handle=handle, finished=6, confirmed=7) == "fenced"
    read = await book.read()
    assert _reason(read) == "snapshot_query_pending"
    assert read.basis_id is None


@pytest.mark.asyncio
async def test_new_unfinished_query_invalidates_old_basis(book) -> None:
    """Legacy 537-548."""
    await book.policy("fUST")
    await book.accept()
    await book.begin(started=5)
    assert _reason(await book.read()) == "snapshot_query_pending"


@pytest.mark.asyncio
async def test_no_basis_is_snapshot_unavailable(book) -> None:
    await book.policy("fUST")
    read = await book.read()
    assert (_reason(read), read.basis_id) == ("snapshot_unavailable", None)
    await book.begin()
    assert _reason(await book.read()) == "snapshot_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "held"),
    [("rejected", "0"), ("not_sent", "0"), ("ack", "200"), (None, "200")],
)
async def test_outcomes_release_or_hold_commitments(book, outcome, held) -> None:
    """Legacy 608-633: rejected/not_sent release, pending/acknowledged hold."""
    await book.policy("fUST")
    await book.accept()
    await book.attempt("200", outcome=outcome, venue_offer_id="offer-1")
    view = _view(await book.read())
    assert view.snapshot.unreflected_commitments == Decimal(held)
    assert view.snapshot.cell_exposure == Decimal(held)
    assert view.budget.spendable == Decimal("900") - Decimal(held)


@pytest.mark.asyncio
async def test_unknown_blocks_only_its_symbol(book) -> None:
    """Legacy 1546-1576 and 608-633 (unknown)."""
    await book.policy("fUST")
    await book.policy("fUSD", enabled=False)
    await book.accept(_observation(usd=True))
    unknown = await book.attempt("200", outcome="unknown")
    read = await book.read()
    assert _reason(read) == "execution_unknown"
    assert read.result.evidence == (("uncertainty", str(unknown)),)
    assert _view(await book.read("fUSD")).budget.reason == "policy_disabled"
    # Still after an acceptance that names it; the other symbol is unaffected.
    await book.accept(_observation(usd=True))
    assert _reason(await book.read()) == "execution_unknown"
    assert _view(await book.read("fUSD")).budget.reason == "policy_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "venue", "held"), [("not_accepted", None, "0"), ("bound_to_venue", "o-1", "200")]
)
async def test_tail_unknown_resolution_releases_or_holds(book, action, venue, held) -> None:
    """Legacy 302-350 (resolution evidence): a resolved tail UNKNOWN is not open."""
    await book.policy("fUST")
    await book.accept()
    unknown = await book.attempt("200", outcome="unknown")
    await book.resolve(action, attempt_id=unknown, venue_offer_id=venue)
    view = _view(await book.read())
    assert view.snapshot.unreflected_commitments == Decimal(held)


@pytest.mark.asyncio
async def test_resolved_unknown_waits_for_a_new_basis(book) -> None:
    """Legacy 695-723: resolution clears it, but only through a new acceptance
    (ruling 3: the basis that recorded it unresolved keeps blocking)."""
    await book.policy("fUST")
    await book.accept()
    unknown = await book.attempt("200", outcome="unknown")
    await book.accept()
    await book.resolve("not_accepted", attempt_id=unknown)
    read = await book.read()
    assert _reason(read) == "execution_unknown"
    assert read.result.evidence == (("basis", "unresolved"),)
    await book.accept()
    view = _view(await book.read())
    assert view.snapshot.unreflected_commitments == 0


@pytest.mark.asyncio
async def test_partial_fill_does_not_add_original_reservation(book) -> None:
    """Legacy 636-661."""
    await book.policy("fUST")
    await book.accept()
    await book.attempt("200", venue_offer_id="offer-1")
    await book.accept(
        _observation(
            "800",
            offers=(_offer("offer-1", "200", "50"),),
            credits=(_credit("c1", "150", opening=101_100),),
        )
    )
    view = _view(await book.read())
    assert view.snapshot.total_capital == Decimal("1000")
    assert view.snapshot.cell_exposure == Decimal("200")
    assert view.unattributed_credit_exposure == 0
    assert view.snapshot.unreflected_commitments == 0


@pytest.mark.asyncio
async def test_policy_faults_are_scoped_to_their_symbol(book) -> None:
    """Legacy 880-902: stale, other-account pointer and invalid digest block."""
    await book.policy("fUST", digest="0" * 64)
    other = uuid4()
    async with book.factory.begin() as session:
        await session.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','o')"),
            {"id": other},
        )
    await book.policy("fUSD", owner=other)
    await book.policy("fEUR")
    await book.accept(_observation(usd=True))
    assert _reason(await book.read("fUST")) == "invalid_policy_schema_or_digest"
    assert _reason(await book.read("fUSD")) == "inconsistent_policy_pointer"
    assert _reason(await book.read("fEUR")) == "snapshot_symbol_missing"
    await book.policy("fUST")
    assert _view(await book.read("fUST")).applied.revision == 2
    assert _reason(await book.read("fUST", now=20000)) == "snapshot_stale"


@pytest.mark.asyncio
async def test_symbol_block_and_scope_block(book) -> None:
    """Basis R7 (capital_repository.py:849-853): a symbol fact blocks that symbol,
    a scope fact blocks every symbol."""
    await book.policy("fUST")
    await book.policy("fUSD")
    await book.accept(_observation(usd=True))
    await book.attempt("200", venue_offer_id="X")
    bound = await book.attempt("200", outcome="unknown")
    await book.resolve("bound_to_venue", attempt_id=bound, venue_offer_id="X")
    await book.accept(_observation("800", offers=(_offer("X", "200"),), usd=True))
    read = await book.read()
    assert isinstance(read.result, Blocked)
    assert ("reason", "offer_provenance_conflict") in read.result.evidence
    assert ("venue_offer_id", "X") in read.result.evidence
    assert _view(await book.read("fUSD")).snapshot.available_amount == Decimal("5")
    await book.accept(_observation(offers=(_offer("eur-1", "10", symbol="fEUR"),), usd=True))
    for symbol in ("fUST", "fUSD"):
        read = await book.read(symbol)
        assert _reason(read) == "missing_wallet"
        assert ("symbol", "fEUR") in read.result.evidence


@pytest.mark.asyncio
async def test_quarantine_opened_after_the_basis_blocks_its_symbol(book) -> None:
    """Ruling 5: ``opened_revision > accept_revision`` reaches the reader; it
    blocks its symbol only, even once resolved, until a new basis."""
    await book.policy("fUST")
    await book.policy("fUSD")
    await book.accept(_observation(usd=True))
    await book.attempt("10", outcome="rejected")
    await book.accept(_observation(usd=True))  # accept_revision 2: the clock has moved
    # The opening bumps the clock: a query begun before it cannot be accepted.
    handle = await book.begin()
    first = await book.quarantine()
    assert await book.accept(_observation(usd=True), handle=handle) == "fenced"
    read = await book.read()
    assert _reason(read) == "snapshot_query_pending"
    await book.accept(_observation(usd=True))
    listed = await book.read()
    assert listed.result.evidence == (("basis_quarantine", str(first)),)
    await book.resolve("manual", quarantine_id=first)
    quarantine = await book.quarantine()
    read = await book.read()
    assert _reason(read) == "execution_unknown"
    assert read.result.evidence == (("uncertainty", str(quarantine)),)
    assert isinstance((await book.read("fUSD")).result, Available)
    await book.resolve("manual", quarantine_id=quarantine)
    assert _reason(await book.read()) == "execution_unknown"
    await book.accept(_observation(usd=True))
    assert isinstance((await book.read()).result, Available)


@pytest.mark.asyncio
async def test_quarantine_resolved_after_its_basis_blocks_until_a_new_basis(book) -> None:
    """Ruling 3: a basis that lists a quarantine unresolved keeps blocking."""
    await book.policy("fUST")
    quarantine = await book.quarantine()
    await book.accept()
    read = await book.read()
    assert read.result.evidence == (("basis_quarantine", str(quarantine)),)
    await book.resolve("manual", quarantine_id=quarantine)
    read = await book.read()
    assert _reason(read) == "execution_unknown"
    assert read.result.evidence == (("basis_quarantine", str(quarantine)),)
    await book.accept()
    assert isinstance((await book.read()).result, Available)


@pytest.mark.asyncio
async def test_freshness_boundary(book) -> None:
    """Legacy 880-902 (stale) with capital_repository.py:455-462 inclusive
    bounds: 0 <= now - query start <= max age, on the local clock only."""
    await book.policy("fUST")
    await book.accept(started=10, finished=12, confirmed=14)
    assert isinstance((await book.read(now=12, max_age=100)).result, Available)
    assert isinstance((await book.read(now=110, max_age=100)).result, Available)
    assert _reason(await book.read(now=111, max_age=100)) == "snapshot_stale"
    assert _reason(await book.read(now=11, max_age=100)) == "snapshot_stale"  # before finish
    assert isinstance((await book.read(now=12, max_age=2)).result, Available)
    assert _reason(await book.read(now=12, max_age=1)) == "snapshot_stale"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "ISOLATION LEVEL READ COMMITTED READ ONLY",
        "ISOLATION LEVEL SERIALIZABLE READ ONLY",
        "ISOLATION LEVEL REPEATABLE READ READ WRITE",
    ],
)
async def test_transaction_must_be_repeatable_read_read_only(book, mode) -> None:
    await book.policy("fUST")
    await book.accept()
    with pytest.raises(CapitalReadRefused):
        await book.read(mode=mode)


@pytest.mark.asyncio
async def test_clock_behind_the_basis_is_an_evidence_conflict(book) -> None:
    await book.policy("fUST")
    await book.accept()
    await book.attempt("10", outcome="rejected")
    await book.accept()
    assert isinstance((await book.read()).result, Available)
    async with book.factory.begin() as session:
        await session.execute(text("UPDATE capital_command_clock SET revision = 1"))
    read = await book.read()
    assert _reason(read) == "snapshot_evidence_conflict"
    assert read.result.evidence == (("accept_revision", "2"), ("clock", "1"))


@pytest.mark.asyncio
async def test_scopes_are_isolated(book) -> None:
    other = Book(book.factory, Scope(SCOPE.exchange_account_id, "ci2"))
    await book.policy("fUST")
    await other.policy("fUST")
    await book.accept()
    await other.accept(_observation("300"))
    await book.attempt("200", outcome="unknown")
    await book.quarantine()
    view = _view(await other.read())
    assert (view.snapshot.available_amount, view.snapshot.unreflected_commitments) == (
        Decimal("300"),
        0,
    )
    assert _reason(await book.read()) == "execution_unknown"


@pytest.mark.asyncio
async def test_basis_is_the_latest_querys_not_the_latest_accepted(book) -> None:
    """The basis follows query order; acceptance times are venue-paced evidence."""
    await book.policy("fUST")
    await book.accept(started=1, finished=2, confirmed=900)
    await book.accept(_observation("600"), started=5, finished=6, confirmed=8)
    view = _view(await book.read())
    assert view.snapshot.available_amount == Decimal("600")


@pytest.mark.asyncio
async def test_attempts_of_older_bases_are_not_charged_again(book) -> None:
    await book.policy("fUST")
    await book.accept()
    await book.attempt("200", venue_offer_id="offer-1")
    await book.accept(_observation("800", offers=(_offer("offer-1", "200"),)))
    await book.accept(_observation("800", offers=(_offer("offer-1", "200"),)))
    view = _view(await book.read())
    assert view.snapshot.unreflected_commitments == 0
    assert view.snapshot.cell_exposure == Decimal("200")


@pytest.mark.asyncio
async def test_tail_longer_than_the_cap_fails_closed(book, monkeypatch) -> None:
    monkeypatch.setattr(capital_reader, "MAX_TAIL_ATTEMPTS", 2)
    await book.policy("fUST")
    await book.accept()
    for _ in range(2):
        await book.attempt("10", outcome="rejected")
    assert isinstance((await book.read()).result, Available)
    await book.attempt("10", outcome="rejected")
    assert _reason(await book.read()) == "attempt_tail_unbounded"


@pytest.mark.asyncio
@pytest.mark.parametrize(("occurred", "reflected"), [(10000, True), (10001, False)])
async def test_terminal_history_allows_venue_clock_tolerance(book, occurred, reflected) -> None:
    """Incident 2026-09-29/30: a venue stamp compared with a local bound allows
    VENUE_CLOCK_TOLERANCE_MS. History was requested up to 5000 (local)."""
    await book.policy("fUST")
    await book.accept()
    await book.attempt("200", venue_offer_id="offer-1")
    executed = _offer("offer-1", "200", "0", created=101_150)
    await book.accept(
        _observation(
            "800",
            credits=(_credit("61621685", "200", kind="loan", opening=101_150),),
            history=(OfferHistory(executed, "executed", occurred),),
        )
    )
    read = await book.read()
    if reflected:
        assert _view(read).snapshot.cell_exposure == Decimal("200")
    else:
        # The unplaced attempt cannot offset the loan, so its lending is also unexplained;
        # the protection-grade verdict leads and names the fact-level cause.
        assert _reason(read) == "venue_lent_above_ledger"
        assert ("also", "unclassifiable_commitment") in read.result.evidence


@pytest.mark.asyncio
async def test_read_work_does_not_grow_with_history(book) -> None:
    """Many old bases, settled attempts and resolved quarantines: same rows loaded."""
    classes = {mapper.class_ for mapper in _ledger_mappers()} | {CapitalPolicyRevisionRow}
    await book.policy("fUST")
    await book.accept(_observation("700", offers=(_offer("manual", "300"),)))

    async def history(n: int) -> None:
        for _ in range(n):
            await book.attempt("10", outcome="rejected")
            quarantine = await book.quarantine()
            await book.accept(_observation("700", offers=(_offer("manual", "300"),)))
            await book.resolve("manual", quarantine_id=quarantine)
            await book.accept(_observation("700", offers=(_offer("manual", "300"),)))
        await book.attempt("10", outcome="rejected")  # one tail attempt

    async def measured() -> int:
        loads = 0

        def loaded(_target: Any, _context: Any) -> None:
            nonlocal loads
            loads += 1

        for cls in classes:
            event.listen(cls, "load", loaded)
        try:
            view = _view(await book.read())
        finally:
            for cls in classes:
                event.remove(cls, "load", loaded)
        assert view.snapshot.unreflected_commitments == 0
        return loads

    await history(1)
    small = await measured()
    await history(15)
    large = await measured()
    assert small > 0
    assert large == small
