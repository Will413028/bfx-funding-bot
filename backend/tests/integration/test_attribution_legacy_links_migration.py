"""Migration a0b1c2d3e4f5: the weekly attribution's legacy links, copied out of the frozen tables.

The weekly must compute the same output from the copy as it did from the legacy tables:
``reference_*`` below is the loader's pre-S1-8 legacy read, verbatim; the same fixture is
computed with it before the migration and with the real loader after, and the two results (the
persisted rows, the reconciliations, the credit -> cell model and the rendered report) must be
equal.

Mutations (one at a time, revert after each, run this file):

* Drop ``signal_correlation_id`` from the copy (insert NULL): the equality test fails.
* Copy terminal ``venue_credit_state`` rows too (drop ``NOT is_terminal``): the copy and
  equality tests fail.
* Read the fill's offer id from the column only: the copy and equality tests fail.
* Order the loader's links by id only and insert fills first: the precedence unit test fails
  (tests/scripts/test_weekly_attribution_loader.py).
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
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.accounts.exchange_accounts import account_scope_clause
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
    OfferLink,
    load_and_compute,
    render_reconciliation,
)
from bfx_funding_bot.modules.live_validation.credit_attribution import CreditLifetime
from bfx_funding_bot.modules.live_validation.tables import (
    FundingCreditHistoryRow,
    FundingInterestPaymentRow,
    FundingTradeRow,
)
from tests.pg_templates import alembic, stamp_realm

from .legacy_attribution_links import PARENT, materialize
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
LINKS = "attribution_legacy_offer_links"
OPEN_CREDITS = "attribution_legacy_open_credits"


# -- the loader's legacy read before S1-8, verbatim ----------------------------------------

async def reference_legacy_offer_links(
    session: AsyncSession, account_uuid: UUID, env: str,
) -> list[OfferLink]:
    claims = (await session.execute(select(
        OfferClaimRow.venue_offer_id, OfferClaimRow.execution_decision_id,
        OfferClaimRow.signal_correlation_id,
    ).where(
        OfferClaimRow.exchange_account_id == account_uuid,
        OfferClaimRow.deployment_environment == env,
        OfferClaimRow.venue_offer_id.is_not(None),
    ))).all()
    venue_offers = (await session.execute(select(
        VenueOfferStateRow.venue_offer_id, VenueOfferStateRow.execution_decision_id,
        VenueOfferStateRow.signal_correlation_id,
    ).where(
        VenueOfferStateRow.exchange_account_id == account_uuid,
        VenueOfferStateRow.deployment_environment == env,
    ))).all()
    fills = (await session.scalars(select(EventLogRow).where(
        EventLogRow.event_type == "ORDER_FILL",
        account_scope_clause(session, account_id=str(account_uuid),
                             exchange_account_column=EventLogRow.exchange_account_id,
                             legacy_account_column=EventLogRow.account_id),
        EventLogRow.deployment_environment == env,
    ))).all()
    links = [OfferLink(str(voi), edid, scid) for voi, edid, scid in claims]
    links += [OfferLink(voi, edid, scid) for voi, edid, scid in venue_offers]
    links += [OfferLink(
        str(f.payload.get("venue_offer_id") or f.venue_offer_id or ""), None,
        str(f.payload.get("signal_correlation_id") or "") or None,
    ) for f in fills]
    return links


async def reference_legacy_open_credits(
    session: AsyncSession, account_uuid: UUID, env: str,
) -> list[CreditLifetime]:
    open_rows = (await session.scalars(select(VenueCreditStateRow).where(
        VenueCreditStateRow.exchange_account_id == account_uuid,
        VenueCreditStateRow.deployment_environment == env,
        VenueCreditStateRow.is_terminal.is_(False),
    ))).all()
    out = []
    for r in open_rows:
        if r.mts_created is None or r.rate is None or r.period_days is None:
            continue
        out.append(CreditLifetime(
            credit_id=r.credit_id, symbol=r.symbol, amount=abs(Decimal(r.amount)),
            rate=Decimal(r.rate), period_days=int(r.period_days), mts_create=int(r.mts_created),
            opened_ms=int(r.mts_created), closed_ms=None,
        ))
    return out


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
    return pg_clone(pg_templates.template("attribution_links_parent", _build_parent))


@pytest.fixture
def sync_engine(parent_url: str) -> Iterator[Any]:
    engine = create_engine(parent_url)
    yield engine
    engine.dispose()


@pytest_asyncio.fixture
async def factory(parent_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(parent_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


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


def _open(credit_id: str, *, rate: Decimal | None = RATE, terminal: bool = False,
          created: int | None = T, amount: Decimal = Decimal("-100")) -> VenueCreditStateRow:
    return VenueCreditStateRow(
        exchange_account_id=ACCOUNT, deployment_environment=ENV, credit_id=credit_id,
        symbol="fUST", amount=amount, rate=rate, period_days=2,
        status="CLOSED" if terminal else "ACTIVE", flags={}, mts_created=created,
        mts_updated=created, first_seen_event_seq=1, last_seen_event_seq=1,
        is_terminal=terminal,
    )


def _ended(credit_id: int, at: int, amount: Decimal = Decimal("100")) -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=ACCOUNT, kind="credit", credit_id=credit_id,
        deployment_environment=ENV, symbol="fUST", side=1, mts_create=at, mts_update=at + DAY,
        amount=amount, status="CLOSED", rate=RATE, period_days=2, mts_opening=at,
        mts_last_payout=at + DAY,
    )


def _trade(trade_id: int, offer: int, at: int, amount: Decimal = Decimal("100")) -> FundingTradeRow:
    return FundingTradeRow(
        exchange_account_id=ACCOUNT, trade_id=trade_id, deployment_environment=ENV,
        symbol="fUST", mts_create=at, offer_id=offer, amount=amount, rate=RATE,
        period_days=2, maker=True,
    )


async def _plant(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as s:
        for account in (ACCOUNT, OTHER):
            await s.execute(text("INSERT INTO exchange_accounts(id, venue, label) "
                                 "VALUES (:id, 'bitfinex', 'attribution')"), {"id": account})
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
            # legacy open credits: complete (one with a trade), a loan, incomplete, terminal
            _open("8001"), _open("loan:8002", amount=Decimal("50")),
            _open("8003", rate=None), _open("8004", created=None), _open("8005", terminal=True),
            # ended credits and the trades that link them to the offers above
            *[_ended(9000 + i, T + i * 1_000) for i in range(1, 6)],
            *[_trade(7000 + i, 1000 + i, T + i * 1_000) for i in range(1, 6)],
            _trade(7100, 1003, T + 3_600_000), _ended(9100, T + 3_600_000),
            _trade(7101, 1001, T - DAY),  # credit 8001's opening: a legacy open credit's cell
            FundingInterestPaymentRow(
                exchange_account_id=ACCOUNT, ledger_id=1, deployment_environment=ENV,
                currency="UST", mts=T + DAY, amount=Decimal("0.05"), balance=Decimal("500"),
                description="Margin Funding Payment on wallet funding"),
        ])
        legacy_open_credit = await s.get(VenueCreditStateRow, (ACCOUNT, ENV, "8001"))
        assert legacy_open_credit is not None
        legacy_open_credit.mts_created = T - DAY


async def _weekly(factory: async_sessionmaker[AsyncSession]) -> AttributionResult:
    return await load_and_compute(factory, account_id=str(ACCOUNT), deployment_environment=ENV,
                                  now_ms=NOW)


def _copied(engine: Any) -> tuple[set[tuple[Any, ...]], set[tuple[Any, ...]]]:
    with engine.connect() as conn:
        links = {tuple(r) for r in conn.execute(text(
            f"SELECT exchange_account_id, source, venue_offer_id, execution_decision_id, "
            f"signal_correlation_id FROM {LINKS}"))}
        credits = {tuple(r) for r in conn.execute(text(
            f"SELECT credit_id, symbol, amount, rate, period_days, mts_created FROM {OPEN_CREDITS}"))}
    return links, credits


# -- tests ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_weekly_from_the_copy_equals_the_weekly_from_the_legacy_tables(
    factory: async_sessionmaker[AsyncSession], parent_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _plant(factory)
    with monkeypatch.context() as patched:
        patched.setattr(attribution_loader, "legacy_offer_links", reference_legacy_offer_links)
        patched.setattr(attribution_loader, "legacy_open_credits", reference_legacy_open_credits)
        legacy = await _weekly(factory)
    alembic(parent_url, "upgrade", "head")
    copied = await _weekly(factory)

    assert legacy.cells is not None and legacy.rows, "the fixture must attribute something"
    assert {legacy.cells.cell_by_credit[c] for c in ("9001", "9002", "9003", "9004", "9100")} == {
        "fUST_p2", "fUST_p30", "fUST_a30", "fUST_p7"}
    assert legacy.cells.cell_by_credit["9005"] == "unattributed"  # 1005: no correlation
    assert legacy.cells.cell_by_credit["8001"] == "fUST_p2"        # a legacy open credit
    assert {"loan:8002", "8001"} <= set(legacy.cells.cell_by_credit)
    assert not {"8003", "8004", "8005"} & set(legacy.cells.cell_by_credit)

    assert copied.rows == legacy.rows                      # persisted rows, in order
    assert copied.reconciliations == legacy.reconciliations
    assert copied.cells == legacy.cells
    assert (copied.credits, copied.has_credit_history, copied.offer_conflicts) == (
        legacy.credits, legacy.has_credit_history, legacy.offer_conflicts)
    assert render_reconciliation(copied) == render_reconciliation(legacy)


@pytest.mark.asyncio
async def test_the_copy_holds_exactly_what_the_weekly_used(
    factory: async_sessionmaker[AsyncSession], parent_url: str, sync_engine: Any,
) -> None:
    await _plant(factory)
    alembic(parent_url, "upgrade", "head")
    links, credits = _copied(sync_engine)
    assert links == {
        (ACCOUNT, "claim", "1001", "d1", "s1"),
        (ACCOUNT, "claim", "1002", None, "s2"),
        (OTHER, "claim", "1001", "d1", "s1"),
        (ACCOUNT, "venue_offer", "1001", "d1", "s1"),
        (ACCOUNT, "venue_offer", "1003", None, "s3"),
        (ACCOUNT, "fill", "1004", None, "s4"),             # payload number -> text, once
        (ACCOUNT, "fill", "1005", None, None),             # column offer id, empty correlation
        (OTHER, "fill", "1006", None, "s3"),
    }
    assert credits == {
        ("8001", "fUST", Decimal("-100"), RATE, 2, T - DAY),
        ("loan:8002", "fUST", Decimal("50"), RATE, 2, T),
    }

    # Idempotent: the migration's copy run again adds nothing.
    await materialize(factory)
    assert _copied(sync_engine) == (links, credits)

    # Deterministic: down and up again copies the same rows.
    alembic(parent_url, "downgrade", PARENT)
    with sync_engine.connect() as conn:
        assert conn.scalar(text(f"SELECT to_regclass('public.{LINKS}')")) is None
        assert conn.scalar(text(f"SELECT to_regclass('public.{OPEN_CREDITS}')")) is None
    alembic(parent_url, "upgrade", "head")
    alembic(parent_url, "check")
    assert _copied(sync_engine) == (links, credits)


@pytest.mark.asyncio
async def test_the_migration_is_a_no_op_on_empty_legacy_tables(
    factory: async_sessionmaker[AsyncSession], parent_url: str, sync_engine: Any,
) -> None:
    alembic(parent_url, "upgrade", "head")
    assert _copied(sync_engine) == (set(), set())
    await materialize(factory)
    assert _copied(sync_engine) == (set(), set())
    alembic(parent_url, "check")


def _privileges(conn: Any, role: str, table: str) -> list[str]:
    return [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
                        "TRIGGER")
            if conn.scalar(text("SELECT has_table_privilege(:r, :t, :p)"),
                           {"r": role, "t": f"public.{table}", "p": p})]


def test_the_weekly_role_only_reads_the_copy(parent_url: str, sync_engine: Any) -> None:
    alembic(parent_url, "upgrade", "head")
    with sync_engine.connect() as conn:
        for table in (LINKS, OPEN_CREDITS):
            assert _privileges(conn, "bfx_bot", table) == ["SELECT"], table
            assert _privileges(conn, "bfx_webapi", table) == [], table
            assert _privileges(conn, "bfx_webauth", table) == [], table
            assert _privileges(conn, "public", table) == [], table
        for role in ("bfx_bot", "bfx_webapi"):
            for privilege in ("USAGE", "SELECT", "UPDATE"):
                assert not conn.scalar(
                    text("SELECT has_sequence_privilege(:r, :s, :p)"),
                    {"r": role, "s": f"public.{LINKS}_id_seq", "p": privilege}), (role, privilege)
    with sync_engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(
            f"INSERT INTO {OPEN_CREDITS} VALUES ('{ACCOUNT}', 'ci', '1', 'fUST', 1, 1, 2, 1)")
    with sync_engine.connect() as conn:
        conn.exec_driver_sql("SET ROLE bfx_bot")
        assert conn.scalar(text(f"SELECT count(*) FROM {LINKS}")) == 0
