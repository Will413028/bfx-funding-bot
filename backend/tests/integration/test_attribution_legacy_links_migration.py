"""Migration a0b1c2d3e4f5: the legacy offer -> cell, resolved once out of the frozen tables.

The weekly must compute the same output from the copy as it did from the legacy tables when
no offer has two cells: ``reference_legacy_offer_cells`` is the loader's pre-S1-8 read,
verbatim; the same fixture is computed with it and, after the migration, with the real loader,
and the results (persisted rows, reconciliations, credit -> cell model, rendered report) must be
equal. An offer the legacy records place in two cells becomes a reported conflict.

Mutations (one at a time, revert after each, run this file):

* Write only the smallest cell of an offer (``sorted(cells[offer])[:1]``): the conflict test
  fails.
* Drop the epoch check (``_require_frozen_source`` returns at once): the refusal test fails.
* Resolve without the diagnostics (pass ``[]``): the equality and copy tests fail.
* Read the fill's offer id from the column only: the copy and equality tests fail.
* Grant ``bfx_bot`` INSERT: the grants test fails.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.live_validation import attribution_loader
from bfx_funding_bot.modules.live_validation.attribution_loader import (
    AttributionResult,
    OfferCellConflict,
    load_and_compute,
    render_reconciliation,
)
from bfx_funding_bot.modules.live_validation.tables import (
    FundingCreditHistoryRow,
    FundingInterestPaymentRow,
    FundingTradeRow,
)
from tests.pg_templates import alembic, stamp_realm

from .legacy_attribution_links import PARENT, materialize, reference_legacy_offer_cells
from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

ENV = "ci"
ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
OTHER = UUID("aaaaaaaa-bbbb-4ccc-8ddd-000000000002")
DAY = 86_400_000
MON = 1_789_948_800_000          # 2026-09-21 UTC Monday
T = MON + 2 * DAY
NOW = MON + 3 * 7 * DAY
RATE = Decimal("0.00019999")
TABLE = "attribution_legacy_offer_cells"


# -- databases ------------------------------------------------------------------------------

def _build_parent(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)  # worst case: default privileges hand bfx_bot / bfx_webapi everything
    with engine.begin() as conn:
        conn.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                             "rolname='bfx_webauth') THEN CREATE ROLE bfx_webauth; END IF; END $$")
    engine.dispose()
    alembic(url, "upgrade", PARENT)
    stamp_realm(url, ENV)


@pytest.fixture
def parent_url(pg_templates: Any, pg_clone: Any) -> str:
    return pg_clone(pg_templates.template("attribution_cells_parent", _build_parent))


@pytest.fixture
def sync_engine(parent_url: str) -> Iterator[Any]:
    engine = create_engine(parent_url)
    yield engine
    engine.dispose()


# The migration's copy (a0b1c2d3e4f5) is re-run below c2d3e4f5a6b7, which then archives its
# legacy sources out of public.
PRE_ARCHIVE = "b1c2d3e4f5a6"


@pytest_asyncio.fixture
async def factory(parent_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # Rows are planted (and the reference read runs) at PARENT, where the legacy tables are
    # still in public; the ORM names the archive schema they move to later.
    engine = create_async_engine(
        parent_url, execution_options={"schema_translate_map": {"legacy_archive": None}})
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _switch(engine: Any) -> None:
    """The authority switch: the legacy tables are frozen from here on."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            "SELECT max(epoch_seq) + 1, 'ledger', 1, 'test', 'switch' FROM capital_authority_epoch")


# -- fixture rows ---------------------------------------------------------------------------

def _decision(decision_id: str, cell: str, scid: str) -> ExecutionDecisionRow:
    return ExecutionDecisionRow(
        decision_id=decision_id, account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
        deployment_environment=ENV, reconcile_id="r", cell_id=cell, symbol="fUST",
        signal_correlation_id=scid, outcome="submitted", signal_rate=RATE,
        amount_usdt=Decimal("100"), duration_days=2, model_evidence={}, safety_result={},
        execution_policy="p", service_version="v", config_hash="h",
        occurred_at_ms=T - 60_000, recorded_at_ms=T - 60_000,
    )


