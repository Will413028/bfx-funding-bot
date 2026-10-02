"""Ledger positions and offers read models, run as the web API's own role.

Every read runs under ``SET LOCAL ROLE bfx_webapi``: a statement outside the column
grants fails here (the offers path must not touch ``execution_decisions``, whose only
reader is the bot).

Mutation checks (one at a time; revert after each):

* ``lent`` = credits + unattributed_credits: ``test_positions_*``.
* positions from an older basis: ``test_positions_*``.
* ``nCredits`` counted across bases: ``test_positions_*``.
* a foreign or conflicted mirror row listed: ``test_offers_*_set``.
* ``offerKey`` = ``venue_offer_id``: ``test_offers_*_set``, ``test_offer_key_*``.
* the offers path calling ``reads._correlations``: every offers test (permission denied).
* an attempt with an outcome shown as ``pending``: ``test_offers_*_set``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import OfferView, Outcome, PositionView, Scope
from bfx_funding_bot.modules.ledger.wiring import build_operator_reads

from .test_ledger_basis import _credit, _observation, _offer
from .test_ledger_capital_reader import JOURNAL, Book
from .test_ledger_capital_reader import book as book
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = pytest.mark.integration
READS = build_operator_reads()
ACTIVE = ("pending", "unknown", "claimed")


@asynccontextmanager
async def _webapi(book: Book) -> AsyncIterator[AsyncSession]:
    async with book.factory() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE bfx_webapi"))
        assert await session.scalar(text("SELECT current_user")) == "bfx_webapi"
        yield session


async def _positions(book: Book, scope: Scope | None = None) -> tuple[PositionView, ...]:
    async with _webapi(book) as session:
        return await READS.list_positions(session, scope or book.scope)


async def _offers(book: Book, states: tuple[str, ...] = ACTIVE) -> dict[str, OfferView]:
    async with _webapi(book) as session:
        views = await READS.list_offers(session, book.scope, states=states)
    keys = [view.offer_key for view in views]
    assert len(keys) == len(set(keys))
    return {view.offer_key: view for view in views}


async def _started_at_ms(book: Book, attempt: UUID) -> int:
    async with book.factory() as session:
        value = await session.scalar(
            text("SELECT started_at_ms FROM submission_attempt_journal WHERE attempt_id = :a"),
            {"a": attempt},
        )
    assert value is not None
    return int(value)


# --- positions -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positions_use_only_the_latest_basis(book) -> None:
    assert await _positions(book) == ()  # no basis, no positions
    await book.accept(
        _observation(
            "1000",
            credits=(
                _credit("old-1", "100", opening=101_100),
                _credit("old-2", "100", opening=101_100),
                _credit("old-3", "100", opening=101_100),
            ),
            usd=True,
        )
    )
    first = await _positions(book)
    assert [p.symbol for p in first] == ["fUSD", "fUST"]
    assert {p.symbol: p.n_credits for p in first} == {"fUSD": 0, "fUST": 3}

    await book.attempt("100", venue_offer_id="101")
    await book.accept(
        _observation(
            "600",
            offers=(_offer("101", "100", created=101_200),),
            credits=(_credit("c1", "300", opening=101_100),),
            usd=True,
        ),
        started=10, finished=11, confirmed=12,
    )
    positions = {p.symbol: p for p in await _positions(book)}
    fust = positions["fUST"]
    # The latest basis only: its own available, its single credit, its own stamp.
    assert (fust.available, fust.offered, fust.lent) == (
        Decimal("600"), Decimal("100"), Decimal("300"),
    )
    # The 300 credit is unattributed: a SUBSET of lent, never an addend.
    assert fust.unattributed_lent == Decimal("300")
    assert fust.n_credits == 1
    assert fust.last_updated_ms == fust.last_reconciled_at_ms
    assert fust.last_updated_ms is not None and fust.last_updated_ms > 0
    assert positions["fUSD"].n_credits == 0
    assert positions["fUSD"].available == Decimal("5")


@pytest.mark.asyncio
async def test_positions_are_scoped(book) -> None:
    await book.accept(_observation("1000", credits=(_credit("c1", "50", opening=101_100),)))
    other = Scope(book.scope.exchange_account_id, "ci2")
    assert await _positions(book, other) == ()
    assert len(await _positions(book)) == 1


# --- offers ----------------------------------------------------------------------


class Offers:
    pending: UUID
    open_unknown: UUID
    resolved_unknown: UUID
    claimed: UUID
    claimed_no_stamp: UUID
    conflict_ack: UUID
    conflict_bound: UUID
    rejected: UUID
    unreflected: UUID


@pytest_asyncio.fixture
async def offers(book: Book) -> Offers:
    await book.accept()
    o = Offers()
    o.open_unknown = await book.attempt("22", outcome="unknown")
    o.resolved_unknown = await book.attempt("33", outcome="unknown")
    await book.resolve("not_accepted", attempt_id=o.resolved_unknown)
    o.claimed = await book.attempt("44", venue_offer_id="m-1")
    o.claimed_no_stamp = await book.attempt("55", venue_offer_id="m-2")
    o.conflict_ack = await book.attempt("66", venue_offer_id="c-1")
    o.conflict_bound = await book.attempt("77", outcome="unknown")
    await book.resolve("bound_to_venue", attempt_id=o.conflict_bound, venue_offer_id="c-1")
    o.rejected = await book.attempt("88", outcome="rejected")
    await book.accept(
        _observation(
            "500",
            offers=(
                replace(_offer("m-1", "44"), mts_updated=777_000),
                _offer("m-2", "55"),
                _offer("c-1", "66"),
                _offer("foreign-1", "99"),
            ),
        ),
        started=10, finished=11, confirmed=12,
    )
    # After the basis (a query is refused while an attempt has no outcome): in flight,
    # and acknowledged but not yet in any accepted snapshot.
    o.pending = await book.attempt("11", outcome=None)
    o.unreflected = await book.attempt("12", venue_offer_id="not-yet")
    return o


@pytest.mark.asyncio
async def test_offers_managed_set(book, offers) -> None:
    listed = await _offers(book)
    assert set(listed) == {
        str(offers.pending),
        str(offers.open_unknown),
        str(offers.claimed),
        str(offers.claimed_no_stamp),
        str(offers.unreflected),
    }, "foreign, conflicted, resolved, rejected attempts must not be listed"

    pending = listed[str(offers.pending)]
    assert (pending.state, pending.venue_offer_id, pending.size_usdt, pending.symbol) == (
        "pending", None, Decimal("11"), "fUST",
    )
    started = await _started_at_ms(book, offers.pending)
    assert pending.last_updated_ms == pending.occurred_at_ms == started

    unknown = listed[str(offers.open_unknown)]
    assert (unknown.state, unknown.venue_offer_id, unknown.size_usdt) == (
        "unknown", None, Decimal("22"),
    )
    assert unknown.last_updated_ms == 0  # the outcome's completed_at_ms

    claimed = listed[str(offers.claimed)]
    assert (claimed.state, claimed.venue_offer_id, claimed.size_usdt) == (
        "claimed", "m-1", Decimal("44"),
    )
    assert claimed.last_updated_ms == 777_000  # the mirror's mts_updated

    no_stamp = listed[str(offers.claimed_no_stamp)]
    assert (no_stamp.state, no_stamp.venue_offer_id) == ("claimed", "m-2")
    assert no_stamp.last_updated_ms == 0  # falls back to the ack's completed_at_ms

    unreflected = listed[str(offers.unreflected)]
    assert (unreflected.state, unreflected.venue_offer_id) == ("claimed", "not-yet")
    assert unreflected.last_updated_ms == 0
    assert unreflected.occurred_at_ms == await _started_at_ms(book, offers.unreflected)
    # Never keyed by the venue offer id.
    assert not {"m-1", "m-2", "c-1", "foreign-1", "not-yet"} & set(listed)


@pytest.mark.asyncio
async def test_offers_are_ordered_newest_update_first(book, offers) -> None:
    async with _webapi(book) as session:
        views = await READS.list_offers(session, book.scope, states=ACTIVE)
    stamps = [view.last_updated_ms for view in views]
    assert stamps == sorted(stamps, reverse=True)
    assert views[0].offer_key == str(offers.claimed)


@pytest.mark.asyncio
async def test_offers_state_filter_and_history(book, offers) -> None:
    assert set(await _offers(book, ("pending",))) == {str(offers.pending)}
    assert set(await _offers(book, ("unknown",))) == {str(offers.open_unknown)}
    # History (released / failed) is not read from the ledger yet.
    assert await _offers(book, ("released",)) == {}
    assert await _offers(book, ("failed",)) == {}
    assert await _offers(book, ("released", "failed")) == {}


@pytest.mark.asyncio
async def test_resolved_unknown_is_not_listed_as_unknown(book, offers) -> None:
    unknown = await _offers(book, ("unknown",))
    assert str(offers.resolved_unknown) not in unknown
    assert str(offers.conflict_bound) not in await _offers(book)


@pytest.mark.asyncio
async def test_offer_key_is_stable_from_pending_to_claimed(book) -> None:
    await book.accept()
    attempt = await book.attempt("10", outcome=None)
    before = await _offers(book)
    assert set(before) == {str(attempt)} and before[str(attempt)].state == "pending"
    async with book.factory.begin() as session:
        await JOURNAL.record_outcome(
            session, book.scope, Outcome(attempt, "ack", "venue-9", None, 5, {})
        )
    after = await _offers(book)
    assert set(after) == {str(attempt)}
    assert (after[str(attempt)].state, after[str(attempt)].venue_offer_id) == ("claimed", "venue-9")
    assert after[str(attempt)].last_updated_ms == 5
    # Then the offer is live in an accepted snapshot: still the same key.
    await book.accept(_observation("990", offers=(_offer("venue-9", "10"),)), started=10, finished=11, confirmed=12)
    live = await _offers(book)
    assert set(live) == {str(attempt)} and live[str(attempt)].state == "claimed"


@pytest.mark.asyncio
async def test_offers_without_a_basis_or_attempts_are_empty(book) -> None:
    assert await _offers(book) == {}


@pytest.mark.asyncio
async def test_the_router_serves_the_ledger_wire_as_the_web_api_role(
    book, offers, monkeypatch
) -> None:
    import httpx
    from fastapi import FastAPI

    from bfx_funding_bot.apps.read_models import select_read_models
    from bfx_funding_bot.core.auth import Principal, require_operator
    from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
    from bfx_funding_bot.modules.api.deps import get_session
    from bfx_funding_bot.modules.api.projections import build_projections_router

    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    account = book.scope.exchange_account_id
    async with book.factory.begin() as session:
        await grant_membership(
            session, exchange_account_id=account, user_id="operator-1", role="owner"
        )

    async def restricted() -> AsyncIterator[AsyncSession]:
        async with _webapi(book) as session:
            yield session

    app = FastAPI()
    app.include_router(build_projections_router())
    app.state.read_models = select_read_models("ledger")
    app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
    app.dependency_overrides[get_session] = restricted
    base = f"/api/v1/exchange-accounts/{account}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(f"{base}/offers")
        assert listed.status_code == 200, listed.text
        rows = {row["offerKey"]: row for row in listed.json()["data"]}
        assert str(offers.claimed) in rows and "cid" not in rows[str(offers.claimed)]
        assert rows[str(offers.claimed)]["sizeUsdt"] == "44"
        assert rows[str(offers.claimed)]["venueOfferId"] == "m-1"
        released = await client.get(f"{base}/offers", params={"state": "released"})
        assert (released.status_code, released.json()["data"]) == (200, [])
        positions = await client.get(f"{base}/positions")
        assert positions.status_code == 200, positions.text
        fust = positions.json()["data"][0]
        assert set(fust) == {
            "symbol", "available", "offered", "lent", "unattributedLent",
            "nCredits", "lastUpdatedMs", "lastReconciledAtMs",
        }
        assert fust["symbol"] == "fUST"
