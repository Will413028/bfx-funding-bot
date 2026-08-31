from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.live_validation.tables  # noqa: F401  # registers ORM
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.api.attribution import build_attribution_router
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow


def _row(
    cell: str = "fUST_p2",
    week: int = 1_782_691_200_000,
    env: str = "prod",
    account: str = "default",
) -> AttributionWeeklyRow:
    return AttributionWeeklyRow(
        deployment_environment=env, account_id=account, cell=cell,
        week_start_ms=week, week_end_ms=week + 604_800_000, n_fills=2,
        gross_interest_usdt=Decimal("0.2"), net_interest_usdt=Decimal("0.17"),
        capital_days=Decimal("1000"), realized_apr_net_pct=Decimal("6.205"),
        baseline_close_apr_net_pct=Decimal("6.205"),
        baseline_frr_apr_net_pct=None,
        baseline_frr_util_apr_net_pct=Decimal("4.34"),
    )


@pytest_asyncio.fixture
async def app_client(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(_row())
        s.add(_row(cell="fUST_a30"))
        # different realm — must never leak into the default-realm response
        s.add(_row(cell="fUST_p2", env="canary"))
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

    app.dependency_overrides[require_operator] = _fake_user
    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


def test_weekly_returns_rows_camel_case(app_client):
    resp = app_client.get("/api/v1/attribution/weekly")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    # ordered by cell: 'fUST_a30' < 'fUST_p2'
    assert [d["cell"] for d in data] == ["fUST_a30", "fUST_p2"]
    row = next(d for d in data if d["cell"] == "fUST_p2")
    assert row["weekStartMs"] == 1_782_691_200_000
    assert row["realizedAprNetPct"] == "6.205"
    assert row["baselineFrrAprNetPct"] is None
    assert row["baselineFrrUtilAprNetPct"] == "4.34"
    assert row["grossInterestUsdt"] == "0.2"
    assert row["netInterestUsdt"] == "0.17"
    # regression guard: _dec_str must not emit scientific notation ("1E+3")
    # for whole-number Decimals.
    assert row["capitalDays"] == "1000"


def test_weekly_cell_filter(app_client):
    resp = app_client.get("/api/v1/attribution/weekly", params={"cell": "fUST_a30"})
    assert [d["cell"] for d in resp.json()["data"]] == ["fUST_a30"]


def test_weekly_excludes_other_realm(app_client):
    """Rows from a different deployment_environment/account_id must never
    leak into the default-realm response — otherwise the FE weekly chart
    (Task 7) would plot duplicate points per cell/week."""
    resp = app_client.get("/api/v1/attribution/weekly")
    data = resp.json()["data"]
    assert len(data) == 2
    assert all(d["cell"] in ("fUST_a30", "fUST_p2") for d in data)
    # both default-realm rows for fUST_p2/fUST_a30 present exactly once each —
    # the canary-realm fUST_p2 row seeded in app_client is excluded.
    cells = [d["cell"] for d in data]
    assert cells.count("fUST_p2") == 1
    assert cells.count("fUST_a30") == 1


def test_weekly_requires_auth(sqlite_engine):
    app = FastAPI()
    app.include_router(build_attribution_router())
    client = TestClient(app)
    assert client.get("/api/v1/attribution/weekly").status_code in (401, 403)
