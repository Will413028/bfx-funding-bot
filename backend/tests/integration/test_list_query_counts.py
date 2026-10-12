"""The operator list endpoints issue the same number of statements for one row as for many.

Each list is requested over HTTP as the web API's own role (``bfx_webapi``) with one row
seeded, then again after more rows are added; the statement count must not grow with
the row count, so a per-row lookup (N+1) fails here by endpoint.

Mutation check: make a list read one extra statement per row (for example, look up each
position's credits separately) — that endpoint's count differs between the two seeds.

``KNOWN_PER_ROW`` names the lists that still read per row, each with its defect. The test
fails once such a list stops growing, so the entry is removed together with the fix.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.apps.read_models import build_read_models
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    create_exchange_account_credential,
    grant_membership,
)
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.projections import build_projections_router
from bfx_funding_bot.modules.api.routers import build_router
from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router

from .test_ledger_basis import _credit, _observation, _offer
from .test_ledger_capital_reader import Book
from .test_ledger_capital_reader import book as book
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = pytest.mark.integration
OPERATOR = "operator-1"
MANY = 5
# The open-uncertainty list builds each row's resolution context with its own reads
# (``modules/api/uncertainties.py`` ``_resolution_context`` inside the row loop).
KNOWN_PER_ROW = {"uncertainties": "F14"}


@asynccontextmanager
async def _webapi(book: Book) -> AsyncIterator[AsyncSession]:
    async with book.factory() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE bfx_webapi"))
        yield session


def _app(book: Book) -> FastAPI:
    async def restricted() -> AsyncIterator[AsyncSession]:
        async with _webapi(book) as session:
            yield session

    app = FastAPI()
    for router in (
        build_router(), build_api_keys_router(), build_projections_router(),
        build_uncertainties_router(),
    ):
        app.include_router(router)
    app.state.read_models = build_read_models()
    app.dependency_overrides[require_operator] = lambda: Principal(OPERATOR, None, "admin")
    app.dependency_overrides[get_session] = restricted
    return app


async def _seed(book: Book, start: int, stop: int) -> None:
    """Rows ``start..stop-1`` of every listed kind; the observation names all rows so far."""
    for i in range(start, stop):
        at = 2_000 + 10 * i  # above the switch's watermark, so /executions lists it
        await book.attempt("10", venue_offer_id=f"o-{i}", started_at_ms=at, completed_at_ms=at + 1)
        await book.quarantine()
    await book.accept(
        _observation(
            "1000",
            offers=tuple(_offer(f"o-{i}", "10") for i in range(stop)),
            credits=tuple(_credit(f"c-{i}", "10", opening=101_100) for i in range(stop)),
        ),
        started=10 + stop, finished=11 + stop, confirmed=12 + stop,
    )
    async with book.factory.begin() as session:
        for i in range(start, stop):
            await create_exchange_account_credential(
                session, exchange_account_id=book.scope.exchange_account_id, venue="bitfinex",
                label=f"key-{i}", api_key=f"k-{i}", secret_ciphertext=b"c", secret_nonce=b"n",
                wrapped_dek=b"d", dek_nonce=b"n", key_version=1,
            )
            other = uuid4()
            await session.execute(
                text("INSERT INTO exchange_accounts(id, venue, label) VALUES (:id, 'bitfinex', :l)"),
                {"id": other, "l": f"extra-{i}"},
            )
            await grant_membership(
                session, exchange_account_id=other, user_id=OPERATOR, role="owner",
            )


async def _counts(book: Book, paths: dict[str, str]) -> dict[str, tuple[int, int]]:
    """Per endpoint: (statements issued, rows returned)."""
    engine = book.factory.kw["bind"]
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        statements.append(statement)

    result: dict[str, tuple[int, int]] = {}
    transport = httpx.ASGITransport(app=_app(book))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for name, path in paths.items():
            statements.clear()
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            try:
                response = await client.get(path)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record)
            assert response.status_code == 200, (name, response.text)
            result[name] = (len(statements), _rows(response.json()))
    return result


def _rows(body: dict[str, Any]) -> int:
    data = body["data"]
    assert isinstance(data, list), sorted(body)
    return len(data)


@pytest.mark.asyncio
async def test_list_endpoints_do_not_query_per_row(book, monkeypatch) -> None:
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    account = book.scope.exchange_account_id
    async with book.factory.begin() as session:
        await grant_membership(session, exchange_account_id=account, user_id=OPERATOR, role="owner")
    await book.accept()
    async with book.factory.begin() as session:
        await session.execute(text(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            "SELECT max(epoch_seq) + 1, 'ledger', 1000, 'test', 'switch' "
            "FROM capital_authority_epoch"))
    base = f"/api/v1/exchange-accounts/{account}"
    paths = {
        "positions": f"{base}/positions",
        "offers": f"{base}/offers",
        "executions": f"{base}/executions",
        "credentials": f"{base}/credentials",
        "uncertainties": f"{base}/uncertainties?state=open",
        "exchange-accounts": "/api/v1/exchange-accounts",
    }

    await _seed(book, 0, 1)
    one = await _counts(book, paths)
    await _seed(book, 1, MANY)
    many = await _counts(book, paths)

    # The seed must actually add rows to each list, or equal counts prove nothing. Positions
    # are one row per symbol; the credits behind the one fUST row grow instead.
    assert [name for name in paths if name != "positions" and many[name][1] <= one[name][1]] == []
    flat = [name for name in paths if name not in KNOWN_PER_ROW]
    assert {name: many[name][0] for name in flat} == {name: one[name][0] for name in flat}
    assert [name for name in KNOWN_PER_ROW if many[name][0] == one[name][0]] == []
