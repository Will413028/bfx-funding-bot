"""The weekly chain and the research jobs, logged in as ``bfx_research``, at head.

``test_research_privilege_allowlist.py`` compares the role's privileges with a list; this
module runs the steps' own code logged in as the role (a LOGIN role with a password, as the
weekly container connects), so PostgreSQL's permission checks decide, not a copy of the list:

* each research table accepts the write its script makes (the upserts twice, so ON CONFLICT
  DO UPDATE runs; the replaces twice, so DELETE runs over existing rows);
* the bot's tables refuse every write: ``trading_state`` and the ledger journals;
* the read paths of the weekly operational steps (attribution, realized interest, G3) and of
  the research scripts and backtests run to the end.

The fixtures plant rows as the owner so the conditional reads run: trades make the
attribution read the journals' cell provenance, credits make it read candles and stats.

Mutation checks (one at a time; revert after each):

* Drop ``funding_interest_payments`` from ``RESEARCH_TABLE_PRIVILEGES`` in ``3c5e7a9b1d2f``:
  the read-path tests fail with ``permission denied``.
* Drop ``UPDATE`` from ``funding_stats``'s grant: the write test fails on the second upsert.
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from bfx_funding_bot.apps.config import load_cells_only
from bfx_funding_bot.external.bitfinex.funding_stat_row import FundingStat
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.external_signals.repository import (
    get_liq_max_mts,
    get_liq_min_mts,
    get_perp_max_mts,
    get_perp_min_mts,
    upsert_liquidations,
    upsert_perp_funding,
)
from bfx_funding_bot.modules.external_signals.schemas import LiquidationRecord, PerpFundingRecord
from bfx_funding_bot.modules.external_signals.tables import LiquidationRow, PerpFundingRateRow
from bfx_funding_bot.modules.funding_stats.repository import get_max_mts, upsert_funding_stats
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.lending.tracking.tables import (
    FillRateModelArtifactRow,
    FillRateStatsRow,
)
from bfx_funding_bot.modules.live_validation.tables import (
    AttributionWeeklyRow,
    FundingCreditHistoryRow,
    FundingInterestPaymentRow,
    FundingTradeRow,
)
from bfx_funding_bot.modules.marketfeed.tables import FundingBookSnapshotRow
from scripts import (
    ingest_liquidations,
    ingest_perp_funding,
    report_interest,
    run_g3_live_validation,
    run_period_structure_backtest,
    run_signal_eda,
    run_weekly_attribution,
)
from scripts.learn_book_fill_rate import learn_book_and_store
from tests.pg_templates import alembic, stamp_realm

from .test_research_privilege_allowlist import ROLE
from .test_trading_state_migration import _reset

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PASSWORD = "research-role-test"
ACCOUNT = UUID("5f0c1e2d-3b4a-4c5d-8e6f-7a8b9c0d1e2f")
_H = 3_600_000
_T0 = 1_704_067_200_000  # 2024-01-01T00:00Z
RATE = Decimal("0.0002")
FRR = Decimal("0.00001")  # funding_stats.frr's unit: x365 is a per-day market rate


def _build(url: str) -> None:
    """Production's host setup for the role (fresh-host-setup §1a), migrated and stamped."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{ROLE}') "
                             f"THEN CREATE ROLE {ROLE}; END IF; END $$")
        conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    engine.dispose()
    alembic(url, "upgrade", "head")
    stamp_realm(url, "ci")


@pytest.fixture
def db_url(pg_templates: Any, pg_clone: Any) -> Iterator[str]:
    url = pg_clone(pg_templates.template("research_role_behaviour", _build))
    engine = create_engine(url)
    with engine.begin() as conn:
        # The role is cluster-wide and other modules create it NOLOGIN: set the login on every
        # use, never only when the cached template is built.
        conn.exec_driver_sql(f"ALTER ROLE {ROLE} LOGIN PASSWORD '{PASSWORD}'")
    engine.dispose()
    yield url


