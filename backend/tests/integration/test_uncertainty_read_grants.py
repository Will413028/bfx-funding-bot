"""Exercise migration-provisioned grants, not owner-only API fixtures."""

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import ReservationIntent, ReservationUnknown
from tests.modules.api.test_uncertainties_router import (
    ACCOUNT_ID,
    OTHER_ACCOUNT_ID,
    SCID,
    _append_snapshot,
    _attempt,
    _decision,
    _seed_account,
    _snapshot,
)
from tests.pg_templates import upgrade_head

pytestmark = pytest.mark.integration


def _build_read_schema(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN CREATE ROLE bfx_webapi; END IF; END $$")
            conn.exec_driver_sql("ALTER ROLE bfx_webapi NOSUPERUSER NOCREATEROLE NOINHERIT")
    finally:
        engine.dispose()
    # The role must exist before migrations run, as on a fresh host.
    upgrade_head(url)


@pytest_asyncio.fixture
async def migrated_sessions(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("uncertainty_read_grants", _build_read_schema))
    engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_migration_grants_allow_scoped_uncertainty_reads_without_execution_writes(
    migrated_sessions, monkeypatch,
):
    """Missing either projection/attempt SELECT breaks actual list/detail reads."""
    from decimal import Decimal

    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    factory = migrated_sessions
    async with factory.begin() as session:
        await _seed_account(session, ACCOUNT_ID, "operator-1")
        await _seed_account(session, OTHER_ACCOUNT_ID, "other-operator")

    async def restricted_session():
        async with factory() as session:
            await session.execute(text("SET LOCAL ROLE bfx_webapi"))
            assert await session.scalar(text("SELECT current_user")) == "bfx_webapi"
            yield session

    app = FastAPI()
    app.include_router(build_uncertainties_router())
    app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
    app.dependency_overrides[get_session] = restricted_session
    path = f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        empty = await client.get(path, params={"state": "open"})
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"data": []}
        async with factory.begin() as session:
            session.add(_decision())
            await session.flush()
            store = PostgresEventStore(deployment_environment="ci")
            await store.append(session, _snapshot(ACCOUNT_ID, finished_at=1000))
            reference = ReservationRef(execution_decision_id="decision-7", cid=7, signal_correlation_id=SCID)
            await store.append(session, ReservationIntent(
                symbol="fUST", cid=7, signal_correlation_id=SCID, account_id=str(ACCOUNT_ID),
                is_simulated=True, execution_decision_id="decision-7", reservation_ref=reference,
                submission_attempt=_attempt(), amount=Decimal("100"), occurred_at_ms=1050,
            ))
            await store.append(session, ReservationUnknown(
                symbol="fUST", cid=7, size_usdt=Decimal("100"), signal_correlation_id=SCID,
                account_id=str(ACCOUNT_ID), is_simulated=True, reason="connection_reset",
                occurred_at_ms=1100, reservation_ref=reference,
            ))
        reconcile_seq = await _append_snapshot(factory, finished_at=2000, started_at=1990)
        populated = await client.get(path, params={"state": "open"})
        assert populated.status_code == 200, populated.text
        rows = populated.json()["data"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "submit_outcome_unknown"
        assert rows[0]["resolutionContext"] == {
            "reconcileEventSeq": reconcile_seq,
            "queryStartedAtMs": 1990,
            "queryFinishedAtMs": 2000,
            "candidateCount": 0,
            "candidateVenueOfferIds": [],
            "unavailableReason": None,
        }
        detail = await client.get(f"{path}/{rows[0]['uncertaintyId']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["resolutionContext"] == rows[0]["resolutionContext"]
        denied = await client.get(f"/api/v1/exchange-accounts/{OTHER_ACCOUNT_ID}/uncertainties")
        assert denied.status_code == 404
        denied_detail = await client.get(
            f"/api/v1/exchange-accounts/{OTHER_ACCOUNT_ID}/uncertainties/{rows[0]['uncertaintyId']}"
        )
        assert denied_detail.status_code == 404
    async with factory() as session:
        for table in ("execution_uncertainties", "submission_attempts", "event_log", "position_state", "offer_claims"):
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not await session.scalar(text(
                    "SELECT has_table_privilege('bfx_webapi', :table, :privilege)"
                ), {"table": table, "privilege": privilege})
            assert not await session.scalar(text(
                "SELECT has_any_column_privilege('bfx_webapi', :table, 'INSERT,UPDATE')"
            ), {"table": table})
