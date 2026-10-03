"""S1-3e5c: the conservation verdict computed at acceptance, stored with the basis.

Two consecutive accepted bases are compared; the verdict is read back through the
facade and, for ``unexplained_lending``, through the capital read.

Mutations (apply one at a time, run this file, revert):

2. foreign executed includes managed offers (drop ``not c.provenance.get(venue_id)`` in
   ``basis._conservation``): ``test_managed_fill_is_never_foreign_execution``.
3. foreign executed includes offers ended before the previous query started (drop the
   ``occurred_at_ms > started_at_ms`` term): ``test_foreign_offer_ended_before_the_previous_query_is_not_counted``.
5. offered excludes foreign offers (``SymbolFlow(row.offered, ...)`` / ``v.offered``):
   ``test_foreign_offer_that_filled_and_ended_is_an_offered_drop``.
6. the first basis compared with zero instead of a baseline (``previous is None`` path):
   ``test_first_basis_and_new_currency_are_baselines``.
7. the capital read ignores ``unexplained_lending`` (``_symbol_block``):
   ``test_unexplained_lending_blocks_the_capital_read_until_the_next_basis``.
Also: the ``placed`` term (drop it: ``test_offer_placed_and_filled_between_bases_is_conserved``)
and its previous-observation de-duplication (``test_placed_offer_already_in_the_previous_basis_counts_once``),
and the quarantine exclusion (``test_quarantine_member_is_not_called_foreign``).
Pure-function mutations 1, 4, 9 and mapping mutation 8: ``tests/modules/ledger/test_conservation.py``,
``tests/modules/execution/safety/test_protection.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.ledger import (
    QuarantineMember,
    SymbolConservation,
)
from bfx_funding_bot.modules.ledger.wiring import build_ledger_conservation_reader
from bfx_funding_bot.modules.trading import Available, Blocked

from .test_ledger_basis import _credit, _observation, _offer
from .test_ledger_capital_reader import JOURNAL, SCOPE, book  # noqa: F401 - fixture
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
CONSERVATION = build_ledger_conservation_reader()
D = Decimal


async def accept(ledger: Any, observation: Any = None, *, at: int) -> None:
    """Accept an observation whose query started at ``at`` (local ms)."""
    assert (
        await ledger.accept(observation, started=at, finished=at + 1, confirmed=at + 3) == "accepted"
    )


def history(venue_id: str, original: str, remaining: str, occurred: int, **kwargs: Any) -> Any:
    from bfx_funding_bot.modules.ledger import OfferHistory

    return OfferHistory(_offer(venue_id, original, remaining, **kwargs), "executed", occurred)


async def verdict(ledger: Any, symbol: str = "fUST") -> SymbolConservation:
    async with ledger.factory() as session:
        latest = await CONSERVATION.latest(session, SCOPE)
    assert latest is not None and latest.basis_id == ledger.basis_id
    return next(s for s in latest.symbols if s.symbol == symbol)


def expect(found: SymbolConservation, name: str, unexplained: str, foreign: str) -> None:
    assert (found.conservation, found.lent_unexplained, found.foreign_executed) == (
        name,
        D(unexplained),
        D(foreign),
    )


@pytest.mark.asyncio
async def test_reader_has_nothing_before_the_first_basis(book) -> None:  # noqa: F811
    async with book.factory() as session:
        assert await CONSERVATION.latest(session, SCOPE) is None


@pytest.mark.asyncio
async def test_first_basis_and_new_currency_are_baselines(book) -> None:  # noqa: F811
    # Lending already on the first basis is not compared with an empty ledger.
    await accept(book, _observation("700", credits=(_credit("c1", "300", opening=101_100),)), at=10)
    expect(await verdict(book), "baseline", "0", "0")
    # A currency that first appears later is its own baseline; fUST is compared.
    await accept(
        book,
        _observation("700", credits=(_credit("c1", "300", opening=101_100),), usd=True),
        at=1000,
    )
    expect(await verdict(book), "conserved", "0", "0")
    expect(await verdict(book, "fUSD"), "baseline", "0", "0")


@pytest.mark.asyncio
async def test_unchanged_loan_ended_and_added_offer_are_conserved(book) -> None:  # noqa: F811
    loan = _credit("c1", "300", opening=101_100)
    await accept(book, _observation("700", credits=(loan,)), at=10)
    await accept(book, _observation("700", credits=(loan,)), at=1000)
    expect(await verdict(book), "conserved", "0", "0")
    # The loan ended and its money is back in the wallet.
    await accept(book, _observation("1000"), at=2000)
    expect(await verdict(book), "conserved", "0", "0")
    # A foreign offer appearing without any lending (mutation 1 in the pure function).
    await accept(book, _observation("700", offers=(_offer("manual", "300"),)), at=3000)
    expect(await verdict(book), "conserved", "0", "0")


@pytest.mark.asyncio
async def test_foreign_offer_that_filled_and_ended_is_an_offered_drop(book) -> None:  # noqa: F811
    """A foreign offer 300 was in the first basis; it filled, so lent rose by 300.

    offered + foreign_offers is every venue offer (mutation 5 reads managed ``offered`` only).
    """
    await accept(book, _observation("700", offers=(_offer("manual", "300"),)), at=10)
    await accept(
        book,
        _observation("700", credits=(_credit("c1", "300", opening=101_100),)),
        at=1000,
    )
    expect(await verdict(book), "conserved", "0", "0")


@pytest.mark.asyncio
async def test_foreign_execution_between_bases_is_foreign_lending(book) -> None:  # noqa: F811
    await book.policy("fUST")
    await accept(book, _observation("1000"), at=10)
    foreign = history("ghost", "100", "0", occurred=1500)
    await accept(
        book,
        _observation("900", credits=(_credit("c1", "100", opening=101_100),), history=(foreign,)),
        at=1000,
    )
    expect(await verdict(book), "foreign_lending", "100", "100")
    read = await book.read(now=1100)
    assert isinstance(read.result, Available), read.result


@pytest.mark.asyncio
async def test_foreign_execution_that_covers_only_part_is_unexplained(book) -> None:  # noqa: F811
    await accept(book, _observation("1000"), at=10)
    foreign = history("ghost", "40", "0", occurred=1500)
    await accept(
        book,
        _observation("900", credits=(_credit("c1", "100", opening=101_100),), history=(foreign,)),
        at=1000,
    )
    expect(await verdict(book), "unexplained_lending", "100", "40")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("occurred", "counted"), [(999, False), (1000, False), (1001, True)]
)
async def test_foreign_offer_ended_before_the_previous_query_is_not_counted(
    book,  # noqa: F811
    occurred,
    counted,
) -> None:
    """Only offers that ended after the previous accepted query *started* (strictly) count."""
    await accept(book, _observation("1000"), at=1000)
    foreign = history("ghost", "100", "0", occurred=occurred)
    await accept(
        book,
        _observation("900", credits=(_credit("c1", "100", opening=101_100),), history=(foreign,)),
        at=2000,
    )
    if counted:
        expect(await verdict(book), "foreign_lending", "100", "100")
    else:
        expect(await verdict(book), "unexplained_lending", "100", "0")


@pytest.mark.asyncio
async def test_managed_fill_is_never_foreign_execution(book) -> None:  # noqa: F811
    """Our offer 100 filled (an offered drop of 100) but lent rose by 200: the other 100 is
    unexplained; counting our own execution as foreign would turn it into an alert."""
    await accept(book, _observation("1000"), at=10)
    await book.attempt("100", venue_offer_id="mine")
    await accept(book, _observation("900", offers=(_offer("mine", "100"),)), at=1000)
    expect(await verdict(book), "conserved", "0", "0")
    executed = history("mine", "100", "0", occurred=1500)
    await accept(
        book,
        _observation("900", credits=(_credit("c1", "200", opening=101_100),), history=(executed,)),
        at=2000,
    )
    expect(await verdict(book), "unexplained_lending", "100", "0")


@pytest.mark.asyncio
async def test_offer_placed_and_filled_between_bases_is_conserved(book) -> None:  # noqa: F811
    """Legacy's ledger held a placed offer from the submit; a basis sees it only afterwards."""
    await accept(book, _observation("1000"), at=10)
    await book.attempt("200", venue_offer_id="mine")
    await accept(
        book,
        _observation(
            "800",
            offers=(_offer("mine", "200", "50"),),
            credits=(_credit("c1", "150", opening=101_100),),
        ),
        at=1000,
    )
    expect(await verdict(book), "conserved", "0", "0")


