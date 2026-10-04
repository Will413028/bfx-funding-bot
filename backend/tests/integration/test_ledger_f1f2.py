"""Ledger F1 / F2 regressions through the real adapter, the real REST client and PostgreSQL.

F1: venue rows decode with ``parse_float=Decimal``; the ledger persistence boundary must write
them to the JSON columns as plain strings (``format(v, "f")``, never normalized).
F2: an offer that rests across observations and then ends must leave a terminal row, however
old it is; the adapter finds it by id (the venue's start/end filter MTS_UPDATE, probed
2026-10-04). Absence never confirms terminal.

The venue is an HTTP fake (``httpx.MockTransport``) that is stateful and honours ``id``;
its windowed filter field is a knob (``update`` is the probed truth, ``create`` the other
possibility), and the by-id path must make both pass.

Mutations (apply one at a time, run this file, revert):

* the adapter's ``_json_safe`` removed (venue_observation): every flow test (the ledger refuses
  the Decimal raw); ``_json_safe`` using ``normalize()``: the adapter unit test (``-150.0``).
* the ledger converts a Decimal instead of rejecting it (``_check_json``):
  ``test_validate_refuses_raw_that_is_not_json_*``.
* by-id fetch skipped (venue_observation ``_offer_history``): the ``ends`` flow tests.
* a missing by-id row treated as terminal/complete: ``test_a_vanished_offer_the_venue_does_not_return_*``.
* offer pager back to MTS_CREATE (auth_rest): the ``update`` pager unit test.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, text

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.venue_observation import BitfinexVenueObservation
from bfx_funding_bot.modules.ledger import CreditHistory, OfferHistory, SymbolConservation
from bfx_funding_bot.modules.ledger._internal.observation import digest
from bfx_funding_bot.modules.ledger.tables import (
    LedgerObservationOfferHistoryRow,
    LedgerObservationRow,
    VenueOfferMirrorRow,
)
from bfx_funding_bot.modules.ledger.wiring import build_ledger_conservation_reader

from . import test_ledger_conservation as cons
from .test_ledger_basis import _credit, _observation, _offer, _trade
from .test_ledger_capital_reader import JOURNAL, OBSERVATIONS, SCOPE, book  # noqa: F401 - fixture
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
CONSERVATION = build_ledger_conservation_reader()
MINUTE = 60_000
CTX = AccountContext(str(SCOPE.exchange_account_id), Credentials("K", "S"), Decimal("1000"))


# --- F1 -----------------------------------------------------------------------------------

# The adapter hands the ledger JSON (exact decimals as strings); the ledger stores what it gets
# and refuses anything that is not JSON, Decimal included.
JSON_RAW = {"row": [7, "0.00021", "-150.0", ["1.50", None, "x"], {"k": "2.50"}, 3, True]}
DECIMAL_RAW = {"row": [7, Decimal("0.00021"), Decimal("-150.0"), [Decimal("1.50"), None, "x"],
                       {"k": Decimal("2.50")}, 3, True]}


def _with_raw(raw: Any) -> Any:
    offer = replace(_offer("o1", "100", "40"), raw=raw)
    ended = replace(_offer("o2", "10", "0"), raw=raw)
    credit = replace(_credit("c1", "60", opening=101_100), raw=raw)
    closed = replace(_credit("c2", "5", opening=101_100), raw=raw)
    return replace(
        _observation("900", offers=(offer,), credits=(credit,),
                     history=(OfferHistory(ended, "executed", 101_500),)),
        credit_history=(CreditHistory(closed, "closed", 101_600),),
    )


@pytest.mark.asyncio
async def test_json_raw_is_stored_and_read_back_exactly_in_all_four_tables(book) -> None:  # noqa: F811
    assert await book.accept(_with_raw(JSON_RAW)) == "accepted"
    async with book.factory() as session:
        for table in ("ledger_observation_offer", "ledger_observation_credit",
                      "ledger_observation_offer_history", "ledger_observation_credit_history"):
            raws = (await session.execute(text(f"SELECT raw FROM {table}"))).scalars().all()
            assert raws == [JSON_RAW], table


def test_digest_is_unchanged_by_this_work() -> None:
    """The digest keeps its own (normalizing) encoding: pinned from the code before F1."""
    assert digest(_with_raw(DECIMAL_RAW)) == (
        "c4ff82722f1ea135e0af130620991181018e5272e8d001b918b59e194ad2308b")


@pytest.mark.parametrize("bad", [{"x": object()}, {"x": Decimal("1.5")}, {"x": [Decimal("0")]},
                                 {"x": Decimal("NaN")}, {"x": float("inf")},
                                 {"x": {1: "int key"}}, {"x": {"set"}}])
def test_validate_refuses_raw_that_is_not_json_and_never_converts_a_decimal(
        bad: dict[str, Any]) -> None:
    from bfx_funding_bot.modules.ledger._internal.observation import _validate

    for observation in (
        _observation("1", offers=(replace(_offer("o", "1"), raw=bad),)),
        _observation("1", credits=(replace(_credit("c", "1", opening=1), raw=bad),)),
        _observation("1", history=(OfferHistory(replace(_offer("o", "1"), raw=bad), "executed", 1),)),
        replace(_observation("1"), credit_history=(
            CreditHistory(replace(_credit("c", "1", opening=1), raw=bad), "closed", 1),)),
    ):
        with pytest.raises(ValueError, match=r"raw venue evidence|non-finite"):
            _validate(observation)


# --- F2: a venue that behaves like Bitfinex ----------------------------------------------

class Venue:
    """Stateful HTTP venue. ``filter_field`` is what start/end filter in offer history."""

    def __init__(self, filter_field: str = "update") -> None:
        self.now = 50_000_000
        self.filter_field = filter_field
        self.balance = Decimal("1000")
        self.offers: dict[int, dict[str, Any]] = {}
        self.credits: dict[int, dict[str, Any]] = {}
        self.history: dict[int, dict[str, Any]] = {}
        self.trades: list[list[Any]] = []
        self.hidden: set[int] = set()  # ended offers the venue no longer lists (retention)
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.port: BitfinexVenueObservation | None = None  # the adapter of the last cycle
        self._ids = 100

    # world
    def place(self, amount: str, rate: str = "0.00021", period: int = 2) -> int:
        self._ids += 1
        self.offers[self._ids] = {"id": self._ids, "amount": Decimal(amount), "remaining": Decimal(amount),
                                  "created": self.now, "updated": self.now, "rate": rate, "period": period}
        return self._ids

    def cancel(self, offer_id: int) -> None:
        offer = self.offers.pop(offer_id)
        self.history[offer_id] = {**offer, "updated": self.now, "status": "CANCELED"}

    def fill(self, offer_id: int) -> None:
        offer = self.offers.pop(offer_id)
        self.history[offer_id] = {**offer, "remaining": Decimal(0), "updated": self.now, "status": "EXECUTED"}
        self.credits[offer_id + 5000] = {"id": offer_id + 5000, "amount": offer["amount"],
                                         "rate": offer["rate"], "period": offer["period"],
                                         "created": self.now}
        self.trades.append([offer_id * 10, "fUST", self.now, offer_id, float(offer["amount"]),
                            float(offer["rate"]), offer["period"], 1])

    def partial_fill(self, offer_id: int, amount: str) -> None:
        """Fill ``amount`` of the offer, then cancel the rest."""
        offer = self.offers.pop(offer_id)
        filled = Decimal(amount)
        self.history[offer_id] = {**offer, "remaining": offer["amount"] - filled,
                                  "updated": self.now, "status": "CANCELED (was: PARTIALLY FILLED)"}
        self.credits[offer_id + 5000] = {"id": offer_id + 5000, "amount": filled, "rate": offer["rate"],
                                         "period": offer["period"], "created": self.now}
        self.trades.append([offer_id * 10, "fUST", self.now, offer_id, float(filled),
                            float(offer["rate"]), offer["period"], 1])

    # wire
    def _offer_row(self, o: dict[str, Any], status: str) -> list[Any]:
        return [o["id"], "fUST", o["created"], o["updated"], float(-o["remaining"]), float(-o["amount"]),
                "LIMIT", None, None, 0, status, None, None, None, float(o["rate"]), o["period"], "x"]

    def _credit_row(self, c: dict[str, Any]) -> list[Any]:
        return [c["id"], "fUST", 1, c["created"], c["created"], float(-c["amount"]), 0, "ACTIVE", "FIXED",
                None, None, float(c["rate"]), c["period"], c["created"], c["created"], "x"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.now += 10
        path = request.url.path.removeprefix("/v2/auth/r/")
        body = json.loads(request.content)
        self.requests.append((path, body))
        if path == "wallets":
            lent = sum((o["remaining"] for o in self.offers.values()), Decimal(0)) + sum(
                (c["amount"] for c in self.credits.values()), Decimal(0))
            return httpx.Response(200, json=[["funding", "UST", float(self.balance), 0,
                                              float(self.balance - lent)]])
        if path == "funding/offers/fUST":
            return httpx.Response(200, json=[self._offer_row(o, "ACTIVE") for o in self.offers.values()])
        if path == "funding/credits/fUST" or path == "funding/credits":
            return httpx.Response(200, json=[self._credit_row(c) for c in self.credits.values()])
        if path in ("funding/offers", "funding/loans", "funding/loans/fUST"):
            return httpx.Response(200, json=[self._offer_row(o, "ACTIVE") for o in self.offers.values()]
                                  if path == "funding/offers" else [])
        if path == "funding/offers/fUST/hist":
            if "id" in body:
                rows = [h for i, h in self.history.items() if i in body["id"] and i not in self.hidden]
            else:
                field = "updated" if self.filter_field == "update" else "created"
                rows = [h for i, h in self.history.items()
                        if body["start"] <= h[field] <= body["end"] and i not in self.hidden]
            rows.sort(key=lambda h: -h["updated"])
            return httpx.Response(200, json=[self._offer_row(h, h["status"]) for h in rows[:body["limit"]]])
        if path == "funding/trades/fUST/hist":
            return httpx.Response(200, json=[t for t in self.trades if body["start"] <= t[2] <= body["end"]])
        if path in ("funding/credits/fUST/hist", "funding/loans/fUST/hist"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected venue request {path}")


async def cycle(ledger: Any, venue: Venue, *, request_cap: int = 48,
                restart: bool = False) -> tuple[str, Any]:
    """One full observation cycle through the real port, REST client and acceptance.

    A new adapter object per cycle keeps the test's HTTP client scoped, so the adapter's
    in-process grace state is carried over by hand, unless ``restart`` (a new process).
    """
    async with ledger.factory.begin() as session:
        handle = await JOURNAL.begin_query(session, SCOPE, venue.now)
    async with ledger.factory() as session:
        window = await OBSERVATIONS.observation_window(session, SCOPE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        port = BitfinexVenueObservation(
            rest=BitfinexAuthREST(http=http), ctx=CTX, scope=SCOPE,
            clock_ms=lambda: venue.now, request_cap=request_cap)
        if venue.port is not None and not restart:
            port._missing_since = venue.port._missing_since
        venue.port = port
        first, confirmation, confirmation_started = await port.observe(SCOPE, handle.started_at_ms, window)
    async with ledger.factory.begin() as session:
        result = await OBSERVATIONS.accept(session, SCOPE, handle, first, confirmation, confirmation_started)
    if result.decision == "accepted":
        await ledger.accept_result(result)
    return result.decision, first


async def verdict(ledger: Any) -> SymbolConservation:
    async with ledger.factory() as session:
        latest = await CONSERVATION.latest(session, SCOPE)
    assert latest is not None
    return next(s for s in latest.symbols if s.symbol == "fUST")


async def rested_offer(ledger: Any, venue: Venue, *, filter_field: str) -> int:
    """A baseline, an own offer resting across two accepted observations, ten minutes pass."""
    assert (await cycle(ledger, venue))[0] == "accepted"
    offer_id = venue.place("100")
    await ledger.attempt("100", venue_offer_id=str(offer_id), started_at_ms=venue.now)
    venue.now += 30_000
    assert (await cycle(ledger, venue))[0] == "accepted"
    venue.now += 10 * MINUTE  # far beyond the 60 s window margin
    assert (await cycle(ledger, venue))[0] == "accepted"
    venue.now += 10 * MINUTE
    return offer_id


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_field", ["update", "create"])
@pytest.mark.parametrize("ending", ["cancel", "fill"])
async def test_an_offer_older_than_the_window_that_ends_is_observed_and_conserved(
        book, filter_field: str, ending: str) -> None:  # noqa: F811
    venue = Venue(filter_field)
    offer_id = await rested_offer(book, venue, filter_field=filter_field)
    getattr(venue, ending)(offer_id)
    venue.now += 5_000
    decision, first = await cycle(book, venue)
    assert decision == "accepted"
    assert [i.offer.venue_offer_id for i in first.offer_history] == [str(offer_id)]
    assert first.offer_history[0].terminal_kind == ("canceled" if ending == "cancel" else "executed")
    found = await verdict(book)
    assert (found.conservation, found.fill_conflicts) == ("conserved", 0)
    async with book.factory() as session:
        mirror = await session.get(VenueOfferMirrorRow, (SCOPE.exchange_account_id, "ci", str(offer_id)))
        assert mirror is not None and mirror.terminal_kind == first.offer_history[0].terminal_kind
        assert not mirror.present_in_latest_accepted_snapshot


@pytest.mark.asyncio
async def test_a_vanished_offer_the_venue_does_not_return_is_incomplete_then_converges(book) -> None:  # noqa: F811
    venue = Venue()
    offer_id = await rested_offer(book, venue, filter_field="update")
    venue.cancel(offer_id)
    venue.hidden.add(offer_id)  # retention: the venue no longer lists it by id or by window
    before = await _accepted_count(book)
    decision, first = await cycle(book, venue)
    assert decision == "incomplete_or_unequal" and not first.coverage.offer_history_complete
    assert first.offer_history == ()
    assert await _accepted_count(book) == before  # absence did not confirm anything
    venue.hidden.clear()  # the venue returns it on the retry
    venue.now += 5_000
    decision, _ = await cycle(book, venue)
    assert decision == "accepted"
    assert (await verdict(book)).conservation == "conserved"


@pytest.mark.asyncio
async def test_request_cap_exceeded_is_incomplete_then_converges(book) -> None:  # noqa: F811
    venue = Venue("create")  # the window cannot see the old offer, so the lookup is needed
    offer_id = await rested_offer(book, venue, filter_field="create")
    venue.cancel(offer_id)
    before = await _accepted_count(book)
    # cap 9 -> 5 history slots after the confirmation reserve: 4 active reads + the window
    # page leave nothing for the by-id lookup.
    decision, first = await cycle(book, venue, request_cap=9)
    assert decision == "incomplete_or_unequal" and not first.coverage.offer_history_complete
    assert await _accepted_count(book) == before
    venue.now += 5_000
    decision, _ = await cycle(book, venue)
    assert decision == "accepted" and (await verdict(book)).conservation == "conserved"


async def _accepted_count(ledger: Any) -> int:
    async with ledger.factory() as session:
        return int(await session.scalar(select(func.count()).select_from(LedgerObservationRow)
                                        .where(LedgerObservationRow.accepted.is_(True))) or 0)


@pytest.mark.asyncio
async def test_r6_quarantines_only_the_unobserved_acked_attempt_not_the_old_ending_offer(book) -> None:  # noqa: F811
    venue = Venue()
    old = await rested_offer(book, venue, filter_field="update")
    venue.cancel(old)
    # An attempt the venue acknowledged but that never shows anywhere: R6's case, created
    # after the anchor. Its own history evidence is the windowed stream, untouched by the
    # id lookup of the old offer.
    attempt = await book.attempt("50", venue_offer_id="999999", started_at_ms=venue.now - 1_000)
    venue.now += 5_000
    decision, first = await cycle(book, venue)
    assert decision == "accepted" and first.coverage.offer_history_complete
    assert [i.offer.venue_offer_id for i in first.offer_history] == [str(old)]
    async with book.factory() as session:
        rows = (await session.execute(
            text("SELECT source_attempt_id, intended_amount FROM quarantine_opening"))).all()
    assert [(UUID(str(a)), b) for a, b in rows] == [(attempt, Decimal("50"))]
    assert (await verdict(book)).conservation == "conserved"
    async with book.factory() as session:
        history_rows = await session.scalar(
            select(func.count()).select_from(LedgerObservationOfferHistoryRow))
    assert history_rows == 1


# --- C+E: an end the venue never dates is judged by the offer's own trades -----------------

GRACE = 120_000


async def _hidden_end(ledger: Any, venue: Venue, ending: str, *args: str) -> int:
    """An old offer ends (cancel / fill / partial) and the venue never lists it afterwards."""
    offer_id = await rested_offer(ledger, venue, filter_field="update")
    getattr(venue, {"cancel": "cancel", "fill": "fill", "partial": "partial_fill"}[ending])(
        offer_id, *args)
    venue.hidden.add(offer_id)
    return offer_id


async def _past_grace(ledger: Any, venue: Venue) -> tuple[str, Any]:
    """First sighting (incomplete, grace starts), then the grace runs out, then the next cycle."""
    before = await _accepted_count(ledger)
    decision, first = await cycle(ledger, venue)
    assert decision == "incomplete_or_unequal" and not first.coverage.offer_history_complete
    assert await _accepted_count(ledger) == before
    venue.now += GRACE + 10_000
    return await cycle(ledger, venue)


@pytest.mark.asyncio
async def test_after_the_grace_a_plain_cancel_with_no_trades_is_conserved_and_no_terminal_is_made(
        book, monkeypatch) -> None:  # noqa: F811
    emitted: list[tuple[str, dict[str, Any]]] = []
    from bfx_funding_bot.modules.observability import alerts
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: emitted.append((event, fields)))
    venue = Venue()
    offer_id = await _hidden_end(book, venue, "cancel")
    decision, first = await _past_grace(book, venue)
    assert decision == "accepted"
    assert first.coverage.offer_history_complete and first.offer_history == ()
    assert first.unconfirmed_ends == (str(offer_id),)
    found = await verdict(book)
    assert (found.conservation, found.fill_conflicts, found.lent_unexplained) == (
        "conserved", 0, Decimal(0))
    async with book.factory() as session:
        mirror = await session.get(VenueOfferMirrorRow, (SCOPE.exchange_account_id, "ci", str(offer_id)))
        assert mirror is not None and not mirror.present_in_latest_accepted_snapshot
        assert mirror.terminal_evidence_id is None and mirror.terminal_kind is None  # nothing made up
        stored = await session.scalar(select(LedgerObservationRow.evidence).where(
            LedgerObservationRow.id == book.observation_id))
        assert stored["unconfirmed_offer_ends"] == [str(offer_id)]
    unconfirmed = [f for event, f in emitted if event == "offer_end_unconfirmed"]
    assert [(f["venue_offer_id"], f["symbol"], f["cause"]) for f in unconfirmed] == [
        (str(offer_id), "fUST", "not_returned")]
    # after the verdict: what the trades-only rule made of it, with the verdict it fed
    judged = [f for event, f in emitted if event == "offer_end_judged"]
    assert [(f["venue_offer_id"], f["outcome"], f["filled"], f["verdict"]) for f in judged] == [
        (str(offer_id), "explained", "0", "conserved")]
    # the offer left P's live set: later cycles neither ask for it nor alert again
    venue.now += 60_000
    assert (await cycle(book, venue))[0] == "accepted"
    assert len([1 for event, _ in emitted if event == "offer_end_unconfirmed"]) == 1  # left the live set
    assert (await verdict(book)).conservation == "conserved"


@pytest.mark.asyncio
@pytest.mark.parametrize(("ending", "args"), [("fill", ()), ("partial", ("40",))])
async def test_after_the_grace_a_fill_that_the_trades_explain_is_conserved(
        book, ending: str, args: tuple[str, ...]) -> None:  # noqa: F811
    venue = Venue()
    await _hidden_end(book, venue, ending, *args)
    decision, first = await _past_grace(book, venue)
    assert decision == "accepted" and first.offer_history == () and len(first.trades) == 1
    found = await verdict(book)
    assert (found.conservation, found.fill_conflicts, found.lent_unexplained) == (
        "conserved", 0, Decimal(0))


@pytest.mark.asyncio
async def test_after_the_grace_lending_that_no_trade_explains_is_unexplained(book) -> None:  # noqa: F811
    venue = Venue()
    await _hidden_end(book, venue, "cancel")
    venue.credits[7777] = {"id": 7777, "amount": Decimal("100"), "rate": "0.00021", "period": 2,
                           "created": venue.now}  # lending appears, but no trade names the offer
    decision, _ = await _past_grace(book, venue)
    assert decision == "accepted"
    found = await verdict(book)
    assert (found.conservation, found.lent_unexplained) == ("unexplained_lending", Decimal(100))


@pytest.mark.asyncio
async def test_a_restart_starts_a_new_grace(book) -> None:  # noqa: F811
    venue = Venue()
    await _hidden_end(book, venue, "cancel")
    assert (await cycle(book, venue))[0] == "incomplete_or_unequal"  # sighting 1, grace starts
    venue.now += GRACE + 10_000
    decision, first = await cycle(book, venue, restart=True)  # a new process has no sighting
    assert decision == "incomplete_or_unequal" and not first.coverage.offer_history_complete
    venue.now += GRACE + 10_000
    decision, first = await cycle(book, venue)  # the same process, past its own grace
    assert decision == "accepted" and len(first.unconfirmed_ends) == 1


@pytest.mark.asyncio
async def test_an_offer_presumed_ended_that_reappears_is_fail_closed_and_converges(book) -> None:  # noqa: F811
    venue = Venue()
    offer_id = await _hidden_end(book, venue, "cancel")
    assert (await _past_grace(book, venue))[0] == "accepted"
    assert (await verdict(book)).conservation == "conserved"
    # the venue lists it as active again, partly filled earlier than anything the trades show
    venue.offers[offer_id] = {"id": offer_id, "amount": Decimal("100"), "remaining": Decimal("40"),
                              "created": 1, "updated": venue.now, "rate": "0.00021", "period": 2}
    venue.credits[8888] = {"id": 8888, "amount": Decimal("60"), "rate": "0.00021", "period": 2,
                           "created": venue.now}
    venue.now += 60_000
    assert (await cycle(book, venue))[0] == "accepted"
    assert (await verdict(book)).conservation == "unexplained_lending"
    venue.now += 60_000
    assert (await cycle(book, venue))[0] == "accepted"
    assert (await verdict(book)).conservation == "conserved"  # the next basis is the new reference


# --- the same rule on stored facts, without an adapter: where trades cannot decide -----------

def _vanished(*, trades: tuple[Any, ...] = (), credit: str | None = None, ends: tuple[str, ...] = ("web",),
              start: int | None = None) -> Any:
    observation = _observation(
        "900", credits=(cons.loan("c1", credit),) if credit else (), trades=trades)
    if start is not None:
        observation = replace(observation, coverage=replace(
            observation.coverage, trades_requested_start_ms=start))
    return replace(observation, unconfirmed_ends=ends)


async def _judge(ledger: Any, observation: Any) -> SymbolConservation:
    await cons.accept(ledger, _observation("900", offers=(_offer("web", "100"),)), at=20_000)
    await cons.accept(ledger, observation, at=30_000)
    return await cons.verdict(ledger)


@pytest.mark.asyncio
async def test_unconfirmed_end_with_exact_trades_is_conserved(book) -> None:  # noqa: F811
    found = await _judge(book, _vanished(trades=(_trade("web", "60", mts_create=29_000),), credit="60"))
    # explained exactly; "web" has no provenance, so the fill is foreign (an alert, not a halt)
    assert (found.conservation, found.fill_conflicts, found.lent_unexplained,
            found.foreign_executed) == ("foreign_lending", 0, Decimal(0), Decimal(60))


@pytest.mark.asyncio
async def test_unconfirmed_end_not_listed_as_unconfirmed_keeps_the_conflict(book) -> None:  # noqa: F811
    found = await _judge(book, _vanished(ends=()))
    assert (found.conservation, found.fill_conflicts) == ("unexplained_lending", 1)


@pytest.mark.asyncio
async def test_unconfirmed_end_whose_trades_window_cannot_cover_it_is_a_conflict(book) -> None:  # noqa: F811
    # P began at 20_000; the trades window starts after P's start less the clock tolerance.
    found = await _judge(book, _vanished(start=18_000))
    assert (found.conservation, found.fill_conflicts) == ("unexplained_lending", 1)


@pytest.mark.asyncio
async def test_unconfirmed_end_with_a_trade_around_the_previous_read_is_a_conflict(book) -> None:  # noqa: F811
    """A trade within the clock tolerance of P's read may or may not be in P's remaining."""
    found = await _judge(book, _vanished(trades=(_trade("web", "60", mts_create=20_002),), credit="60"))
    assert (found.conservation, found.fill_conflicts) == ("unexplained_lending", 1)


@pytest.mark.asyncio
async def test_unconfirmed_end_with_trades_above_what_the_previous_read_left_is_a_conflict(book) -> None:  # noqa: F811
    found = await _judge(book, _vanished(trades=(_trade("web", "130", mts_create=29_000),), credit="100"))
    assert found.conservation == "unexplained_lending" and found.fill_conflicts == 1


@pytest.mark.asyncio
async def test_an_unconfirmed_id_that_was_never_in_the_previous_basis_changes_nothing(book) -> None:  # noqa: F811
    found = await _judge(book, _vanished(ends=("web", "stranger")))
    assert (found.conservation, found.fill_conflicts) == ("conserved", 0)


@pytest.mark.parametrize("ends", [("",), ("a", "a")])
def test_validate_refuses_malformed_unconfirmed_ends(ends: tuple[str, ...]) -> None:
    from bfx_funding_bot.modules.ledger._internal.observation import _validate

    with pytest.raises(ValueError, match="unconfirmed"):
        _validate(replace(_observation("1"), unconfirmed_ends=ends))


# --- the basis is a function of stored rows -------------------------------------------------

@pytest.mark.asyncio
async def test_recomputing_the_basis_from_stored_rows_gives_the_same_verdict(book) -> None:  # noqa: F811
    """``unconfirmed_ends`` is read back from the observation's evidence, not carried in memory."""
    from bfx_funding_bot.modules.ledger._internal import unconfirmed_ends
    from bfx_funding_bot.modules.ledger._internal.basis import write_basis

    await cons.accept(book, _observation("900", offers=(_offer("web", "100"),)), at=20_000)
    await cons.accept(book, _vanished(trades=(_trade("web", "60", mts_create=29_000),), credit="60"),
                      at=30_000)
    assert book.observation_id is not None
    columns = "symbol, conservation, lent_unexplained, foreign_executed, fill_conflicts"
    async with book.factory() as session:
        evidence = await session.scalar(select(LedgerObservationRow.evidence).where(
            LedgerObservationRow.id == book.observation_id))
        assert unconfirmed_ends.decode(evidence) == {"web"}
        stored = (await session.execute(text(
            f"SELECT {columns} FROM accepted_capital_basis_symbol s "
            "JOIN accepted_capital_basis b ON b.id = s.basis_id WHERE b.observation_id = :o "
            "ORDER BY symbol"), {"o": book.observation_id})).all()
    assert any(row[1] == "foreign_lending" and row[4] == 0 for row in stored)  # E explained it
    async with book.factory() as session:
        # A scratch transaction (rolled back): drop the basis as an owner would and write it
        # again from the stored rows alone.
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        for table in ("accepted_capital_basis_credit_cell", "accepted_capital_basis_credit",
                      "accepted_capital_basis_cell", "accepted_capital_basis_attempt",
                      "accepted_capital_basis_quarantine", "accepted_capital_basis_symbol"):
            await session.execute(text(
                f"DELETE FROM {table} WHERE basis_id IN "
                "(SELECT id FROM accepted_capital_basis WHERE observation_id = :o)"),
                {"o": book.observation_id})
        await session.execute(text("DELETE FROM accepted_capital_basis WHERE observation_id = :o"),
                              {"o": book.observation_id})
        await write_basis(session, SCOPE, book.observation_id)
        again = (await session.execute(text(
            f"SELECT {columns} FROM accepted_capital_basis_symbol s "
            "JOIN accepted_capital_basis b ON b.id = s.basis_id WHERE b.observation_id = :o "
            "ORDER BY symbol"), {"o": book.observation_id})).all()
        await session.rollback()
    assert again == stored
