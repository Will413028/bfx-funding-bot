"""Exercise migration-provisioned grants, not owner-only API fixtures: the web API reads the
ledger's uncertainties and their resolution context as ``bfx_webapi``."""
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from bfx_funding_bot.apps.read_models import select_read_models
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router

from .test_ledger_capital_reader import book  # noqa: F401 - fixture re-export
from .test_ledger_operator_resolution_pg import OPEN_AT, open_unknown
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export
from .test_ledger_unknown_resolver_pg import SCOPE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
OTHER_ACCOUNT_ID = uuid4()


async def test_migration_grants_allow_scoped_uncertainty_reads_without_ledger_writes(
    book, monkeypatch,  # noqa: F811
):
    """Missing any read grant the context needs breaks the actual list/detail reads."""
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    factory = book.factory
    account = SCOPE.exchange_account_id
    async with factory.begin() as session:
        await grant_membership(session, exchange_account_id=account, user_id="operator-1",
                               role="owner")
        session.add(ExchangeAccount(id=OTHER_ACCOUNT_ID, venue="bitfinex", label="other",
                                    lifecycle_status="active"))
        await session.flush()
        await grant_membership(session, exchange_account_id=OTHER_ACCOUNT_ID,
                               user_id="other-operator", role="owner")

    async def restricted_session():
        async with factory() as session:
            await session.execute(text("SET LOCAL ROLE bfx_webapi"))
            assert await session.scalar(text("SELECT current_user")) == "bfx_webapi"
            yield session

    app = FastAPI()
    app.include_router(build_uncertainties_router())
    app.state.read_models = select_read_models()
    app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
    app.dependency_overrides[get_session] = restricted_session
    path = f"/api/v1/exchange-accounts/{account}/uncertainties"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        empty = await client.get(path, params={"state": "open"})
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"data": []}
        uncertainty_id, ref = await open_unknown(book)
        populated = await client.get(path, params={"state": "open"})
        assert populated.status_code == 200, populated.text
        rows = populated.json()["data"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "submit_outcome_unknown"
        assert rows[0]["uncertaintyId"] == str(uncertainty_id)
        context = rows[0]["resolutionContext"]
        assert context["evidenceRef"] == ref
        assert (context["candidateCount"], context["candidateVenueOfferIds"],
                context["unavailableReason"]) == (0, [], None)
        assert context["queryFinishedAtMs"] > context["queryStartedAtMs"] >= OPEN_AT
        detail = await client.get(f"{path}/{uncertainty_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["resolutionContext"] == context
        denied = await client.get(f"/api/v1/exchange-accounts/{OTHER_ACCOUNT_ID}/uncertainties")
        assert denied.status_code == 404
        denied_detail = await client.get(
            f"/api/v1/exchange-accounts/{OTHER_ACCOUNT_ID}/uncertainties/{uncertainty_id}"
        )
        assert denied_detail.status_code == 404
    async with factory() as session:
        for table in ("submission_attempt_journal", "transport_outcome_journal",
                      "ledger_observation", "quarantine_opening", "execution_resolution_journal"):
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not await session.scalar(text(
                    "SELECT has_table_privilege('bfx_webapi', :table, :privilege)"
                ), {"table": table, "privilege": privilege}), (table, privilege)
            assert not await session.scalar(text(
                "SELECT has_any_column_privilege('bfx_webapi', :table, 'INSERT,UPDATE')"
            ), {"table": table}), table