@pytest.mark.asyncio
async def test_placed_offer_already_in_the_previous_basis_counts_once(book) -> None:  # noqa: F811
    """An UNKNOWN attempt whose offer the previous basis already counted (as foreign, for it had
    no provenance) and a resolution binds afterwards must not be added to the offered side again."""
    await accept(book, _observation("1000"), at=10)
    unknown = await book.attempt("100", outcome="unknown")
    await accept(book, _observation("900", offers=(_offer("late", "100"),)), at=1000)
    expect(await verdict(book), "conserved", "0", "0")
    await book.resolve("bound_to_venue", attempt_id=unknown, venue_offer_id="late")
    # 60 filled (offered 100 -> 40): lent rose by 160, so 100 is unexplained.
    await accept(
        book,
        _observation(
            "800",
            offers=(_offer("late", "100", "40"),),
            credits=(_credit("c1", "160", opening=101_100),),
        ),
        at=2000,
    )
    expect(await verdict(book), "unexplained_lending", "100", "0")


@pytest.mark.asyncio
async def test_quarantine_member_is_not_called_foreign(book) -> None:  # noqa: F811
    await accept(book, _observation("1000"), at=10)
    ghost = history("ghost", "100", "0", occurred=1500)
    credit = _credit("c1", "100", opening=101_100)
    await accept(book, _observation("900", credits=(credit,), history=(ghost,)), at=1000)
    expect(await verdict(book), "foreign_lending", "100", "100")
    quarantine = await book.quarantine()
    async with book.factory.begin() as session:
        await JOURNAL.add_quarantine_member(
            session,
            SCOPE,
            QuarantineMember(quarantine, "offer", "ghost", book.observation_id, D("100")),
        )
    # The same terminal offer is still in the history window: held by a quarantine, it is not
    # known to be foreign, so the 100 that lent after it is unexplained.
    await accept(
        book,
        _observation("800", credits=(credit, _credit("c2", "100", opening=101_200)), history=(ghost,)),
        at=2000,
    )
    expect(await verdict(book), "unexplained_lending", "100", "0")


