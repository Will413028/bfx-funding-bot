"""The long-tenor exposure read on a clone PostgreSQL database (plan 2026-11-22, step 1b).

The counting rules are unit-tested in ``tests/modules/ledger/test_long_tenor_exposure.py``;
this checks that the read hands them the right rows: the tenor from the decision, the
attempt's outcome and resolution, the venue's terminal row, the trades, the credit mirror.

Mutations (apply one at a time, run this file, revert):

1. drop the decision's tenor filter in ``reads._long_tenor_submissions``: the 2-day offer counts.
2. keep attempts the venue refused / UNKNOWNs resolved ``not_accepted``: the count rises.
3. bound offer ids are ignored (only ``ack`` ids looked up): the bound, unfilled one counts.
4. trades are not looked up: the canceled-but-traded offer drops out.
5. terminal rows of unaccepted observations are used: the rejected-only offer drops out.
6. a tie on ``occurred_at_ms`` picks the unfilled-looking row: the tied offer drops out.
7. drop the ``mts_opening IS NULL`` arm in ``reads._long_tenor_credits``: the undated lent
   credit drops out.
8. drop the ``period_days IS NULL`` arm in ``reads._long_tenor_credits``: the lent credit
   of unreported tenor drops out.

The equality bounds on tenor and start time are not separately killable: the counting in
``read.long_tenor`` filters both again, and its unit mutations cover them.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
import pytest_asyncio

from bfx_funding_bot.modules.execution.deployment.tenor import LongTenorLoad
from bfx_funding_bot.modules.execution.deployment_input import LedgerDeploymentInput
from bfx_funding_bot.modules.ledger import (
    CycleResult,
    LongTenorExposure,
    LongTenorQuery,
    OfferHistory,
)
from bfx_funding_bot.modules.ledger.wiring import build_ledger_managed_offers

from .test_ledger_basis import _credit, _observation, _offer, _trade
from .test_ledger_capital_reader import OBSERVATIONS, Book
from .test_ledger_capital_reader import book as book
from .test_ledger_schema_roles import ledger_db as ledger_db

pytestmark = pytest.mark.integration

SINCE = 100
QUERY = LongTenorQuery(symbol="fUST", period_days=7, since_ms=SINCE)


@pytest_asyncio.fixture
async def ledger(book: Book):
    await book.accept()
    return book


def _input(book: Book, query: LongTenorQuery | None = QUERY) -> LedgerDeploymentInput:
    return LedgerDeploymentInput(
        session_factory=book.factory, account_id=book.scope.exchange_account_id,
        offers=build_ledger_managed_offers(), long_tenor=query,
    )


async def _long(book: Book, amount: str, **kwargs) -> None:
    kwargs.setdefault("started_at_ms", SINCE)
    await book.attempt(amount, duration_days=7, **kwargs)


@pytest.mark.asyncio
async def test_offers_count_from_submissions_minus_the_ones_the_venue_ended_unfilled(
    ledger: Book,
) -> None:
    await ledger.accept(_observation(usd=True))
    # Counted: filled before any snapshot saw it; canceled with equal amounts but traded;
    # an open UNKNOWN; and (below, since it holds back observation) one with no outcome yet.
    await _long(ledger, "11", venue_offer_id="unseen")
    await _long(ledger, "12", venue_offer_id="traded")
    await _long(ledger, "13", outcome="unknown")
    # Not counted: ended unfilled (acked, and bound from an UNKNOWN); refused; UNKNOWN that
    # never went out; the strategy's tenor; before the start; another symbol.
    await _long(ledger, "21", venue_offer_id="unfilled")
    bound = await ledger.attempt("22", outcome="unknown", duration_days=7, started_at_ms=SINCE)
    await _long(ledger, "23", outcome="rejected")
    not_accepted = await ledger.attempt(
        "24", outcome="unknown", duration_days=7, started_at_ms=SINCE,
    )
    await ledger.attempt("25", venue_offer_id="two-day", started_at_ms=SINCE)
    await _long(ledger, "26", venue_offer_id="early", started_at_ms=SINCE - 1)
    await _long(ledger, "27", venue_offer_id="usd", symbol="fUSD")
    await ledger.resolve("bound_to_venue", attempt_id=bound, venue_offer_id="bound")
    await ledger.resolve("not_accepted", attempt_id=not_accepted)
    canceled = [
        OfferHistory(_offer(offer_id, amount), "canceled", 1150)
        for offer_id, amount in (
            ("unfilled", "21"), ("bound", "22"), ("traded", "12"), ("two-day", "25"),
            ("early", "26"),
        )
    ]
    assert await ledger.accept(
        _observation(usd=True, history=tuple(canceled), trades=(_trade("traded", "12"),))
    ) == "accepted"
    await _long(ledger, "14", outcome=None)

    state = await _input(ledger).long_tenor()
    exposure = state.exposure if state is not None else None

    assert exposure is not None
    assert exposure.offers_filled == 4


async def _fenced(book: Book, history: tuple[OfferHistory, ...]) -> None:
    """Record an observation the ledger stores but does not accept: a newer query fenced it.

    (An observation whose digests disagree is not stored at all; a fenced one is, with
    its history rows.)
    """
    handle = await book.begin()
    await book.begin()
    observation = replace(_observation(history=history), finished_at_ms=2)
    confirmation = replace(
        observation, finished_at_ms=4, offer_history=(), credit_history=(), trades=(),
    )
    async with book.factory.begin() as session:
        result = await OBSERVATIONS.accept(
            session, book.scope, handle, observation, confirmation, 2,
        )
    assert result.decision != "accepted"
    assert result.observation_id is not None  # stored, history rows included


@pytest.mark.asyncio
async def test_terminal_rows_come_from_accepted_observations_and_ties_keep_counting(
    ledger: Book,
) -> None:
    await ledger.accept(_observation())
    await _long(ledger, "31", venue_offer_id="filled")      # executed, amounts equal
    await _long(ledger, "32", venue_offer_id="rejected-only")
    await _long(ledger, "33", venue_offer_id="tied")
    await _fenced(ledger, (OfferHistory(_offer("rejected-only", "32"), "canceled", 1150),))
    assert await ledger.accept(_observation(history=(
        OfferHistory(_offer("filled", "31"), "executed", 1150),
        OfferHistory(_offer("tied", "33"), "canceled", 1150),
        OfferHistory(_offer("tied", "33", "20"), "canceled", 1150),
    ))) == "accepted"

    state = await _input(ledger).long_tenor()
    exposure = state.exposure if state is not None else None

    assert exposure is not None
    assert exposure.offers_filled == 3


@pytest.mark.asyncio
async def test_lent_and_ended_come_from_the_credit_mirror_since_the_start(ledger: Book) -> None:
    await ledger.accept(_observation(credits=(
        _credit("ended", "100", opening=SINCE, period=7),
        _credit("ended-loan", "90", opening=SINCE, period=7, kind="loan"),
    )))
    await ledger.accept(_observation(credits=(
        _credit("lent", "150", opening=SINCE, period=7),
        _credit("lent-loan", "40", opening=SINCE, period=7, kind="loan"),
        _credit("old", "70", opening=SINCE - 1, period=7),
        _credit("two-day", "200", opening=SINCE, period=2),
    )))

    state = await _input(ledger).long_tenor()
    exposure = state.exposure if state is not None else None

    assert exposure == LongTenorExposure(
        offers_filled=0, lent_amount=Decimal("190"), ended_credits=1,
    )


@pytest.mark.asyncio
async def test_a_lent_credit_with_no_opening_time_is_still_read(ledger: Book) -> None:
    """The start-time bound must not drop a row it cannot date (the amount limit fails closed).

    An observation without ``mts_opening`` is refused today, so the row is made directly.
    """
    from sqlalchemy import text

    await ledger.accept(_observation(credits=(
        _credit("undated", "150", opening=SINCE, period=7),
        _credit("dated", "40", opening=SINCE, period=7),
    )))
    async with ledger.factory.begin() as session:
        updated = (await session.scalars(text(
            "UPDATE venue_credit_mirror SET mts_opening = NULL "
            "WHERE venue_credit_id = 'undated' RETURNING venue_credit_id"
        ))).all()
    assert updated == ["undated"]

    state = await _input(ledger).long_tenor()

    assert state is not None
    assert state.exposure is not None
    assert state.exposure.lent_amount == Decimal("190")


@pytest.mark.asyncio
async def test_a_lent_credit_with_no_reported_tenor_is_read(ledger: Book) -> None:
    """The tenor bound must not drop money out at a tenor the venue did not report."""
    await ledger.accept(_observation(credits=(
        _credit("no-tenor", "150", opening=SINCE, period=None),
        _credit("long", "40", opening=SINCE, period=7),
        _credit("short", "999", opening=SINCE, period=2),
    )))

    state = await _input(ledger).long_tenor()

    assert state is not None
    assert state.exposure is not None
    assert state.exposure.lent_amount == Decimal("190")


@pytest.mark.asyncio
async def test_no_configured_tenor_reads_nothing(ledger: Book) -> None:
    assert await _input(ledger, None).long_tenor() is None


@pytest.mark.asyncio
async def test_managed_offers_carry_their_signal(ledger: Book) -> None:
    await ledger.accept(_observation())
    await ledger.attempt("200", venue_offer_id="managed")
    await ledger.accept(_observation(offers=(_offer("managed", "200"),)))

    (offer,) = await _input(ledger).offers(CycleResult("accepted"))

    assert offer.signal_correlation_id is not None
    assert offer.signal_correlation_id.startswith("reader-")


@pytest.mark.asyncio
async def test_resting_counts_every_managed_offer_of_the_tenor_priced_or_not(
    ledger: Book,
) -> None:
    """Reprice skips an offer without an observed rate; the one-at-a-time limit must not."""
    await _long(ledger, "150", venue_offer_id="unpriced")
    await _long(ledger, "160", venue_offer_id="priced")
    await ledger.attempt("170", venue_offer_id="two-day", started_at_ms=SINCE)
    unpriced = replace(_offer("unpriced", "150"), rate=None, rate_observed=False, period_days=7)
    priced = replace(_offer("priced", "160", "60"), period_days=7)
    assert await ledger.accept(
        _observation(offers=(unpriced, priced, _offer("two-day", "170")))
    ) == "accepted"

    state = await _input(ledger).long_tenor()

    assert state is not None
    assert state.resting == LongTenorLoad(2, Decimal("210"))
    # Step 3 withdraws from the same read: both long offers, never the two-day one.
    assert {o.venue_offer_id for o in state.withdrawable} == {"unpriced", "priced"}
    # The reprice input leaves the unpriced one out, which is why resting is read here.
    priced_ids = {o.venue_offer_id for o in await _input(ledger).offers(CycleResult("accepted"))}
    assert priced_ids == {"priced", "two-day"}


@pytest.mark.asyncio
async def test_a_failed_exposure_read_still_returns_the_offers_to_withdraw(ledger: Book) -> None:
    """A SQL error in the exposure read rolls back its savepoint only (step 3 still withdraws)."""
    from typing import cast

    from sqlalchemy import text

    from bfx_funding_bot.modules.ledger import LedgerManagedOffers

    await _long(ledger, "150", venue_offer_id="long")
    long_offer = replace(_offer("long", "150"), period_days=7)
    assert await ledger.accept(_observation(offers=(long_offer,))) == "accepted"

    class _BrokenExposure:
        def __init__(self) -> None:
            self._real = build_ledger_managed_offers()

        async def managed_live_offers(self, *args, **kwargs):
            return await self._real.managed_live_offers(*args, **kwargs)

        async def long_tenor_exposure(self, session, scope, query):
            await session.execute(text("SELECT * FROM no_such_table"))

    reader = LedgerDeploymentInput(
        session_factory=ledger.factory, account_id=ledger.scope.exchange_account_id,
        offers=cast(LedgerManagedOffers, _BrokenExposure()), long_tenor=QUERY,
    )
    state = await reader.long_tenor()

    assert state is not None
    assert state.exposure is None
    assert [o.venue_offer_id for o in state.withdrawable] == ["long"]
