"""``GET /executions`` under the ledger, served as the web API's own role (``bfx_webapi``).

The journal above the switch's watermark, newest first, then the frozen legacy event log below
it, behind one opaque cursor (plan Q4). A seeded attempt (legacy times, before the watermark)
is not shown twice. Above the watermark the venue's fills and credit ends come from the
ledger's observations, once per venue key however many observations (fenced ones too) saw them:
fills of the bot's own offers only (named by an outcome or a resolution), credit ends of credits
only (not loans), as the archive has them.

Mutation checks (one at a time; revert after each):

* the ledger read models serve ``LegacyExecutionHistory`` (the event log) directly:
  ``test_the_ledger_history_is_the_journal_then_the_archive`` (journal rows missing).
* drop the watermark filter: the seeded attempt appears.
* the archive continuation is dropped: the last page misses the event-log rows.
* the journal reads ``normalized_payload``: every request fails under the column grant.
* drop a ``DISTINCT ON`` (fills or credit ends): ``test_venue_ends_are_shown_once_above_the_watermark``.
* drop the fills' watermark filter: the same test (the pre-switch fill appears).
* drop the fills' own-offer filter: the same test (the foreign fill appears).
* drop the resolutions from the own offers: the same test (the bound FRR fill is missing).
* drop the credit-only filter: the same test (the closed loan appears).
* read the credit history's ``raw``: every request fails under the column grant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.apps.read_models import select_read_models
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.projections import build_projections_router
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.ledger import CreditHistory, OfferHistory

from .test_ledger_basis import _credit, _observation, _offer
from .test_ledger_capital_reader import SCOPE, Book
from .test_ledger_capital_reader import book as book
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
WATERMARK = 1_000


async def _scenario(book: Book) -> dict[str, UUID]:
    """Two legacy events, a seeded attempt, then the switch and three runtime journal facts."""
    async with book.factory.begin() as session:
        for at, etype in ((100, "RESERVATION_INTENT"), (200, "ORDER_FILL")):
            session.add(EventLogRow(
                account_id="account", exchange_account_id=SCOPE.exchange_account_id,
                deployment_environment=SCOPE.deployment_environment, event_type=etype,
                venue_offer_id="legacy-1", cid=7,
                payload={"symbol": "fUST", "amount": "50", "fill_rate": 0.0002},
                occurred_at_ms=at,
            ))
    await book.accept()
    seeded = await book.attempt("40", outcome="ack", venue_offer_id="seeded-1",
                                started_at_ms=500, completed_at_ms=600)
    async with book.factory.begin() as session:
        await session.execute(text(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            f"VALUES (2, 'ledger', {WATERMARK}, 'test', 'switch')"))
    acked = await book.attempt("30", outcome="ack", venue_offer_id="o-1",
                               started_at_ms=2_000, completed_at_ms=2_100)
    unknown = await book.attempt("20", outcome="unknown", started_at_ms=3_000,
                                 completed_at_ms=3_100)
    await book.accept(started=3_150, finished=3_160, confirmed=3_170)  # the resolution's evidence
    await book.resolve("not_accepted", attempt_id=unknown, resolved_at_ms=3_200)
    return {"seeded": seeded, "acked": acked, "unknown": unknown}


async def _client(book: Book, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", SCOPE.deployment_environment)
    async with book.factory.begin() as session:
        await grant_membership(session, exchange_account_id=SCOPE.exchange_account_id,
                               user_id="operator-1", role="owner")

    async def restricted() -> AsyncIterator[AsyncSession]:
        async with book.factory() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE bfx_webapi"))
            yield session

    app = FastAPI()
    app.include_router(build_projections_router())
    app.state.read_models = select_read_models("ledger")
    app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
    app.dependency_overrides[get_session] = restricted
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _path() -> str:
    return f"/api/v1/exchange-accounts/{SCOPE.exchange_account_id}/executions"


async def _pages(client: httpx.AsyncClient, **params: Any) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = []
    before: str | None = None
    while True:
        query = {**params, **({"before": before} if before is not None else {})}
        response = await client.get(_path(), params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body["data"])
        before = body["pagination"]["nextBefore"]
        assert body["pagination"]["hasMore"] is (before is not None)
        if before is None:
            return pages
        assert isinstance(before, str)


async def test_the_ledger_history_is_the_journal_then_the_archive(book, monkeypatch) -> None:
    await _scenario(book)
    async with await _client(book, monkeypatch) as client:
        pages = await _pages(client, limit=2)
    rows = [row for page in pages for row in page]
    assert [len(page) for page in pages] == [2, 2, 2, 1]
    assert [(row["eventType"], row["occurredAtMs"], row["venueOfferId"], row["amount"])
            for row in rows] == [
        ("UNCERTAINTY_MARKED_NOT_ACCEPTED", 3_200, None, "20"),
        ("SUBMIT_OUTCOME_UNKNOWN", 3_100, None, "20"),
        ("RESERVATION_INTENT", 3_000, None, "20"),
        ("RESERVATION_CLAIMED", 2_100, "o-1", "30"),
        ("RESERVATION_INTENT", 2_000, None, "30"),
        # Below the watermark: the frozen event log, not the seeded copy (500/600).
        ("ORDER_FILL", 200, "legacy-1", "50"),
        ("RESERVATION_INTENT", 100, "legacy-1", "50"),
    ], rows
    assert rows[1]["symbol"] == "fUST" and rows[1]["cid"] is None
    assert rows[5]["cid"] == 7 and rows[5]["rate"] == 0.0002
    assert len({row["eventKey"] for row in rows}) == len(rows)


async def test_one_page_spans_the_watermark_and_the_filter_applies_to_both(
    book, monkeypatch,
) -> None:
    await _scenario(book)
    async with await _client(book, monkeypatch) as client:
        (page,) = await _pages(client, limit=50)
        intents = await _pages(client, limit=1, event_type="RESERVATION_INTENT")
    assert [row["occurredAtMs"] for row in page] == [3_200, 3_100, 3_000, 2_100, 2_000, 200, 100]
    assert [[row["occurredAtMs"] for row in p] for p in intents] == [[3_000], [2_000], [100]]


async def test_a_foreign_cursor_is_refused(book, monkeypatch) -> None:
    await _scenario(book)
    async with await _client(book, monkeypatch) as client:
        for cursor in ("12", "j.%%%", "j.MTox", "x.MTox"):
            response = await client.get(_path(), params={"before": cursor})
            assert (response.status_code, response.json()["detail"]) == (
                422, "invalid_cursor"), cursor


async def _venue_ends(book: Book) -> None:
    """``_scenario``, an UNKNOWN attempt bound to a venue offer, then two overlapping observations
    of the venue's terminal history: the first fenced (a newer query began), the second accepted.
    """
    await _scenario(book)
    bound = await book.attempt("25", outcome="unknown", started_at_ms=2_200,
                               completed_at_ms=2_300)
    await book.resolve("bound_to_venue", attempt_id=bound, venue_offer_id="f-bound",
                       resolved_at_ms=2_400)
    # o-1: acked after the switch; f-bound: named only by the resolution (an FRR offer).
    fill = OfferHistory(_offer("o-1", "60", "0", created=2_000), "executed", 2_500)
    early = OfferHistory(_offer("seeded-1", "70", "0", created=800), "executed", 900)
    canceled = OfferHistory(_offer("c-1", "80", created=2_000), "canceled", 2_550)
    foreign = OfferHistory(_offer("foreign-1", "85", "0", created=2_000), "executed", 2_600)
    frr = _offer("f-bound", "90", "0", created=2_000)
    frr_fill = OfferHistory(replace(frr, rate=None, rate_observed=False), "executed", 2_800)
    closed = CreditHistory(_credit("77", "60", opening=2_500), "closed", 2_700)
    loan = CreditHistory(_credit("88", "40", opening=2_500, kind="loan"), "closed", 2_650)
    fenced = await book.begin(3_300)
    current = await book.begin(3_310)
    assert await book.accept(
        _observation(history=(early, fill), credit_history=(closed,)),
        handle=fenced, started=3_300, finished=3_320, confirmed=3_330,
    ) == "fenced"
    assert await book.accept(
        _observation(history=(fill, fill, canceled, foreign, frr_fill),
                     credit_history=(closed, loan)),
        handle=current, started=3_310, finished=3_340, confirmed=3_350,
    ) == "accepted"


async def test_venue_ends_are_shown_once_above_the_watermark(book, monkeypatch) -> None:
    await _venue_ends(book)
    async with await _client(book, monkeypatch) as client:
        (page,) = await _pages(client, limit=50)
        walked = await _pages(client, limit=1)
    assert [(row["eventType"], row["occurredAtMs"], row["venueOfferId"], row["amount"])
            for row in page] == [
        ("UNCERTAINTY_MARKED_NOT_ACCEPTED", 3_200, None, "20"),
        ("SUBMIT_OUTCOME_UNKNOWN", 3_100, None, "20"),
        ("RESERVATION_INTENT", 3_000, None, "20"),
        ("ORDER_FILL", 2_800, "f-bound", "90"),
        # The closed loan (2_650) is not a credit end; the foreign offer's fill (2_600) and
        # the canceled offer (2_550) are no fills of the bot's.
        ("CREDIT_CLOSED", 2_700, None, "60"),
        # Seen by both observations (and twice in the second): one fill. The seeded offer's
        # fill before the switch (900) is the archive's, which lacks it.
        ("ORDER_FILL", 2_500, "o-1", "60"),
        ("UNCERTAINTY_BOUND_TO_VENUE_OFFER", 2_400, "f-bound", "25"),
        ("SUBMIT_OUTCOME_UNKNOWN", 2_300, None, "25"),
        ("RESERVATION_INTENT", 2_200, None, "25"),
        ("RESERVATION_CLAIMED", 2_100, "o-1", "30"),
        ("RESERVATION_INTENT", 2_000, None, "30"),
        ("ORDER_FILL", 200, "legacy-1", "50"),
        ("RESERVATION_INTENT", 100, "legacy-1", "50"),
    ], page
    # The archive's fields: symbol, rate (an FRR fill has none, none is invented), no cid.
    assert [(row["symbol"], row["rate"], row["cid"]) for row in page[3:6]] == [
        ("fUST", None, None), ("fUST", 0.0001, None), ("fUST", 0.0001, None)]
    assert len({row["eventKey"] for row in page}) == len(page)
    # One row per page: every cursor, the fill (3) and credit-end (4) ranks included, resumes
    # exactly after its row.
    assert [row for p in walked for row in p] == page


async def test_the_fill_filter_walks_from_the_observations_into_the_archive(
    book, monkeypatch,
) -> None:
    await _venue_ends(book)
    async with await _client(book, monkeypatch) as client:
        fills = await _pages(client, limit=1, event_type="ORDER_FILL")
        ends = await _pages(client, limit=1, event_type="CREDIT_CLOSED")
    assert [[(row["occurredAtMs"], row["venueOfferId"]) for row in p] for p in fills] == [
        [(2_800, "f-bound")], [(2_500, "o-1")], [(200, "legacy-1")]]
    assert [[row["occurredAtMs"] for row in p] for p in ends] == [[2_700]]


async def test_the_web_api_cannot_read_the_credit_history_payload(book) -> None:
    await _venue_ends(book)
    async with book.factory() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE bfx_webapi"))
        assert await session.scalar(text(
            "SELECT count(*) FROM ledger_observation_credit_history "
            "WHERE terminal_kind = 'closed'")) == 3
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with book.factory() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE bfx_webapi"))
            await session.execute(text("SELECT raw FROM ledger_observation_credit_history"))