@pytest.mark.asyncio
async def test_unexplained_lending_blocks_the_capital_read_until_the_next_basis(book) -> None:  # noqa: F811
    await book.policy("fUST")
    await accept(book, _observation("1000"), at=10)
    assert isinstance((await book.read()).result, Available)
    await accept(
        book, _observation("900", credits=(_credit("c1", "100", opening=101_100),)), at=1000
    )
    expect(await verdict(book), "unexplained_lending", "100", "0")
    blocked = (await book.read(now=1100)).result
    assert isinstance(blocked, Blocked)
    assert blocked.reason == "venue_lent_above_ledger"
    assert dict(blocked.evidence) == {"lent_unexplained": "100", "foreign_executed": "0"}
    # The next basis compares against this one: nothing new, so the block lifts.
    await accept(
        book, _observation("900", credits=(_credit("c1", "100", opening=101_100),)), at=2000
    )
    expect(await verdict(book), "conserved", "0", "0")
    assert isinstance((await book.read(now=2100)).result, Available)


@pytest.mark.asyncio
async def test_unexplained_lending_leads_a_fact_level_block(book) -> None:  # noqa: F811
    """A transient fact block must not hide a protection-grade verdict; it stays in the evidence."""
    await book.policy("fUST")
    await accept(book, _observation("1000"), at=10)
    # A loan with no trades evidence covering it blocks the symbol at the fact level too.
    uncovered = _credit("c1", "100", opening=900_000)
    await accept(book, _observation("900", credits=(uncovered,)), at=1000)
    blocked = (await book.read(now=1100)).result
    assert isinstance(blocked, Blocked) and blocked.reason == "venue_lent_above_ledger"
    assert ("also", "trades_range_uncovered") in blocked.evidence
