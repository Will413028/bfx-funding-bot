"""S1-2b-2 capital basis classification on a clone PostgreSQL database.

Parity with ``test_capital_repository.py`` (legacy ``_classify``), rebuilt on the
journals and observation store. A cell holds the remaining amount of its resting
managed offers only; lent credits count in the symbol, in no cell
(docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md).

Mutations, each applied alone to a copy of ``modules/ledger/_internal/basis.py`` with
this file run from the copy (the working tree is never edited). The first is automated as
M19 of ``scripts/capital_mutation_gate.py``; the others are listed by what they break:

* count a lent credit into a cell:
  ``test_lent_credit_counts_in_the_symbol_and_in_no_cell``.
* charge the original instead of the remaining offer amount:
  ``test_partial_fill_leaves_only_the_resting_remainder_in_the_cell``.
* load every historical attempt, or remove the high-water bound:
  ``test_basis_work_does_not_grow_with_history``.
* treat an offer without provenance as managed:
  ``test_foreign_offer_is_counted_out_of_managed_exposure``.
* remove the offer amount-conflict check: ``test_amount_mismatch_and_unaccounted_ack_block``.
* drop the provenance check of ended offers:
  ``test_an_ended_offer_with_contradictory_provenance_blocks_its_symbol``.
* block the whole scope for a symbol-level fact (R7):
  ``test_provenance_conflict_blocks_only_its_symbol``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import count
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.ledger import (
    Attempt,
    Coverage,
    Credit,
    CreditHistory,
    Observation,
    Offer,
    OfferHistory,
    Outcome,
    Resolution,
    Scope,
    Trade,
    Wallet,
)
from bfx_funding_bot.modules.ledger._internal.journal import record_attempt
from bfx_funding_bot.modules.ledger.tables import (
    LEDGER_TABLES,
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisCellRow,
    AcceptedCapitalBasisCreditCellRow,
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
)
from bfx_funding_bot.modules.ledger.wiring import build_ledger_journal, build_ledger_observations

from .test_ledger_schema_roles import _A, _P, ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
SCOPE = Scope(UUID(_A))
JOURNAL = build_ledger_journal()
OBSERVATIONS = build_ledger_observations()
_IDS = count(1)


def _engine(sync_engine):
    return create_async_engine(
        sync_engine.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg")
    )


def _coverage() -> Coverage:
    return Coverage(
        True,
        True,
        True,
        True,
        True,
        True,
        1,
        1,
        1,
        1,
        1,
        1,
        0,
        5000,
        None,
        None,
        trades_complete=True,
        trades_requested_start_ms=0,
        trades_requested_end_ms=105_000,
        history_symbols=frozenset({"fUST", "fUSD"}),
    )


def _offer(
    venue_id: str,
    original: str,
    remaining: str | None = None,
    *,
    created: int = 101_000,
    symbol: str = "fUST",
) -> Offer:
    left = Decimal(remaining if remaining is not None else original)
    return Offer(
        venue_id,
        symbol,
        Decimal(original),
        left,
        Decimal("0.0001"),
        True,
        2,
        "LIMIT",
        None,
        "partially_filled" if left < Decimal(original) else "active",
        created,
        None,
        {},
    )


def _credit(
    venue_id: str,
    amount: str,
    *,
    opening: int,
    kind: str = "credit",
    created: int | None = None,
    period: int = 2,
) -> Credit:
    return Credit(
        kind,  # type: ignore[arg-type]
        venue_id,
        "fUST",
        Decimal(amount),
        Decimal("0.0001"),
        period,
        "active",
        None,
        created if created is not None else opening,
        None,
        opening,
        {},
    )


def _trade(offer_id: str, amount: str, *, mts_create: int = 101_150) -> Trade:
    return Trade(next(_IDS), "fUST", offer_id, Decimal(amount), Decimal("0.0001"), 2, mts_create)


def _observation(
    available: str = "1000",
    *,
    offers: tuple[Offer, ...] = (),
    credits: tuple[Credit, ...] = (),
    history: tuple[OfferHistory, ...] = (),
    trades: tuple[Trade, ...] = (),
    usd: bool = False,
    credit_history: tuple[CreditHistory, ...] = (),
) -> Observation:
    wallets = [Wallet("funding", "UST", Decimal(available), Decimal(available), "fUST")]
    if usd:
        wallets.append(Wallet("funding", "USD", Decimal("5"), Decimal("5"), "fUSD"))
    return Observation(
        tuple(wallets), offers, credits, _coverage(), 2, history, credit_history, trades
    )


@dataclass(frozen=True)
class Basis:
    row: AcceptedCapitalBasisRow
    symbols: dict[str, AcceptedCapitalBasisSymbolRow]
    cells: dict[str, Decimal]
    credits: dict[str, Decimal]
    credit_cells: int
    attempts: dict[UUID, tuple[str, str]]

    def reasons(self, symbol: str = "fUST") -> list[str]:
        block = self.symbols[symbol].block
        return [] if block is None else [item["reason"] for item in block["reasons"]]

    @property
    def blocked(self) -> bool:
        return self.row.scope_block is not None or any(
            s.block is not None for s in self.symbols.values()
        )


class Ledger:
    def __init__(self, factory) -> None:
        self.factory = factory
        self.basis_id: UUID | None = None

    async def accept(self, observation: Observation, *, started_at_ms: int = 1) -> Basis:
        async with self.factory.begin() as session:
            handle = await JOURNAL.begin_query(session, SCOPE, started_at_ms)
        observation = replace(observation, finished_at_ms=started_at_ms + 1)
        async with self.factory.begin() as session:
            result = await OBSERVATIONS.accept(
                session,
                SCOPE,
                handle,
                observation,
                replace(
                    observation,
                    finished_at_ms=started_at_ms + 3,
                    offer_history=(),
                    credit_history=(),
                    trades=(),
                ),
                started_at_ms + 2,
            )
        assert result.decision == "accepted"
        basis = await self.read(result.observation_id)
        self.basis_id = basis.row.id
        return basis

    async def read(self, observation_id: UUID | None) -> Basis:
        async with self.factory.begin() as session:
            row = await session.scalar(
                select(AcceptedCapitalBasisRow).where(
                    AcceptedCapitalBasisRow.observation_id == observation_id
                )
            )
            assert row is not None
            symbols = {
                s.symbol: s
                for s in await session.scalars(
                    select(AcceptedCapitalBasisSymbolRow).where(
                        AcceptedCapitalBasisSymbolRow.basis_id == row.id
                    )
                )
            }
            cells = {
                c.cell_id: c.amount
                for c in await session.scalars(
                    select(AcceptedCapitalBasisCellRow).where(
                        AcceptedCapitalBasisCellRow.basis_id == row.id
                    )
                )
            }
            credits = {
                c.venue_credit_id: c.amount
                for c in await session.scalars(
                    select(AcceptedCapitalBasisCreditRow).where(
                        AcceptedCapitalBasisCreditRow.basis_id == row.id
                    )
                )
            }
            credit_cells = await session.scalar(
                select(func.count())
                .select_from(AcceptedCapitalBasisCreditCellRow)
                .where(AcceptedCapitalBasisCreditCellRow.basis_id == row.id)
            )
            attempts = {
                a.attempt_id: (a.symbol, a.classification)
                for a in await session.scalars(
                    select(AcceptedCapitalBasisAttemptRow).where(
                        AcceptedCapitalBasisAttemptRow.basis_id == row.id
                    )
                )
            }
        return Basis(row, symbols, cells, credits, credit_cells, attempts)

    async def attempt(
        self,
        *,
        amount: str,
        cell: str = "a30",
        symbol: str = "fUST",
        outcome: str = "ack",
        venue_offer_id: str | None = None,
        started_at_ms: int = 0,
    ) -> UUID:
        assert self.basis_id is not None, "an attempt is authorized against a basis"
        decision_id = f"decision-{next(_IDS)}"
        attempt_id = uuid4()
        async with self.factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO execution_decisions(decision_id, account_id, "
                    "exchange_account_id, reconcile_id, cell_id, "
                    "symbol, signal_correlation_id, outcome, signal_rate, amount_usdt, "
                    "duration_days, model_evidence, safety_result, execution_policy, "
                    "service_version, config_hash, occurred_at_ms, recorded_at_ms) VALUES "
                    "(:d, 'account', :a, 'r', :cell, :symbol, :d, 'submitted', 0, "
                    ":amount, 2, '{}', '{}', 'policy', 'test', 'hash', 0, 0)"
                ),
                {"d": decision_id, "a": _A, "cell": cell, "symbol": symbol, "amount": amount},
            )
            await record_attempt(
                session,
                SCOPE,
                Attempt(
                    attempt_id,
                    decision_id,
                    symbol,
                    cell,
                    {"amount": amount, "symbol": symbol},
                    self.basis_id,
                    UUID(_P),
                    {},
                    started_at_ms,
                ),
            )
        async with self.factory.begin() as session:
            await JOURNAL.record_outcome(
                session,
                SCOPE,
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
        self, attempt_id: UUID, action: str, venue_offer_id: str | None = None
    ) -> None:
        async with self.factory.begin() as session:
            observation_id = await session.scalar(
                select(AcceptedCapitalBasisRow.observation_id).where(
                    AcceptedCapitalBasisRow.id == self.basis_id
                )
            )
            await JOURNAL.record_resolution(
                session,
                SCOPE,
                Resolution(
                    uuid4(),
                    "fUST",
                    action,  # type: ignore[arg-type]
                    venue_offer_id,
                    observation_id,
                    "system",
                    "test",
                    10,
                    "test",
                    {},
                    attempt_id=attempt_id,
                ),
            )


@pytest_asyncio.fixture
async def ledger(ledger_db):  # noqa: F811
    with ledger_db.begin() as conn:
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','ledger')"),
            {"id": _A},
        )
        conn.execute(
            text(
                "INSERT INTO capital_policy_revisions(id, exchange_account_id, "
                "symbol, revision, schema_version, policy, digest, "
                "source) VALUES (:p, :a, 'fUST', 1, 1, '{}', 'p', '{}')"
            ),
            {"p": _P, "a": _A},
        )
    engine = _engine(ledger_db)
    try:
        yield Ledger(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_policy_head_does_not_stop_acceptance(ledger) -> None:
    """Facts only: policy is applied per symbol at read time, never here."""
    async with ledger.factory.begin() as session:
        heads = await session.scalar(text("SELECT count(*) FROM capital_policy_heads"))
    assert heads == 0
    basis = await ledger.accept(_observation("700"))
    assert not basis.blocked
    assert basis.row.schema_version == 2
    assert basis.symbols["fUST"].available == Decimal("700")
    assert basis.row.attempt_seq_high_water == 0
    assert len(basis.row.digest) == 64


@pytest.mark.asyncio
async def test_lent_credit_counts_in_the_symbol_and_in_no_cell(ledger) -> None:
    """A cell with an offer resting and money lent holds the offer's remainder only."""
    await ledger.accept(_observation())
    await ledger.attempt(amount="100", venue_offer_id="101")
    basis = await ledger.accept(
        _observation(
            "600",
            offers=(_offer("101", "100", created=101_200),),
            credits=(_credit("c1", "300", opening=101_100),),
        )
    )
    fust = basis.symbols["fUST"]
    assert (fust.offered, fust.credits) == (Decimal("100"), Decimal("300"))
    assert basis.credits == {"c1": Decimal("300")}
    assert basis.cells == {"a30": Decimal("100")}
    assert not basis.blocked


