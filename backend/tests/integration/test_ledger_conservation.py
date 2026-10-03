"""S1-3e5c: the id-level conservation verdict computed at acceptance, stored with the basis.

Two consecutive accepted bases are reconciled by venue id (``ledger/conservation.py``); the verdict
is read back through the facade and, for ``unexplained_lending``, through the capital read.

Mutations (apply one at a time, run this file, revert):

* count credit decreases as lending (``max(0, ...)`` in ``conservation.new_lending``):
  ``loan_end_plus_unexplained_credit``.
* use the offer's original amount instead of P's remaining for an offer present in P (in
  ``conservation_facts``: ``start_remaining``): ``foreign_active_in_previous_partly_filled_before``.
* drop the terminal-history fills (``ended``): ``foreign_ended_new``, ``own_ended_new``.
* classify a foreign fill as own (``foreign = ...``): ``foreign_active_new_partially_filled``.
* treat a negative unexplained as conserved (``abs(unexplained)``): ``fill_without_credit``.
* ignore a trades/remaining disagreement (``disagree``): ``trades_exceed_the_remaining_fill``,
  ``remaining_fill_without_trades``.
* a quarantine member counted foreign (``held``): ``test_quarantine_member_is_not_called_foreign``.
* the capital read ignores ``unexplained_lending`` (``_symbol_block``):
  ``test_unexplained_lending_blocks_the_capital_read_until_the_next_basis``.
Pure-function mutations: ``tests/modules/ledger/test_conservation.py``; the trigger mapping:
``tests/modules/execution/safety/test_protection.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.ledger import OfferHistory, QuarantineMember, SymbolConservation
from bfx_funding_bot.modules.ledger.wiring import build_ledger_conservation_reader
from bfx_funding_bot.modules.trading import Available, Blocked

from .test_ledger_basis import _credit, _observation, _offer, _trade
from .test_ledger_capital_reader import JOURNAL, SCOPE, book  # noqa: F401 - fixture
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
CONSERVATION = build_ledger_conservation_reader()
D = Decimal


async def accept(ledger: Any, observation: Any = None, *, at: int) -> None:
    """Accept an observation whose query started at ``at`` (local ms)."""
    assert (
        await ledger.accept(observation, started=at, finished=at + 1, confirmed=at + 3)
        == "accepted"
    )


def ended(
    venue_id: str, original: str, remaining: str, occurred: int, kind: str = "executed"
) -> Any:
    return OfferHistory(_offer(venue_id, original, remaining), kind, occurred)  # type: ignore[arg-type]


def loan(venue_id: str, amount: str, opening: int = 101_100) -> Any:
    return _credit(venue_id, amount, opening=opening)


async def verdict(ledger: Any, symbol: str = "fUST") -> SymbolConservation:
    async with ledger.factory() as session:
        latest = await CONSERVATION.latest(session, SCOPE)
    assert latest is not None and latest.basis_id == ledger.basis_id
    return next(s for s in latest.symbols if s.symbol == symbol)


def expect(
    found: SymbolConservation, name: str, unexplained: str, foreign: str, conflicts: int = 0
) -> None:
    assert (
        found.conservation,
        found.lent_unexplained,
        found.foreign_executed,
        found.fill_conflicts,
    ) == (name, D(unexplained), D(foreign), conflicts)


@pytest.mark.asyncio
async def test_reader_has_nothing_before_the_first_basis(book) -> None:  # noqa: F811
    async with book.factory() as session:
        assert await CONSERVATION.latest(session, SCOPE) is None


@pytest.mark.asyncio
async def test_first_basis_and_new_currency_are_baselines(book) -> None:  # noqa: F811
    # Lending already on the first basis is not compared with an empty ledger.
    await accept(book, _observation("700", credits=(loan("c1", "300"),)), at=10)
    expect(await verdict(book), "baseline", "0", "0")
    # A currency that first appears later is its own baseline; fUST is reconciled.
    await accept(book, _observation("700", credits=(loan("c1", "300"),), usd=True), at=1000)
    expect(await verdict(book), "conserved", "0", "0")
    expect(await verdict(book, "fUSD"), "baseline", "0", "0")


# --- owner x state x presence-in-previous matrix, and the cases around it ----------------------
# Each case runs to a second accepted basis (query at 1000); (verdict, lent_unexplained,
# foreign_executed, fill_conflicts) is what it must store.


async def foreign_active_new_partially_filled(ledger: Any) -> None:
    await accept(ledger, _observation("1000"), at=10)
    await accept(
        ledger,
        _observation(
            "900",
            offers=(_offer("web", "100", "40"),),
            credits=(loan("c1", "60"),),
            trades=(_trade("web", "60"),),
        ),
        at=1000,
    )


async def foreign_active_new_untouched(ledger: Any) -> None:
    await accept(ledger, _observation("1000"), at=10)
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=1000)


async def foreign_active_in_previous(ledger: Any) -> None:
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=10)
    await accept(
        ledger,
        _observation(
            "900",
            offers=(_offer("web", "100", "40"),),
            credits=(loan("c1", "60"),),
            trades=(_trade("web", "60"),),
        ),
        at=1000,
    )


async def foreign_active_in_previous_partly_filled_before(ledger: Any) -> None:
    """P already knew 30 of its fill (its remaining is 70, its credit 30): only 30 is new."""
    first = _observation(
        "900", offers=(_offer("web", "100", "70"),), credits=(loan("c0", "30", 100_000),)
    )
    await accept(ledger, first, at=10)
    await accept(
        ledger,
        _observation(
            "900",
            offers=(_offer("web", "100", "40"),),
            credits=(loan("c0", "30", 100_000), loan("c1", "30")),
            trades=(_trade("web", "30"),),
        ),
        at=1000,
    )


async def foreign_active_in_previous_with_extra_lending(ledger: Any) -> None:
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=10)
    await accept(
        ledger,
        _observation(
            "800",
            offers=(_offer("web", "100", "40"),),
            credits=(loan("c1", "60"), loan("c2", "100", 101_200)),
            trades=(_trade("web", "60"),),
        ),
        at=1000,
    )


async def foreign_ended_new(ledger: Any) -> None:
    await accept(ledger, _observation("1000"), at=10)
    await accept(
        ledger,
        _observation(
            "900",
            credits=(loan("c1", "100"),),
            history=(ended("web", "100", "0", 1500),),
            trades=(_trade("web", "100"),),
        ),
        at=1000,
    )


async def foreign_ended_in_previous(ledger: Any) -> None:
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=10)
    await accept(
        ledger,
        _observation(
            "900",
            credits=(loan("c1", "100"),),
            history=(ended("web", "100", "0", 1500),),
            trades=(_trade("web", "100"),),
        ),
        at=1000,
    )


async def foreign_ended_in_previous_with_extra_lending(ledger: Any) -> None:
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=10)
    await accept(
        ledger,
        _observation(
            "820",
            credits=(loan("c1", "100"), loan("c2", "80", 101_200)),
            history=(ended("web", "100", "0", 1500),),
            trades=(_trade("web", "100"),),
        ),
        at=1000,
    )


async def own_active_new(ledger: Any) -> None:
    await accept(ledger, _observation("1000"), at=10)
    await ledger.attempt("100", venue_offer_id="mine")
    await accept(
        ledger,
        _observation(
            "900",
            offers=(_offer("mine", "100", "40"),),
            credits=(loan("c1", "60"),),
            trades=(_trade("mine", "60"),),
        ),
        at=1000,
    )


async def own_active_in_previous(ledger: Any) -> None:
    await accept(ledger, _observation("1000"), at=10)
    await ledger.attempt("100", venue_offer_id="mine")
    await accept(ledger, _observation("900", offers=(_offer("mine", "100"),)), at=500)
    await accept(
        ledger,
        _observation(
            "900",
            offers=(_offer("mine", "100", "40"),),
            credits=(loan("c1", "60"),),
            trades=(_trade("mine", "60"),),
        ),
        at=1000,
    )


async def own_ended_new(ledger: Any) -> None:
    await accept(ledger, _observation("1000"), at=10)
    await ledger.attempt("100", venue_offer_id="mine")
    await accept(
        ledger,
        _observation(
            "900",
            credits=(loan("c1", "100"),),
            history=(ended("mine", "100", "0", 1500),),
            trades=(_trade("mine", "100"),),
        ),
        at=1000,
    )


async def own_ended_in_previous(ledger: Any) -> None:
    await accept(ledger, _observation("1000"), at=10)
    await ledger.attempt("100", venue_offer_id="mine")
    await accept(ledger, _observation("900", offers=(_offer("mine", "100"),)), at=500)
    await accept(
        ledger,
        _observation(
            "900",
            credits=(loan("c1", "100"),),
            history=(ended("mine", "100", "0", 1500),),
            trades=(_trade("mine", "100"),),
        ),
        at=1000,
    )


async def cancel_plus_unexplained_credit(ledger: Any) -> None:
    """The offer is cancelled (nothing filled) in the same interval a credit appears from nowhere."""
    await accept(ledger, _observation("1000"), at=10)
    await ledger.attempt("100", venue_offer_id="mine")
    await accept(ledger, _observation("900", offers=(_offer("mine", "100"),)), at=500)
    await accept(
        ledger,
        _observation(
            "950",
            credits=(loan("c1", "50"),),
            history=(ended("mine", "100", "100", 1500, "canceled"),),
        ),
        at=1000,
    )


async def loan_end_plus_unexplained_credit(ledger: Any) -> None:
    await accept(ledger, _observation("900", credits=(loan("c0", "100", 100_000),)), at=10)
    await accept(ledger, _observation("930", credits=(loan("c1", "70"),)), at=1000)


async def fill_without_credit(ledger: Any) -> None:
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=10)
    await accept(
        ledger,
        _observation("960", offers=(_offer("web", "100", "40"),), trades=(_trade("web", "60"),)),
        at=1000,
    )


async def lent_rise_with_no_offer_movement(ledger: Any) -> None:
    await accept(ledger, _observation("1000"), at=10)
    await accept(ledger, _observation("940", credits=(loan("c1", "60"),)), at=1000)


async def lent_rise_beside_a_resting_foreign_offer(ledger: Any) -> None:
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=10)
    await accept(
        ledger,
        _observation("840", offers=(_offer("web", "100"),), credits=(loan("c1", "60"),)),
        at=1000,
    )


async def credit_split_by_the_venue(ledger: Any) -> None:
    """One lending replaced by two credits of the same total (same period and opening)."""
    await accept(ledger, _observation("900", credits=(loan("c1", "100"),)), at=10)
    await accept(ledger, _observation("900", credits=(loan("c2", "40"), loan("c3", "60"))), at=1000)


async def credit_merge_by_the_venue(ledger: Any) -> None:
    await accept(ledger, _observation("900", credits=(loan("c2", "40"), loan("c3", "60"))), at=10)
    await accept(ledger, _observation("900", credits=(loan("c1", "100"),)), at=1000)


async def split_plus_extra_lending(ledger: Any) -> None:
    await accept(ledger, _observation("900", credits=(loan("c1", "100"),)), at=10)
    await accept(
        ledger,
        _observation("880", credits=(loan("c2", "40"), loan("c3", "80"))),
        at=1000,
    )


async def trades_exceed_the_remaining_fill(ledger: Any) -> None:
    """The offer's remaining did not move but a trade after P says it filled 30."""
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=10)
    await accept(
        ledger,
        _observation(
            "900",
            offers=(_offer("web", "100"),),
            credits=(loan("c1", "30"),),
            trades=(_trade("web", "30"),),
        ),
        at=1000,
    )


