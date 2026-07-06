from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.live_validation.tables  # noqa: F401  # registers ORM
from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.api.attribution import build_attribution_router
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow


def _row(cell: str = "fUST_p2", week: int = 1_782_691_200_000) -> AttributionWeeklyRow:
    return AttributionWeeklyRow(
        deployment_environment="prod", account_id="default", cell=cell,
        week_start_ms=week, week_end_ms=week + 604_800_000, n_fills=2,
        gross_interest_usdt=Decimal("0.2"), net_interest_usdt=Decimal("0.17"),
        capital_days=Decimal("1000"), realized_apr_net_pct=Decimal("6.205"),
        baseline_close_apr_net_pct=Decimal("6.205"),
        baseline_frr_apr_net_pct=None,
    )


@pytest_asyncio.fixture
async def app_client(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(_row())
        s.add(_row(cell="fUST_a30"))
        await s.commit()

    app = FastAPI()
    app.include_router(build_attribution_router())

    async def _fake_user():
        return Principal(user_id="user_abc", email="will@example.com", role="operator")

    async def _override_session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[require_user] = _fake_user
    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


def test_weekly_returns_rows_camel_case(app_client):
    resp = app_client.get("/api/v1/attribution/weekly")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    row = next(d for d in data if d["cell"] == "fUST_p2")
    assert row["weekStartMs"] == 1_782_691_200_000
    assert row["realizedAprNetPct"] == "6.205"
    assert row["baselineFrrAprNetPct"] is None


def test_weekly_cell_filter(app_client):
    resp = app_client.get("/api/v1/attribution/weekly", params={"cell": "fUST_a30"})
    assert [d["cell"] for d in resp.json()["data"]] == ["fUST_a30"]


def test_weekly_requires_auth(sqlite_engine):
    app = FastAPI()
    app.include_router(build_attribution_router())
    client = TestClient(app)
    assert client.get("/api/v1/attribution/weekly").status_code in (401, 403)