@pytest.mark.asyncio
async def test_a_fully_lent_offer_leaves_its_cell_empty_and_writes_no_credit_cell(ledger) -> None:
    """The trade names our offer exactly; the credit still belongs to no cell."""
    await ledger.accept(_observation())
    await ledger.attempt(amount="200", venue_offer_id="101")
    await ledger.accept(_observation("800", offers=(_offer("101", "200"),)))
    basis = await ledger.accept(
        _observation(
            "800",
            credits=(_credit("c1", "200", opening=101_150),),
            trades=(_trade("101", "200"),),
        )
    )
    assert basis.symbols["fUST"].credits == Decimal("200")
    assert basis.cells == {}
    assert basis.credit_cells == 0
    assert not basis.blocked


@pytest.mark.asyncio
async def test_two_cells_filling_at_one_opening_each_hold_only_what_still_rests(ledger) -> None:
    await ledger.accept(_observation())
    await ledger.attempt(amount="100", venue_offer_id="101")
    await ledger.attempt(amount="300", cell="p2", venue_offer_id="102")
    await ledger.accept(_observation("600", offers=(_offer("101", "100"), _offer("102", "300"))))
    twins = (_credit("c1", "100", opening=101_150), _credit("c2", "100", opening=101_150))
    basis = await ledger.accept(
        _observation(
            "600",
            offers=(_offer("102", "300", "200"),),
            credits=twins,
            trades=(_trade("101", "100"), _trade("102", "100")),
        )
    )
    assert basis.cells == {"p2": Decimal("200")}
    assert basis.symbols["fUST"].credits == Decimal("200")
    assert basis.credit_cells == 0
    assert not basis.blocked


