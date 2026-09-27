"""modules/api/public.py — public proof-summary + funding-rates.csv.

Compliance-critical: these endpoints must never require auth and must never
serialize absolute-$ fields (capital_days / gross_interest_usdt /
net_interest_usdt) — see the public read model in backend/ARCHITECTURE.md.
"""
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.candles.tables  # registers ORM
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401  # registers ORM
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.public import build_public_router
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow

WEEK_1 = 1_782_691_200_000  # 2026-06-29 UTC Monday
WEEK_2 = WEEK_1 + 604_800_000
PUBLIC_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")

# Field names that must NEVER appear anywhere in a public response body —
# raw dollar/capital figures leak the operator's personal capital scale and would
# cross the "no guaranteed/absolute return disclosure" compliance line.
FORBIDDEN_SUBSTRINGS = [
    "capital_days", "capitalDays",
    "gross_interest_usdt", "grossInterestUsdt",
    "net_interest_usdt", "netInterestUsdt",
]


def _attribution_row(
    cell: str,
    week: int,
    *,
    env: str = "prod",
    account: str = "default",
    net: str,
    capital_days: str,
    baseline_util: str | None,
) -> AttributionWeeklyRow:
    return AttributionWeeklyRow(
        deployment_environment=env, account_id=account,
        exchange_account_id=PUBLIC_ACCOUNT_ID, cell=cell,
        week_start_ms=week, week_end_ms=week + 604_800_000, n_fills=2,
        gross_interest_usdt=Decimal("0.2"), net_interest_usdt=Decimal(net),
        capital_days=Decimal(capital_days), realized_apr_net_pct=Decimal("6.205"),
        baseline_close_apr_net_pct=Decimal("6.205"),
        baseline_frr_apr_net_pct=None,
        baseline_frr_util_apr_net_pct=(
            Decimal(baseline_util) if baseline_util is not None else None
        ),
    )


def _candle(symbol: str, mts: int, close: float) -> FundingCandleRow:
    return FundingCandleRow(
        symbol=symbol, timeframe="1h", period_agg="p2", mts=mts,
        open=close, close=close, high=close, low=close, volume=100.0,
    )


@pytest_asyncio.fixture
async def app_client(sqlite_engine, monkeypatch):
    monkeypatch.setenv("BFX_PUBLIC_EXCHANGE_ACCOUNT_ID", str(PUBLIC_ACCOUNT_ID))
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        # week 1: two cells with fills — realized must be capital-days-weighted.
        s.add(_attribution_row(
            "fUST_p2", WEEK_1, net="2", capital_days="6", baseline_util="6.0",
        ))
        s.add(_attribution_row(
            "fUST_a30", WEEK_1, net="1", capital_days="4", baseline_util="4.0",
        ))
        # different realm (canary) — must never leak/blend into the default
        # realm's aggregate, no matter how large its own numbers are.
        s.add(_attribution_row(
            "fUST_p2", WEEK_1, env="canary", net="999", capital_days="1",
            baseline_util="999",
        ))
        # week 2: no fills (capital_days=0) but a baseline is still present —
        # realized must be None, baseline must still surface (G3 convention).
        s.add(_attribution_row(
            "fUST_p2", WEEK_2, net="0", capital_days="0", baseline_util="3.0",
        ))
        s.add(_candle("fUST", WEEK_1, 0.0002))
        s.add(_candle("fUST", WEEK_1 + 3_600_000, 0.0003))
        s.add(_candle("fUSD", WEEK_1, 0.0001))
        await s.commit()

    app = FastAPI()
    app.include_router(build_public_router())

    async def _override_session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


# ── proof-summary ──


def test_proof_summary_requires_no_auth(app_client):
    """No dependency_overrides for require_user exist in this fixture at
    all — a 200 here proves the router truly carries zero auth dependency."""
    resp = app_client.get("/api/v1/public/proof-summary")
    assert resp.status_code == 200


def test_proof_summary_requires_explicit_public_account(sqlite_engine, monkeypatch):
    monkeypatch.delenv("BFX_PUBLIC_EXCHANGE_ACCOUNT_ID", raising=False)
    app = FastAPI()
    app.include_router(build_public_router())
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    async def _override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    response = TestClient(app).get("/api/v1/public/proof-summary")
    assert response.status_code == 503
    assert response.json()["detail"] == "public_account_not_configured"


def test_proof_summary_requires_explicit_deployment_environment(
    sqlite_engine, monkeypatch
):
    monkeypatch.setenv("BFX_PUBLIC_EXCHANGE_ACCOUNT_ID", str(PUBLIC_ACCOUNT_ID))
    monkeypatch.delenv("BFX_DEPLOYMENT_ENV", raising=False)
    app = FastAPI()
    app.include_router(build_public_router())
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    async def _override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    response = TestClient(app).get("/api/v1/public/proof-summary")
    assert response.status_code == 503
    assert response.json()["detail"] == "deployment_environment_not_configured"