def _research_url(url: str) -> str:
    return make_url(url).set(drivername="postgresql+asyncpg", username=ROLE,
                             password=PASSWORD).render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def owner(db_url: str) -> AsyncIterator[async_sessionmaker[Any]]:
    engine = create_async_engine(make_url(db_url).set(drivername="postgresql+asyncpg"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def research(db_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    """An engine logged in as the role, and the env the weekly container gets."""
    url = _research_url(db_url)
    for key, value in {"DATABASE_URL": url, "BFX_EXCHANGE_ACCOUNT_ID": str(ACCOUNT),
                       "BFX_DEPLOYMENT_ENV": "ci"}.items():
        monkeypatch.setenv(key, value)
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT current_user")) == ROLE
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_market(owner: async_sessionmaker[Any]) -> None:
    """Candles and book snapshots as test_learn_book_fill_rate seeds them, plus FRR stats."""
    async with owner() as s:
        for i in range(46):
            for period_agg in ("p2", "p30", "a30"):
                s.add(FundingCandleRow(
                    symbol="fUST", timeframe="1h", period_agg=period_agg, mts=_T0 + i * _H,
                    open=0.0002, close=0.0002, high=0.0002, low=0.0002, volume=1000.0,
                    is_final=True))
        for i in range(40):
            s.add(FundingBookSnapshotRow(
                symbol="fUST", captured_at_ms=_T0 + (i + 1) * _H + 600_000,
                best_ask_rate=Decimal("0.0002"), best_bid_rate=None, bid_depth=Decimal("0"),
                ask_depth=Decimal("2500"), payload={"asks": [[0.0002, 2, 5, 2500.0]], "bids": []}))
        await s.commit()
        await upsert_funding_stats(s, [FundingStat(symbol="fUST", mts=_T0 + i * _H, frr=FRR)
                                       for i in range(46)])
        await s.commit()


async def _seed_account(owner: async_sessionmaker[Any]) -> None:
    """One closed credit, the trade that opened it and its payout, in realm ``ci``."""
    opening, closed = _T0 + 2 * _H, _T0 + 50 * _H
    async with owner() as s:
        await s.execute(text("INSERT INTO exchange_accounts (id, venue, label) "
                             "VALUES (:id, 'bitfinex', 'research-test')"), {"id": ACCOUNT})
        s.add(FundingCreditHistoryRow(
            exchange_account_id=ACCOUNT, kind="credit", credit_id=7001,
            deployment_environment="ci", symbol="fUST", side=1, mts_create=opening,
            mts_update=closed, amount=Decimal("100"), status="CLOSED", rate=RATE, period_days=2,
            mts_opening=opening, mts_last_payout=closed))
        s.add(FundingTradeRow(
            exchange_account_id=ACCOUNT, trade_id=8001, deployment_environment="ci",
            symbol="fUST", mts_create=opening, offer_id=9001, amount=Decimal("100"), rate=RATE,
            period_days=2, maker=True))
        s.add(FundingInterestPaymentRow(
            exchange_account_id=ACCOUNT, ledger_id=6001, deployment_environment="ci",
            currency="UST", mts=closed, amount=Decimal("0.03"), balance=Decimal("570"),
            description="Margin Funding Payment on wallet funding"))
        await s.commit()


# --------------------------------------------------------------------------- (a) writes


async def test_each_research_table_takes_its_scripts_write(
    owner: async_sessionmaker[Any], research: AsyncEngine,
) -> None:
    await _seed_market(owner)
    await _seed_account(owner)
    as_role = async_sessionmaker(research, expire_on_commit=False)
    perp = [PerpFundingRecord(venue="bitfinex", symbol="tBTCF0:USTF0", mts=_T0, funding_rate=0.0001)]
    liq = [LiquidationRecord(venue="bitfinex", pos_id=1, mts=_T0, symbol="tBTCF0:USTF0",
                             amount=-0.5, is_match=0, is_market_sold=1)]
    for run in range(2):  # the second run hits ON CONFLICT DO UPDATE / DELETE over rows
        async with as_role() as s:
            await upsert_funding_stats(s, [FundingStat(symbol="fUSD", mts=_T0, frr=RATE + run)])
            await upsert_perp_funding(s, perp)
            await upsert_liquidations(s, liq)
            assert await learn_book_and_store(s, symbol="fUST", period_days=2)
            await s.commit()
        assert await run_weekly_attribution._amain(argparse.Namespace(weeks=8, out=None)) == 0

    async with owner() as s:
        assert await s.scalar(select(FundingStatRow.frr).where(FundingStatRow.symbol == "fUSD")) \
            == pytest.approx(float(RATE + 1))
        assert await s.scalar(select(func.count()).select_from(PerpFundingRateRow)) == 1
        assert await s.scalar(select(func.count()).select_from(LiquidationRow)) == 1
        assert await s.scalar(select(func.count()).select_from(FillRateStatsRow)) > 0
        assert await s.scalar(select(func.count()).select_from(FillRateModelArtifactRow)) > 0
        assert await s.scalar(select(func.count()).select_from(AttributionWeeklyRow).where(
            AttributionWeeklyRow.exchange_account_id == ACCOUNT)) > 0


# --------------------------------------------------------------------------- (b) denials


async def _columns(owner: async_sessionmaker[Any], table: str) -> list[str]:
    async with owner() as s:
        return list((await s.scalars(text(
            "SELECT attname FROM pg_attribute WHERE attrelid = CAST(:t AS regclass) "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"), {"t": f"public.{table}"})).all())


@pytest.mark.parametrize("table", [
    "trading_state", "submission_attempt_journal", "transport_outcome_journal",
    "execution_resolution_journal", "ledger_observation", "accepted_capital_basis",
    "execution_decisions",
])
async def test_the_bot_tables_refuse_every_write(
    owner: async_sessionmaker[Any], research: AsyncEngine, table: str,
) -> None:
    column = (await _columns(owner, table))[0]
    for statement in (
        f"INSERT INTO {table} DEFAULT VALUES",
        f"UPDATE {table} SET {column} = {column}",
        f"DELETE FROM {table}",
        f"TRUNCATE {table}",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with research.begin() as conn:
                await conn.execute(text(statement))


async def test_the_read_only_inputs_refuse_writes_too(research: AsyncEngine) -> None:
    for statement in ("DELETE FROM funding_candles", "DELETE FROM funding_book_snapshots",
                      "UPDATE funding_interest_payments SET amount = amount",
                      "INSERT INTO database_realm DEFAULT VALUES",
                      "CREATE TABLE research_scratch (x int)"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with research.begin() as conn:
                await conn.execute(text(statement))


# --------------------------------------------------------------------------- (c) reads


async def test_the_weekly_operational_reports_run(
    owner: async_sessionmaker[Any], research: AsyncEngine, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    await _seed_market(owner)
    await _seed_account(owner)
    assert await run_weekly_attribution._amain(argparse.Namespace(weeks=8, out=None)) == 0
    assert "attribution_weekly upserted=" in capsys.readouterr().out
    assert await report_interest._amain(
        argparse.Namespace(weeks=8, currency="UST", out=str(tmp_path / "interest.md"))) == 0
    out = tmp_path / "g3.md"
    monkeypatch.setattr("sys.argv", ["run_g3_live_validation", "--out", str(out)])
    assert await run_g3_live_validation._amain() == 0  # capital from the ledger's payouts
    assert out.is_file()


async def test_the_research_reads_and_backtests_run(
    owner: async_sessionmaker[Any], research: AsyncEngine,
) -> None:
    await _seed_market(owner)
    as_role = async_sessionmaker(research, expire_on_commit=False)
    now = int(datetime.now(UTC).timestamp() * 1000)
    async with as_role() as s:
        await upsert_perp_funding(s, [PerpFundingRecord(
            venue="bitfinex", symbol="tBTCF0:USTF0", mts=_T0, funding_rate=0.0001)])
        await upsert_liquidations(s, [LiquidationRecord(
            venue="bitfinex", pos_id=1, mts=_T0, symbol="tBTCF0:USTF0", amount=-0.5,
            is_match=0, is_market_sold=1)])
        assert await learn_book_and_store(s, symbol="fUST", period_days=2)
        await s.commit()
    async with as_role() as s:
        # ingest_funding_stats / ingest_perp_funding / ingest_liquidations: resume points and checks
        assert await get_max_mts(s, symbol="fUST") is not None
        assert await get_perp_min_mts(s, venue="bitfinex", symbol="tBTCF0:USTF0") == _T0
        assert await get_perp_max_mts(s, venue="bitfinex", symbol="tBTCF0:USTF0") == _T0
        assert await get_liq_min_mts(s, venue="bitfinex") == _T0
        assert await get_liq_max_mts(s, venue="bitfinex") == _T0
        await ingest_perp_funding._series_pass_check(s, venue="bitfinex", symbol="tBTCF0:USTF0")
        await ingest_liquidations._pass_check(s)
        # run_period_structure_backtest (linear and --fill-model book)
        raw, _frr = await run_period_structure_backtest._load_db_series(
            s, symbol="fUST", start_mts=0, end_mts=now)
        assert raw
        await run_period_structure_backtest.load_book_models(s, symbol="fUST", horizon_h=4)
        # run_oos_profitability reads candles only; run_signal_eda candles and stats
        assert await get_candles_in_range(s, symbol="fUST", timeframe="1h", period_agg="p2",
                                          start_mts=0, end_mts=now)
        cell = load_cells_only(run_signal_eda.DEFAULT_CELLS_YAML)[0]
        await run_signal_eda._fetch_cell(s, cell, now)