@pytest.mark.asyncio
async def test_each_symbols_cells_sum_to_its_offered(ledger) -> None:
    """Both are accumulated from the resting managed offers alone: foreign offers and
    credits move neither."""
    await ledger.accept(_observation())
    await ledger.attempt(amount="100", venue_offer_id="101")
    await ledger.attempt(amount="300", cell="p2", venue_offer_id="102")
    await ledger.attempt(amount="50", cell="p2", venue_offer_id="103")
    basis = await ledger.accept(
        _observation(
            "300",
            offers=(
                _offer("101", "100"),
                _offer("102", "300", "120"),
                _offer("103", "50"),
                _offer("manual-1", "40"),
            ),
            credits=(_credit("c1", "180", opening=101_100), _credit("c2", "70", opening=100_000)),
        )
    )
    fust = basis.symbols["fUST"]
    assert basis.cells == {"a30": Decimal("100"), "p2": Decimal("170")}
    assert sum(basis.cells.values()) == fust.offered == Decimal("270")
    assert (fust.credits, fust.foreign_offers) == (Decimal("250"), Decimal("40"))


@pytest.mark.asyncio
async def test_partial_fill_leaves_only_the_resting_remainder_in_the_cell(ledger) -> None:
    await ledger.accept(_observation())
    attempt_id = await ledger.attempt(amount="200", venue_offer_id="offer-1")
    basis = await ledger.accept(
        _observation(
            "800",
            offers=(_offer("offer-1", "200", "50"),),
            credits=(_credit("c1", "150", opening=101_100),),
        )
    )
    fust = basis.symbols["fUST"]
    assert (fust.offered, fust.credits) == (Decimal("50"), Decimal("150"))
    assert basis.cells == {"a30": Decimal("50")}  # never the original 200, never the lent 150
    assert basis.attempts == {attempt_id: ("fUST", "reflected")}