def test_proof_summary_has_cache_control(app_client):
    resp = app_client.get("/api/v1/public/proof-summary")
    assert resp.headers["cache-control"] == "public, max-age=3600"


def test_proof_summary_aggregates_across_cells_weighted_by_capital_days(app_client):
    resp = app_client.get("/api/v1/public/proof-summary")
    weeks = resp.json()["data"]["weeks"]
    assert len(weeks) == 2
    week1 = next(w for w in weeks if w["weekStartMs"] == WEEK_1)
    # net_total=2+1=3, capital_days_total=6+4=10 -> 3/10*365*100 = 10950
    assert week1["realizedAprNetPct"] == "10950"
    # baseline = mean(6.0, 4.0) = 5.0
    assert week1["baselineFrrUtilAprNetPct"] == "5"


def test_proof_summary_excludes_other_realm(app_client):
    """The canary-realm row (net=999, capital_days=1) must never blend into
    the default-realm aggregate for week 1 — confirmed above (10950/5), this
    test pins the exclusion explicitly so a future realm-filter regression
    fails loudly instead of quietly skewing the public series."""
    resp = app_client.get("/api/v1/public/proof-summary")
    weeks = resp.json()["data"]["weeks"]
    week1 = next(w for w in weeks if w["weekStartMs"] == WEEK_1)
    assert week1["realizedAprNetPct"] == "10950"


def test_proof_summary_no_fills_week_is_none_but_baseline_survives(app_client):
    resp = app_client.get("/api/v1/public/proof-summary")
    weeks = resp.json()["data"]["weeks"]
    week2 = next(w for w in weeks if w["weekStartMs"] == WEEK_2)
    assert week2["realizedAprNetPct"] is None
    assert week2["baselineFrrUtilAprNetPct"] == "3"


def test_proof_summary_as_of_present(app_client):
    resp = app_client.get("/api/v1/public/proof-summary")
    as_of = resp.json()["data"]["asOf"]
    assert isinstance(as_of, str)
    assert len(as_of) > 0


def test_proof_summary_never_leaks_absolute_dollar_fields(app_client):
    resp = app_client.get("/api/v1/public/proof-summary")
    raw = resp.text
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in raw, f"leaked forbidden field: {forbidden}"


@pytest_asyncio.fixture
async def empty_app_client(sqlite_engine, monkeypatch):
    """A brand-new deployment with zero attribution_weekly rows."""
    monkeypatch.setenv("BFX_PUBLIC_EXCHANGE_ACCOUNT_ID", str(PUBLIC_ACCOUNT_ID))
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(build_public_router())

    async def _override_session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


def test_proof_summary_empty_realm_returns_empty_weeks_and_asof(empty_app_client):
    """No DB rows at all (a brand-new deployment) must not 500 — asOf falls
    back to wall-clock time instead of crashing on max() of an empty seq."""
    resp = empty_app_client.get("/api/v1/public/proof-summary")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["weeks"] == []
    assert isinstance(data["asOf"], str) and len(data["asOf"]) > 0


# ── funding-rates.csv ──


def test_csv_requires_no_auth_and_returns_fust_rows(app_client):
    resp = app_client.get(
        "/api/v1/public/funding-rates.csv", params={"symbol": "fUST"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["cache-control"] == "public, max-age=3600"
    lines = resp.text.strip("\n").split("\n")
    assert lines[0] == "date,close_apr_pct"
    assert lines[1] == "2026-06-29T00:00:00Z,7.300000"
    assert lines[2] == "2026-06-29T01:00:00Z,10.950000"
    assert len(lines) == 3


def test_csv_fusd_returns_only_fusd_rows(app_client):
    resp = app_client.get(
        "/api/v1/public/funding-rates.csv", params={"symbol": "fUSD"}
    )
    assert resp.status_code == 200
    lines = resp.text.strip("\n").split("\n")
    assert lines == ["date,close_apr_pct", "2026-06-29T00:00:00Z,3.650000"]


def test_csv_rejects_unsupported_symbol(app_client):
    resp = app_client.get(
        "/api/v1/public/funding-rates.csv", params={"symbol": "fETH"}
    )
    assert resp.status_code == 422


def test_csv_rejects_missing_symbol(app_client):
    resp = app_client.get("/api/v1/public/funding-rates.csv")
    assert resp.status_code == 422


def test_csv_never_leaks_absolute_dollar_fields(app_client):
    resp = app_client.get(
        "/api/v1/public/funding-rates.csv", params={"symbol": "fUST"}
    )
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in resp.text