def _claim(cid: int, voi: str | None, decision: str | None, scid: str,
           account: UUID = ACCOUNT) -> OfferClaimRow:
    return OfferClaimRow(
        cid=cid, account_id=str(account), exchange_account_id=account,
        deployment_environment=ENV, state="FILLED", venue_offer_id=voi, symbol="fUST",
        size_usdt=Decimal("100"), signal_correlation_id=scid, execution_decision_id=decision,
        occurred_at_ms=T, last_updated_ms=T, last_event_seq=1,
    )


def _venue_offer(voi: str, decision: str | None, scid: str | None) -> VenueOfferStateRow:
    return VenueOfferStateRow(
        exchange_account_id=ACCOUNT, deployment_environment=ENV, venue_offer_id=voi,
        symbol="fUST", amount_original=Decimal("100"), amount_remaining=Decimal("0"),
        rate=RATE, period_days=2, status="EXECUTED", flags={}, mts_created=T, mts_updated=T,
        execution_decision_id=decision, signal_correlation_id=scid, first_seen_event_seq=1,
        last_seen_event_seq=1, is_terminal=True,
    )


def _fill(payload: dict[str, Any], voi: str | None = None, account: UUID = ACCOUNT,
          event_type: str = "ORDER_FILL") -> EventLogRow:
    return EventLogRow(account_id=str(account), exchange_account_id=account,
                       deployment_environment=ENV, event_type=event_type, venue_offer_id=voi,
                       payload=payload, occurred_at_ms=T)


def _legacy_credit(credit_id: str, *, rate: Decimal | None = RATE,
                   terminal: bool = False) -> VenueCreditStateRow:
    return VenueCreditStateRow(
        exchange_account_id=ACCOUNT, deployment_environment=ENV, credit_id=credit_id,
        symbol="fUST", amount=Decimal("-100"), rate=rate, period_days=2,
        status="CLOSED" if terminal else "ACTIVE", flags={}, mts_created=T, mts_updated=T,
        first_seen_event_seq=1, last_seen_event_seq=1, is_terminal=terminal,
    )


def _ended(credit_id: int, at: int) -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=ACCOUNT, kind="credit", credit_id=credit_id,
        deployment_environment=ENV, symbol="fUST", side=1, mts_create=at, mts_update=at + DAY,
        amount=Decimal("100"), status="CLOSED", rate=RATE, period_days=2, mts_opening=at,
        mts_last_payout=at + DAY,
    )


def _trade(trade_id: int, offer: int, at: int) -> FundingTradeRow:
    return FundingTradeRow(
        exchange_account_id=ACCOUNT, trade_id=trade_id, deployment_environment=ENV,
        symbol="fUST", mts_create=at, offer_id=offer, amount=Decimal("100"), rate=RATE,
        period_days=2, maker=True,
    )


def _ours(offer: int, n: int) -> list[object]:
    """An ended credit whose trade names ``offer``."""
    return [_ended(9000 + n, T + n * 1_000), _trade(7000 + n, offer, T + n * 1_000)]