@pytest.mark.asyncio
async def test_an_offer_that_fills_between_snapshots_is_accounted_for(ledger) -> None:
    """First live fill (2026-09-23): gone from the book, in terminal history, lent as a loan."""
    await ledger.accept(_observation())
    attempt_id = await ledger.attempt(amount="200", venue_offer_id="offer-1")
    executed = _offer("offer-1", "200", "0", created=101_150)
    basis = await ledger.accept(
        _observation(
            "800",
            credits=(_credit("61621685", "200", kind="loan", opening=101_150),),
            history=(OfferHistory(executed, "executed", 1150),),
        )
    )
    assert not basis.blocked
    assert basis.attempts == {attempt_id: ("fUST", "reflected")}
    assert basis.symbols["fUST"].credits == Decimal("200")
    assert basis.cells == {}


@pytest.mark.parametrize("seen_as", ["history", "canceled", "gone", "trade"])
@pytest.mark.asyncio
async def test_an_ended_offer_with_contradictory_provenance_blocks_its_symbol(
    ledger, seen_as: str
) -> None:
    """Two attempts naming one venue offer is a ledger contradiction wherever the offer's
    end shows up: its terminal history row (executed, or canceled with nothing filled),
    its absence after a basis that held it, or a trade (the only place an offer that lent
    and was repaid in between is seen)."""
    await ledger.accept(_observation(usd=True))
    await ledger.attempt(amount="200", venue_offer_id="X")
    bound = await ledger.attempt(amount="200", outcome="unknown")
    await ledger.resolve(bound, "bound_to_venue", "X")
    ended = _observation(
        "800",
        usd=True,
        history={
            "history": (OfferHistory(_offer("X", "200", "0"), "executed", 1150),),
            "canceled": (OfferHistory(_offer("X", "200"), "canceled", 1150),),
        }.get(seen_as, ()),
        trades=(_trade("X", "200"),) if seen_as == "trade" else (),
    )
    if seen_as == "gone":
        held = await ledger.accept(_observation("800", offers=(_offer("X", "200"),), usd=True))
        assert "credit_provenance_conflict" not in held.reasons("fUST")
    basis = await ledger.accept(ended)
    assert "credit_provenance_conflict" in basis.reasons("fUST")
    assert basis.symbols["fUSD"].block is None
    assert basis.row.scope_block is None