async def remaining_fill_without_trades(ledger: Any) -> None:
    """The remaining dropped by 60 and a credit shows it, but no trade says so (P began after
    the clock tolerance, so the trades window must reach back to it)."""
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=20_000)
    await accept(
        ledger,
        _observation("900", offers=(_offer("web", "100", "40"),), credits=(loan("c1", "60"),)),
        at=30_000,
    )


async def vanished_offer_without_history(ledger: Any) -> None:
    await accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=10)
    await accept(ledger, _observation("1000"), at=1000)


MATRIX = {
    func.__name__: (func, *expected)
    for func, expected in (
        (foreign_active_new_partially_filled, ("foreign_lending", "0", "60", 0)),
        (foreign_active_new_untouched, ("conserved", "0", "0", 0)),
        (foreign_active_in_previous, ("foreign_lending", "0", "60", 0)),
        (foreign_active_in_previous_partly_filled_before, ("foreign_lending", "0", "30", 0)),
        (foreign_active_in_previous_with_extra_lending, ("unexplained_lending", "100", "60", 0)),
        (foreign_ended_new, ("foreign_lending", "0", "100", 0)),
        (foreign_ended_in_previous, ("foreign_lending", "0", "100", 0)),
        (foreign_ended_in_previous_with_extra_lending, ("unexplained_lending", "80", "100", 0)),
        (own_active_new, ("conserved", "0", "0", 0)),
        (own_active_in_previous, ("conserved", "0", "0", 0)),
        (own_ended_new, ("conserved", "0", "0", 0)),
        (own_ended_in_previous, ("conserved", "0", "0", 0)),
        (cancel_plus_unexplained_credit, ("unexplained_lending", "50", "0", 0)),
        (loan_end_plus_unexplained_credit, ("unexplained_lending", "70", "0", 0)),
        (fill_without_credit, ("unexplained_lending", "-60", "60", 0)),
        (lent_rise_with_no_offer_movement, ("unexplained_lending", "60", "0", 0)),
        (lent_rise_beside_a_resting_foreign_offer, ("unexplained_lending", "60", "0", 0)),
        (credit_split_by_the_venue, ("conserved", "0", "0", 0)),
        (credit_merge_by_the_venue, ("conserved", "0", "0", 0)),
        (split_plus_extra_lending, ("unexplained_lending", "20", "0", 0)),
        # A conflicting fill is left out of the sums, so its credit stays unexplained too.
        (trades_exceed_the_remaining_fill, ("unexplained_lending", "30", "0", 1)),
        (remaining_fill_without_trades, ("unexplained_lending", "60", "0", 1)),
        (vanished_offer_without_history, ("unexplained_lending", "0", "0", 1)),
    )
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", MATRIX)
async def test_reconciliation_matrix(book, name) -> None:  # noqa: F811
    run, *expected = MATRIX[name]
    await run(book)
    expect(await verdict(book), *expected)


@pytest.mark.asyncio
async def test_quarantine_member_is_not_called_foreign(book) -> None:  # noqa: F811
    """A quarantine member is not known to be foreign: its fill explains lending without an alert."""
    await accept(book, _observation("1000"), at=10)
    ghost = ended("ghost", "100", "0", 1500)
    await accept(
        book,
        _observation(
            "900", credits=(loan("c1", "100"),), history=(ghost,), trades=(_trade("ghost", "100"),)
        ),
        at=1000,
    )
    expect(await verdict(book), "foreign_lending", "0", "100")
    quarantine = await book.quarantine()
    async with book.factory.begin() as session:
        await JOURNAL.add_quarantine_member(
            session,
            SCOPE,
            QuarantineMember(quarantine, "offer", "ghost", book.observation_id, D("100")),
        )
    await accept(
        book,
        _observation(
            "800",
            credits=(loan("c1", "100"), loan("c2", "100", 101_200)),
            history=(ghost,),
            trades=(_trade("ghost", "100"),),
        ),
        at=2000,
    )
    expect(await verdict(book), "conserved", "0", "0")


@pytest.mark.asyncio
async def test_unexplained_lending_blocks_the_capital_read_until_the_next_basis(book) -> None:  # noqa: F811
    await book.policy("fUST")
    await accept(book, _observation("1000"), at=10)
    assert isinstance((await book.read()).result, Available)
    await accept(book, _observation("900", credits=(loan("c1", "100"),)), at=1000)
    expect(await verdict(book), "unexplained_lending", "100", "0")
    blocked = (await book.read(now=1100)).result
    assert isinstance(blocked, Blocked)
    assert blocked.reason == "venue_lent_above_ledger"
    assert dict(blocked.evidence) == {
        "lent_unexplained": "100",
        "foreign_executed": "0",
        "fill_conflicts": "0",
    }
    # The next basis reconciles against this one: nothing new, so the block lifts.
    await accept(book, _observation("900", credits=(loan("c1", "100"),)), at=2000)
    expect(await verdict(book), "conserved", "0", "0")
    assert isinstance((await book.read(now=2100)).result, Available)


@pytest.mark.asyncio
async def test_fills_without_lending_block_the_capital_read(book) -> None:  # noqa: F811
    await book.policy("fUST")
    await fill_without_credit(book)
    blocked = (await book.read(now=1100)).result
    assert isinstance(blocked, Blocked) and blocked.reason == "venue_lent_above_ledger"
    assert dict(blocked.evidence)["lent_unexplained"] == "-60"


@pytest.mark.asyncio
async def test_unexplained_lending_leads_a_fact_level_block(book) -> None:  # noqa: F811
    """A transient fact block must not hide a protection-grade verdict; it stays in the evidence."""
    await book.policy("fUST")
    await accept(book, _observation("1000"), at=10)
    # A loan with no trades evidence covering it blocks the symbol at the fact level too.
    await accept(book, _observation("900", credits=(loan("c1", "100", 900_000),)), at=1000)
    blocked = (await book.read(now=1100)).result
    assert isinstance(blocked, Blocked) and blocked.reason == "venue_lent_above_ledger"
    assert ("also", "trades_range_uncovered") in blocked.evidence