async def _accounts(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as s:
        for account in (ACCOUNT, OTHER):
            await s.execute(text("INSERT INTO exchange_accounts(id, venue, label) "
                                 "VALUES (:id, 'bitfinex', 'attribution')"), {"id": account})


async def _plant(factory: async_sessionmaker[AsyncSession]) -> None:
    """Every link shape, no offer in two cells."""
    await _accounts(factory)
    async with factory.begin() as s:
        s.add_all([
            _decision("d1", "fUST_p2", "s1"),
            _decision("d3", "fUST_a30", "s3"),
            _decision("d4", "fUST_p7", "s4"),
            DiagnosticsRow(account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
                           deployment_environment=ENV, kind="decision",
                           payload={"cell": "fUST_p30", "correlation_id": "s2"},
                           occurred_at=datetime(2026, 9, 23, tzinfo=UTC)),
            # claims: by decision; by signal correlation (diagnostics); no venue offer yet
            _claim(1, "1001", "d1", "s1"), _claim(2, "1002", None, "s2"),
            _claim(3, None, None, "s9"), _claim(4, "1001", "d1", "s1", account=OTHER),
            # venue offers: the same offer again, and one known by signal correlation only
            _venue_offer("1001", "d1", "s1"), _venue_offer("1003", None, "s3"),
            # fills: offer id in the payload as a number; only in the column; no correlation;
            # duplicated; empty; another account's; not a fill
            _fill({"venue_offer_id": 1004, "signal_correlation_id": "s4"}),
            _fill({"venue_offer_id": 1004, "signal_correlation_id": "s4"}),
            _fill({"signal_correlation_id": ""}, voi="1005"),
            _fill({"venue_offer_id": "", "signal_correlation_id": "s1"}),
            _fill({"venue_offer_id": "1006", "signal_correlation_id": "s3"}, account=OTHER),
            _fill({"venue_offer_id": "1007", "signal_correlation_id": "s1"},
                  event_type="OFFER_ACK"),
            # legacy credits the pre-S1-8 loader did not use either (terminal, incomplete)
            _legacy_credit("8003", rate=None), _legacy_credit("8005", terminal=True),
            *[row for i in range(1, 6) for row in _ours(1000 + i, i)],
            _trade(7100, 1003, T + 3_600_000), _ended(9100, T + 3_600_000),
            FundingInterestPaymentRow(
                exchange_account_id=ACCOUNT, ledger_id=1, deployment_environment=ENV,
                currency="UST", mts=T + DAY, amount=Decimal("0.05"), balance=Decimal("500"),
                description="Margin Funding Payment on wallet funding"),
        ])


async def _plant_conflicts(factory: async_sessionmaker[AsyncSession]) -> None:
    """2001: two decisions in two cells; 2002: two correlation ids in two cells; 2003 fine."""
    await _accounts(factory)
    async with factory.begin() as s:
        s.add_all([
            _decision("d1", "fUST_p2", "s1"), _decision("d3", "fUST_a30", "s3"),
            _decision("d5", "fUST_p2", "s5"),
            _claim(1, "2001", "d1", "s1"), _venue_offer("2001", "d3", "s3"),
            _fill({"venue_offer_id": "2002", "signal_correlation_id": "s1"}),
            _fill({"venue_offer_id": "2002", "signal_correlation_id": "s3"}),
            _claim(2, "2003", "d5", "s5"),
            *_ours(2001, 1), *_ours(2002, 2), *_ours(2003, 3),
        ])


async def _weekly(factory: async_sessionmaker[AsyncSession]) -> AttributionResult:
    return await load_and_compute(factory, account_id=str(ACCOUNT), deployment_environment=ENV,
                                  now_ms=NOW)


def _copied(engine: Any) -> set[tuple[Any, ...]]:
    with engine.connect() as conn:
        return {tuple(r) for r in conn.execute(text(
            f"SELECT exchange_account_id, venue_offer_id, cell FROM {TABLE}"))}


# -- tests ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_weekly_from_the_copy_equals_the_weekly_from_the_legacy_tables(
    factory: async_sessionmaker[AsyncSession], parent_url: str, sync_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _plant(factory)
    _switch(sync_engine)
    with monkeypatch.context() as patched:
        patched.setattr(attribution_loader, "legacy_offer_cells", reference_legacy_offer_cells)
        legacy = await _weekly(factory)
    alembic(parent_url, "upgrade", "head")
    copied = await _weekly(factory)

    assert legacy.cells is not None and legacy.rows, "the fixture must attribute something"
    assert {legacy.cells.cell_by_credit[str(9000 + i)] for i in (1, 2, 3, 4)} == {
        "fUST_p2", "fUST_p30", "fUST_a30", "fUST_p7"}
    assert legacy.cells.cell_by_credit["9100"] == "fUST_a30"
    assert legacy.cells.cell_by_credit["9005"] == "unattributed"  # 1005: no correlation
    assert not {"8003", "8005"} & set(legacy.cells.cell_by_credit)

    assert copied.rows == legacy.rows                      # persisted rows, in order
    assert copied.reconciliations == legacy.reconciliations
    assert copied.cells == legacy.cells
    assert (copied.credits, copied.has_credit_history, copied.offer_conflicts) == (
        legacy.credits, legacy.has_credit_history, legacy.offer_conflicts)
    assert render_reconciliation(copied) == render_reconciliation(legacy)


@pytest.mark.asyncio
async def test_the_copy_holds_each_offers_resolved_cell(
    factory: async_sessionmaker[AsyncSession], parent_url: str, sync_engine: Any,
) -> None:
    await _plant(factory)
    _switch(sync_engine)
    alembic(parent_url, "upgrade", PRE_ARCHIVE)
    rows = _copied(sync_engine)
    assert rows == {
        (ACCOUNT, "1001", "fUST_p2"),    # claim and venue offer, by decision
        (ACCOUNT, "1002", "fUST_p30"),   # by correlation, through the diagnostics
        (ACCOUNT, "1003", "fUST_a30"),   # by correlation, through a decision
        (ACCOUNT, "1004", "fUST_p7"),    # fill, payload number -> text
        # 1005 (no correlation) resolves to nothing; OTHER has no decisions
    }

    await materialize(factory)           # idempotent
    assert _copied(sync_engine) == rows
    alembic(parent_url, "upgrade", "head")   # archiving the sources keeps the copy
    assert _copied(sync_engine) == rows
    alembic(parent_url, "downgrade", PARENT)
    with sync_engine.connect() as conn:
        assert conn.scalar(text(f"SELECT to_regclass('public.{TABLE}')")) is None
    alembic(parent_url, "upgrade", "head")   # deterministic
    alembic(parent_url, "check")
    assert _copied(sync_engine) == rows


@pytest.mark.asyncio
async def test_an_offer_the_legacy_records_place_in_two_cells_is_a_conflict(
    factory: async_sessionmaker[AsyncSession], parent_url: str, sync_engine: Any,
) -> None:
    await _plant_conflicts(factory)
    _switch(sync_engine)
    alembic(parent_url, "upgrade", "head")
    assert _copied(sync_engine) == {
        (ACCOUNT, "2001", "fUST_p2"), (ACCOUNT, "2001", "fUST_a30"),
        (ACCOUNT, "2002", "fUST_p2"), (ACCOUNT, "2002", "fUST_a30"),
        (ACCOUNT, "2003", "fUST_p2"),
    }
    result = await _weekly(factory)
    assert result.offer_conflicts == (
        OfferCellConflict("2001", ("fUST_a30", "fUST_p2"), ()),
        OfferCellConflict("2002", ("fUST_a30", "fUST_p2"), ()),
    )
    assert result.cells is not None
    assert result.cells.cell_by_credit == {
        "9001": "unattributed", "9002": "unattributed", "9003": "fUST_p2"}
    assert "OFFER CELL CONFLICTS (2)" in render_reconciliation(result)


@pytest.mark.asyncio
async def test_the_copy_refuses_a_source_that_is_not_frozen(
    factory: async_sessionmaker[AsyncSession], parent_url: str, sync_engine: Any,
) -> None:
    await _plant(factory)                # legacy rows, epoch still 'legacy'
    with pytest.raises(RuntimeError, match="capital authority is 'legacy', not 'ledger'"):
        alembic(parent_url, "upgrade", "head")
    with sync_engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == PARENT
        assert conn.scalar(text(f"SELECT to_regclass('public.{TABLE}')")) is None
    _switch(sync_engine)
    alembic(parent_url, "upgrade", "head")
    assert _copied(sync_engine)


@pytest.mark.asyncio
async def test_the_migration_is_a_no_op_on_empty_legacy_tables(
    factory: async_sessionmaker[AsyncSession], parent_url: str, sync_engine: Any,
) -> None:
    alembic(parent_url, "upgrade", PRE_ARCHIVE)  # epoch 'legacy': nothing to copy, no refusal
    assert _copied(sync_engine) == set()
    await materialize(factory)
    assert _copied(sync_engine) == set()
    alembic(parent_url, "upgrade", "head")
    alembic(parent_url, "check")


def _privileges(conn: Any, role: str, table: str) -> list[str]:
    return [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
                        "TRIGGER")
            if conn.scalar(text("SELECT has_table_privilege(:r, :t, :p)"),
                           {"r": role, "t": f"public.{table}", "p": p})]


def test_the_weekly_role_only_reads_the_copy(parent_url: str, sync_engine: Any) -> None:
    alembic(parent_url, "upgrade", "head")
    with sync_engine.connect() as conn:
        assert _privileges(conn, "bfx_bot", TABLE) == ["SELECT"]
        assert _privileges(conn, "bfx_webapi", TABLE) == []
        assert _privileges(conn, "bfx_webauth", TABLE) == []
        assert _privileges(conn, "public", TABLE) == []
    with sync_engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(f"INSERT INTO {TABLE} VALUES ('{ACCOUNT}', 'ci', '1', 'fUST_p2')")
    with sync_engine.connect() as conn:
        conn.exec_driver_sql("SET ROLE bfx_bot")
        assert conn.scalar(text(f"SELECT count(*) FROM {TABLE}")) == 0