@pytest.mark.asyncio
async def test_foreign_offer_is_counted_out_of_managed_exposure(ledger) -> None:
    basis = await ledger.accept(_observation("700", offers=(_offer("manual-1", "300"),)))
    fust = basis.symbols["fUST"]
    assert (fust.offered, fust.foreign_offers) == (Decimal("0"), Decimal("300"))
    assert basis.cells == {}
    assert not basis.blocked


@pytest.mark.asyncio
async def test_open_unknown_is_unresolved_for_its_symbol_only(ledger) -> None:
    await ledger.accept(_observation(usd=True))
    unknown = await ledger.attempt(amount="200", outcome="unknown")
    rejected = await ledger.attempt(amount="100", outcome="rejected")
    basis = await ledger.accept(_observation(usd=True))
    assert not basis.blocked
    assert basis.attempts == {unknown: ("fUST", "unresolved"), rejected: ("fUST", "settled")}
    assert set(basis.symbols) == {"fUST", "fUSD"}
    assert basis.row.attempt_seq_high_water == 2
    # Resolved not-accepted after that basis: the next one re-examines it.
    await ledger.resolve(unknown, "not_accepted")
    basis = await ledger.accept(_observation(usd=True))
    assert basis.attempts == {unknown: ("fUST", "settled")}
    basis = await ledger.accept(_observation(usd=True))
    assert basis.attempts == {}


@pytest.mark.asyncio
async def test_provenance_conflict_blocks_only_its_symbol(ledger) -> None:
    """R7: a fUST fact blocks fUST; fUSD and the scope stay usable."""
    await ledger.accept(_observation(usd=True))
    await ledger.attempt(amount="200", venue_offer_id="X")
    bound = await ledger.attempt(amount="200", outcome="unknown")
    await ledger.resolve(bound, "bound_to_venue", "X")
    basis = await ledger.accept(_observation("800", offers=(_offer("X", "200"),), usd=True))
    assert "offer_provenance_conflict" in basis.reasons("fUST")
    assert basis.symbols["fUSD"].block is None
    assert basis.row.scope_block is None
    fust = basis.symbols["fUST"]
    # A conflict is never read as a foreign offer.
    assert fust.offered == 0 and fust.foreign_offers == 0
    assert basis.cells == {}


@pytest.mark.asyncio
async def test_fact_without_a_symbol_row_blocks_the_scope(ledger) -> None:
    basis = await ledger.accept(
        _observation(offers=(_offer("eur-1", "10", symbol="fEUR"),), usd=True)
    )
    assert basis.row.scope_block == {"reasons": [{"reason": "missing_wallet", "symbol": "fEUR"}]}
    assert basis.symbols["fUST"].block is None and basis.symbols["fUSD"].block is None


@pytest.mark.asyncio
async def test_amount_mismatch_and_unaccounted_ack_block(ledger) -> None:
    await ledger.accept(_observation())
    await ledger.attempt(amount="200", venue_offer_id="101")
    missing = await ledger.attempt(amount="50", venue_offer_id="gone")
    basis = await ledger.accept(_observation("700", offers=(_offer("101", "300"),)))
    assert set(basis.reasons()) == {"offer_amount_conflict", "unclassifiable_commitment"}
    assert basis.attempts[missing] == ("fUST", "unresolved")
    assert basis.cells == {}


@pytest.mark.asyncio
async def test_basis_work_does_not_grow_with_history(ledger) -> None:
    classes = {mapper.class_ for mapper in _ledger_mappers()}
    observation = _observation("700", offers=(_offer("manual", "300"),))

    async def history(n: int) -> None:
        for _ in range(n):
            await ledger.attempt(amount="10", outcome="rejected")
            await ledger.accept(observation)

    async def measured() -> int:
        loads = 0

        def loaded(_target: Any, _context: Any) -> None:
            nonlocal loads
            loads += 1

        for cls in classes:
            event.listen(cls, "load", loaded)
        try:
            await ledger.accept(observation)
        finally:
            for cls in classes:
                event.remove(cls, "load", loaded)
        return loads

    await ledger.accept(observation)
    await history(2)
    small = await measured()
    await history(25)
    large = await measured()
    async with ledger.factory.begin() as session:
        assert await session.scalar(select(func.count()).select_from(AcceptedCapitalBasisRow)) == 30
    assert small > 0
    assert large == small


def _ledger_mappers():
    from bfx_funding_bot.core.db import Base

    names = {t.name for t in LEDGER_TABLES}
    return [m for m in Base.registry.mappers if m.local_table.name in names]
