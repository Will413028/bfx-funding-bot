"""run_weekly_attribution: per-cell interest from stored venue credits, the
offer → cell resolution, the ledger reconciliation, and the replace semantics.

Credit 466642176 / trade 432914136 / offer 5123273052 are the live account's
(2026-09-25: 150.76884612 fUST at 0.00019999, repaid after 842 s)."""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.apps.bot
import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.diagnostics.tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.funding_stats.tables
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.ledger.attribution_reads import JournalOfferCell
from bfx_funding_bot.modules.ledger.tables import (
    ExecutionResolutionJournalRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
    VenueCreditMirrorRow,
)
from bfx_funding_bot.modules.live_validation.attribution_loader import (
    OfferCellConflict,
    OfferLink,
    load_and_compute,
    merge_offer_cells,
    persist_rows,
    reconciliation_weeks,
    render_reconciliation,
    resolve_offer_cells,
)
from bfx_funding_bot.modules.live_validation.tables import (
    AttributionLegacyOfferLinkRow,
    AttributionLegacyOpenCreditRow,
    AttributionWeeklyRow,
    FundingCreditHistoryRow,
    FundingInterestPaymentRow,
    FundingTradeRow,
)
from bfx_funding_bot.modules.live_validation.weekly_attribution import WeeklyCellRow

_ENV = "prod"
_UUID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_ACCT = str(_UUID)
_LEGACY = "default"
_MON = 1_789_948_800_000          # 2026-09-21 UTC Monday
_DAY = 86_400_000
AMOUNT = Decimal("150.76884612")
RATE = Decimal("0.00019999")
CREATED = 1_790_350_246_000       # 2026-09-25 15:30:46Z
REPAID = 1_790_351_088_000        # + 842 s
OFFER = 5_123_273_052
NOW = _MON + 3 * 7 * _DAY


@pytest_asyncio.fixture
async def sf(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


def _credit(credit_id: int = 466642176, *, amount: Decimal = AMOUNT, kind: str = "credit",
            closed: int | None = REPAID) -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=_UUID, kind=kind, credit_id=credit_id, deployment_environment=_ENV,
        symbol="fUST", side=1, mts_create=CREATED, mts_update=CREATED, amount=amount,
        status="CLOSED", rate=RATE, period_days=2, mts_opening=CREATED, mts_last_payout=closed,
    )


def _trade(trade_id: int = 432914136, offer_id: int = OFFER) -> FundingTradeRow:
    return FundingTradeRow(
        exchange_account_id=_UUID, trade_id=trade_id, deployment_environment=_ENV,
        symbol="fUST", mts_create=CREATED, offer_id=offer_id, amount=AMOUNT, rate=RATE,
        period_days=2, maker=None,
    )


def _decision(decision_id: str, cell: str, scid: str = "scid-x") -> ExecutionDecisionRow:
    return ExecutionDecisionRow(
        decision_id=decision_id, account_id=_ACCT, exchange_account_id=_UUID,
        deployment_environment=_ENV, reconcile_id="r", cell_id=cell, symbol="fUST",
        signal_correlation_id=scid, outcome="submitted", signal_rate=RATE,
        amount_usdt=AMOUNT, duration_days=2, model_evidence={}, safety_result={},
        execution_policy="p", service_version="v", config_hash="h",
        occurred_at_ms=CREATED - 60_000, recorded_at_ms=CREATED - 60_000,
    )


def _link(source: str, voi: str, decision_id: str | None,
          scid: str | None) -> AttributionLegacyOfferLinkRow:
    """A legacy offer link as migration a0b1c2d3e4f5 copied it."""
    return AttributionLegacyOfferLinkRow(
        exchange_account_id=_UUID, deployment_environment=_ENV, source=source,
        venue_offer_id=voi, execution_decision_id=decision_id, signal_correlation_id=scid,
    )


def _claim(voi: str, decision_id: str | None) -> AttributionLegacyOfferLinkRow:
    return _link("claim", voi, decision_id, "scid-x")


GROSS_842S = AMOUNT * RATE * Decimal(842_000) / Decimal(_DAY)


async def test_credit_attributed_through_trade_offer_and_execution_decision(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade(), _claim(str(OFFER), "d1"), _decision("d1", "fUST_p2")])
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    row = next(r for r in result.rows if r.cell == "fUST_p2" and r.week_start_ms == _MON)
    assert row.n_fills == 1
    assert row.gross_interest_usdt == GROSS_842S          # 842 s, not 0 or 2 days
    assert row.net_interest_usdt == GROSS_842S * Decimal("0.85")


async def test_fill_signal_correlation_falls_back_to_diagnostics_decision(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade()])
        s.add(_link("fill", str(OFFER), None, "scid-old"))
        s.add(DiagnosticsRow(
            account_id=_ACCT, exchange_account_id=_UUID, deployment_environment=_ENV,
            kind="decision", payload={"cell": "fUST_a30", "correlation_id": "scid-old"},
            occurred_at=datetime.now(UTC),
        ))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert {r.cell for r in result.rows if r.n_fills} == {"fUST_a30"}


async def test_unmatched_credit_and_open_credit_are_unattributed_and_accrue(sf):
    async with sf() as s:
        s.add(_credit(1, amount=Decimal("99")))            # no trade
        s.add(AttributionLegacyOpenCreditRow(
            exchange_account_id=_UUID, deployment_environment=_ENV, credit_id="loan:7",
            symbol="fUST", amount=Decimal("100"), rate=Decimal("0.0002"), period_days=2,
            mts_created=NOW - _DAY,
        ))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert result.cells is not None and result.cells.without_trade == {"1", "loan:7"}
    open_week = next(r for r in result.rows if r.week_start_ms == NOW - 7 * _DAY and r.n_fills)
    assert open_week.cell == "unattributed"
    assert open_week.gross_interest_usdt == Decimal("100") * Decimal("0.0002")   # one day so far


async def test_loan_turned_credit_is_one_fill_of_its_trades_cell(sf):
    """Live 2026-09-22: loan 61621685 (150.77638588, opened 18:08:30Z) became
    credit 466451710 -- same amount and opening, new id and MTS_CREATE -- and
    trade 432678437 is at the opening. The trade's rate is quoted with fewer
    digits than the credit's. The conversion time (one hour in) is illustrative."""
    opened, used, last_payout = 1_790_100_510_000, 1_790_104_110_000, 1_790_273_311_000
    amount, rate = Decimal("150.77638588"), Decimal("0.0001482")

    def row(kind: str, credit_id: int, created: int, closed: int) -> FundingCreditHistoryRow:
        return FundingCreditHistoryRow(
            exchange_account_id=_UUID, kind=kind, credit_id=credit_id,
            deployment_environment=_ENV, symbol="fUST", side=1, mts_create=created,
            mts_update=closed, amount=amount, status="CLOSED", rate=rate, period_days=2,
            mts_opening=opened, mts_last_payout=closed)

    async with sf() as s:
        s.add_all([
            row("loan", 61621685, opened, used), row("credit", 466451710, used, last_payout),
            FundingTradeRow(
                exchange_account_id=_UUID, trade_id=432678437, deployment_environment=_ENV,
                symbol="fUST", mts_create=opened, offer_id=OFFER, amount=amount,
                rate=Decimal("0.000148"), period_days=2, maker=None),
            _claim(str(OFFER), "d1"), _decision("d1", "fUST_p2"),
        ])
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert result.cells is not None
    assert result.cells.cell_by_credit == {"loan:61621685": "fUST_p2", "466451710": "fUST_p2"}
    [row_] = [r for r in result.rows if r.cell == "fUST_p2" and r.week_start_ms == _MON]
    assert row_.n_fills == 1
    # lent once from opening to last payout, not twice for the loan's hour
    assert row_.capital_days == amount * Decimal(last_payout - opened) / Decimal(_DAY)
    assert row_.gross_interest_usdt == row_.capital_days * rate


async def test_reconciliation_compares_the_shifted_ledger_week(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade()])
        # the payout for Friday 09-25 lands Saturday ~01:30Z
        s.add(FundingInterestPaymentRow(
            exchange_account_id=_UUID, ledger_id=1, deployment_environment=_ENV,
            currency="UST", mts=CREATED + _DAY // 2, amount=Decimal("0.00025"),
            balance=Decimal("395"), description="Margin Funding Payment on wallet funding",
        ))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV,
                                    now_ms=NOW, reconcile_weeks=2)
    week = next(r for r in result.reconciliations if r.week_start_ms == _MON)
    assert week.payouts == 1 and week.complete
    assert week.credit_net == GROSS_842S * Decimal("0.85")
    assert not week.flagged                   # |diff| under the 0.01 floor
    assert "| UST | 2026-09-21 |" in render_reconciliation(result)


def test_reconciliation_weeks_wait_for_tuesday_payouts():
    monday_run = _MON + 7 * _DAY + 4 * 3_600_000       # Mon 04:00Z after the week
    assert reconciliation_weeks(monday_run, 1) == [_MON - 7 * _DAY]
    assert reconciliation_weeks(monday_run + 2 * _DAY, 1) == [_MON]


async def test_legacy_links_keep_the_legacy_source_precedence(sf):
    """Two sources name one offer by signal correlation only, in different cells: the earlier
    legacy source (venue offer before fill) wins whatever order the rows were written in, and
    an audited decision of a later source still overrides both."""
    async with sf() as s:
        s.add_all([_credit(), _trade(), _link("fill", str(OFFER), None, "scid-fill"),
                   _link("venue_offer", str(OFFER), None, "scid-vo"),
                   _decision("d-fill", "fUST_a30", scid="scid-fill"),
                   _decision("d-vo", "fUST_p2", scid="scid-vo")])
        await s.commit()
    assert await _attributed_cells(sf) == {"fUST_p2"}
    async with sf() as s:
        s.add_all([_link("claim", str(OFFER), "d-claim", "scid-x"),
                   _link("venue_offer", str(OFFER), "d-late", None),
                   _decision("d-claim", "fUST_p7", scid="scid-claim"),
                   _decision("d-late", "fUST_p30", scid="scid-late")])
        await s.commit()
    assert await _attributed_cells(sf) == {"fUST_p30"}  # the last audited decision


def test_audited_decision_wins_over_signal_correlation():
    links = [OfferLink("1", None, "s"), OfferLink("1", "d", None), OfferLink("2", None, "s")]
    cells = resolve_offer_cells(links, cell_by_decision={"d": "fUST_p2"},
                                cell_by_scid={"s": "fUST_a30"})
    assert cells == {"1": "fUST_p2", "2": "fUST_a30"}


async def test_no_credit_history_reports_it(sf):
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert not result.has_credit_history and result.rows == []
    legacy = await load_and_compute(sf, account_id=_LEGACY, deployment_environment=_ENV)
    assert not legacy.has_credit_history


async def test_loader_builds_utilization_points_from_funding_stats(sf):
    """funding_amount(_used) present → utilization point used/total; missing or
    zero total → no point for that snapshot (None column ≠ 0 utilization)."""
    async with sf() as s:
        s.add(_credit())
        # 三筆 snapshot：(1000, 800) → 0.8；(None, 500) → skip；(0, 0) → skip。週均 = 0.8。
        s.add(FundingStatRow(symbol="fUST", mts=CREATED + 2000, frr=0.0002, avg_period=2.0,
                             funding_amount=1000.0, funding_amount_used=800.0))
        s.add(FundingStatRow(symbol="fUST", mts=CREATED + 3000, frr=0.0002, avg_period=2.0,
                             funding_amount=None, funding_amount_used=500.0))
        s.add(FundingStatRow(symbol="fUST", mts=CREATED + 4000, frr=0.0002, avg_period=2.0,
                             funding_amount=0.0, funding_amount_used=0.0))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    row = next(r for r in result.rows if r.baseline_frr_apr_net_pct is not None)
    assert row.baseline_frr_util_apr_net_pct == row.baseline_frr_apr_net_pct * Decimal("0.8")


def _wcr(cell: str, wk: int = _MON) -> WeeklyCellRow:
    return WeeklyCellRow(
        cell=cell, week_start_ms=wk, week_end_ms=wk + 604_800_000, n_fills=1,
        gross_interest_usdt=Decimal("0.2"), net_interest_usdt=Decimal("0.17"),
        capital_days=Decimal("1000"), realized_apr_net_pct=Decimal("6.205"),
        baseline_close_apr_net_pct=None, baseline_frr_apr_net_pct=None,
        baseline_frr_util_apr_net_pct=None,
    )


async def test_persist_rows_upserts_idempotently(sf):
    rows = [_wcr("fUST_p2"), _wcr("unattributed")]
    n1 = await persist_rows(sf, rows, account_id=_LEGACY, deployment_environment=_ENV)
    n2 = await persist_rows(sf, rows, account_id=_LEGACY, deployment_environment=_ENV)
    assert n1 == n2 == 2
    async with sf() as s:
        db_rows = (await s.execute(select(AttributionWeeklyRow))).scalars().all()
    assert len(db_rows) == 2  # 重跑不重複


async def test_persist_rows_replaces_stale_rows(sf):
    # full-recompute replace：某 credit 重歸 unattributed 後，舊 cell 的 row 不可殘留。
    await persist_rows(
        sf, [_wcr("fUST_p2"), _wcr("unattributed")],
        account_id=_LEGACY, deployment_environment=_ENV,
    )
    n = await persist_rows(
        sf, [_wcr("fUST_p2")], account_id=_LEGACY, deployment_environment=_ENV,
    )
    assert n == 1
    async with sf() as s:
        db_rows = (await s.execute(select(AttributionWeeklyRow))).scalars().all()
    assert {r.cell for r in db_rows} == {"fUST_p2"}


async def test_persist_rows_replace_is_realm_scoped(sf):
    # delete 以 (env,account) 為界 — 不可波及其他 realm 的 rows。
    async with sf() as s:
        s.add(AttributionWeeklyRow(
            deployment_environment="other", account_id=_LEGACY, cell="fUST_p2",
            week_start_ms=_MON, week_end_ms=_MON + 604_800_000, n_fills=9,
            gross_interest_usdt=Decimal("1"), net_interest_usdt=Decimal("1"),
            capital_days=Decimal("1"), realized_apr_net_pct=None,
            baseline_close_apr_net_pct=None, baseline_frr_apr_net_pct=None,
        ))
        await s.commit()
    await persist_rows(
        sf, [_wcr("fUST_p2")], account_id=_LEGACY, deployment_environment=_ENV,
    )
    async with sf() as s:
        others = (await s.execute(
            select(AttributionWeeklyRow).where(
                AttributionWeeklyRow.deployment_environment == "other"
            )
        )).scalars().all()
    assert len(others) == 1


# ---- S1-6: after the switch new offers exist only in the ledger journal ----------------

def _u(n: int) -> UUID:
    # not numeric-looking: sqlite's NUMERIC affinity would read an all-digit hex id as an int
    return UUID(f"aaaaaaaa-0000-4000-8000-{n:012d}")


def _attempt(voi: str | None, cell: str, *, seq: int = 1, kind: str = "ack",
             seeded: bool = False) -> list[object]:
    attempt_id = _u(seq)
    return [
        SubmissionAttemptJournalRow(
            attempt_id=attempt_id, execution_decision_id=f"jd{seq}", exchange_account_id=_UUID,
            deployment_environment=_ENV, symbol="fUST", cell_id=cell, attempt_seq=seq,
            normalized_payload={"amount": str(AMOUNT), "rate": str(RATE), "period": 2},
            payload_sha256="x", basis_id=_u(99), authorization_evidence={},
            seed_provenance={"legacy": "offer_claim"} if seeded else None,
            started_at_ms=CREATED - 1_000,
        ),
        TransportOutcomeJournalRow(
            attempt_id=attempt_id, kind=kind, venue_offer_id=voi if kind == "ack" else None,
            completed_at_ms=CREATED, evidence={},
        ),
    ]


async def _attributed_cells(sf) -> set[str]:
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    return {r.cell for r in result.rows if r.n_fills}


async def test_journal_only_offer_is_attributed_to_its_cell(sf):
    """The legacy tables know nothing of the offer (post-switch): the journal's ack does."""
    async with sf() as s:
        s.add_all([_credit(), _trade(), *_attempt(str(OFFER), "fUST_p2")])
        await s.commit()
    assert await _attributed_cells(sf) == {"fUST_p2"}
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert result.cells is not None and not result.cells.foreign_offer
    assert result.offer_conflicts == ()


async def test_only_acknowledged_attempts_link_an_offer(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade(), *_attempt(None, "fUST_p2", kind="rejected")])
        await s.commit()
    assert await _attributed_cells(sf) == {"unattributed"}


async def test_seeded_attempt_agreeing_with_legacy_is_one_offer_without_conflict(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade(), _claim(str(OFFER), "d1"), _decision("d1", "fUST_p2"),
                   *_attempt(str(OFFER), "fUST_p2", seeded=True)])
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert {r.cell for r in result.rows if r.n_fills} == {"fUST_p2"}
    assert result.offer_conflicts == ()


async def test_legacy_and_journal_disagreeing_on_a_cell_is_reported_not_picked(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade(), _claim(str(OFFER), "d1"), _decision("d1", "fUST_p2"),
                   *_attempt(str(OFFER), "fUST_a30", seeded=True)])
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert result.offer_conflicts == (OfferCellConflict(str(OFFER), ("fUST_p2",), ("fUST_a30",)),)
    assert {r.cell for r in result.rows if r.n_fills} == {"unattributed"}
    assert "OFFER CELL CONFLICTS (1)" in render_reconciliation(result)


def test_merge_offer_cells_unions_and_flags_journal_self_conflicts():
    links = [JournalOfferCell("1", "c1", "d", False), JournalOfferCell("2", "c2", "d", False),
             JournalOfferCell("2", "c3", "d", False), JournalOfferCell("3", "c3", "d", True)]
    merged, conflicts = merge_offer_cells({"3": "c3", "4": "c4"}, links)
    assert merged == {"1": "c1", "3": "c3", "4": "c4"}
    assert conflicts == [OfferCellConflict("2", (), ("c2", "c3"))]


async def test_bound_to_venue_offer_is_attributed_to_its_cell(sf):
    """An UNKNOWN attempt later bound to the venue offer (resolver or operator) is a real
    submit: the ledger's provenance (ack or bound_to_venue) places it, not only ack."""
    async with sf() as s:
        s.add_all([_credit(), _trade(), *_attempt(None, "fUST_p2", kind="unknown")])
        s.add(ExecutionResolutionJournalRow(
            id=_u(500), attempt_id=_u(1), exchange_account_id=_UUID,
            deployment_environment=_ENV, symbol="fUST", action="bound_to_venue",
            venue_offer_id=str(OFFER), observation_id=_u(7), actor_kind="system",
            actor_id="resolver", resolved_at_ms=CREATED, reason="exact_fingerprint_match",
            evidence={},
        ))
        await s.commit()
    assert await _attributed_cells(sf) == {"fUST_p2"}


async def test_unresolved_unknown_attempt_does_not_link_an_offer(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade(), *_attempt(None, "fUST_p2", kind="unknown")])
        await s.commit()
    assert await _attributed_cells(sf) == {"unattributed"}


async def test_two_attempts_of_one_offer_agree_on_a_cell_or_conflict(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade(), *_attempt(str(OFFER), "fUST_p2", seq=1),
                   *_attempt(None, "fUST_p2", seq=2, kind="unknown")])
        s.add(ExecutionResolutionJournalRow(
            id=_u(500), attempt_id=_u(2), exchange_account_id=_UUID,
            deployment_environment=_ENV, symbol="fUST", action="bound_to_venue",
            venue_offer_id=str(OFFER), observation_id=_u(7), actor_kind="system",
            actor_id="resolver", resolved_at_ms=CREATED, reason="x", evidence={},
        ))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert result.offer_conflicts == ()
    assert {r.cell for r in result.rows if r.n_fills} == {"fUST_p2"}
    async with sf() as s:
        await s.execute(delete(TransportOutcomeJournalRow))
        s.add(TransportOutcomeJournalRow(attempt_id=_u(1), kind="ack",
                                         venue_offer_id=str(OFFER), completed_at_ms=CREATED,
                                         evidence={}))
        s.add(TransportOutcomeJournalRow(attempt_id=_u(2), kind="unknown",
                                         completed_at_ms=CREATED, evidence={}))
        attempt = await s.get(SubmissionAttemptJournalRow, _u(2))
        assert attempt is not None
        attempt.cell_id = "fUST_a30"
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert result.offer_conflicts == (
        OfferCellConflict(str(OFFER), (), ("fUST_a30", "fUST_p2")),)


def _mirror(credit_id: str, *, terminal: bool = False, present: bool = True, kind: str = "credit",
            created: int = CREATED, opening: int = CREATED,
            updated: int = CREATED) -> VenueCreditMirrorRow:
    return VenueCreditMirrorRow(
        exchange_account_id=_UUID, deployment_environment=_ENV, venue_credit_id=credit_id,
        source_kind=kind, symbol="fUST", amount=AMOUNT, rate=RATE, period_days=2,
        status="closed" if terminal else "active", mts_created=created, mts_updated=updated,
        mts_opening=opening, last_accepted_observation_id=_u(7),
        present_in_latest_accepted_snapshot=present and not terminal,
        terminal_kind="closed" if terminal else None,
        terminal_evidence_id=_u(8) if terminal else None,
    )


def _legacy_open(credit_id: str, *, created: int = CREATED) -> AttributionLegacyOpenCreditRow:
    return AttributionLegacyOpenCreditRow(
        exchange_account_id=_UUID, deployment_environment=_ENV, credit_id=credit_id,
        symbol="fUST", amount=AMOUNT, rate=RATE, period_days=2, mts_created=created,
    )


async def _computed(sf):
    return await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)


async def test_mirror_only_open_credit_is_attributed_to_its_cell(sf):
    """Post-switch: no history row, no legacy open row; the mirror has the open credit and
    the journal its offer."""
    async with sf() as s:
        s.add_all([_history_anchor(), _trade(), *_attempt(str(OFFER), "fUST_p2"),
                   _mirror("500")])
        await s.commit()
    result = await _computed(sf)
    assert result.cells is not None
    assert set(result.cells.cell_by_credit) == {"1", "500"}
    assert result.cells.cell_by_credit["500"] == "fUST_p2"
    assert result.credits == 2


def _history_anchor() -> FundingCreditHistoryRow:
    """An ended credit of another opening so the account has credit history."""
    return FundingCreditHistoryRow(
        exchange_account_id=_UUID, kind="credit", credit_id=1, deployment_environment=_ENV,
        symbol="fUST", side=1, mts_create=CREATED - _DAY, mts_update=CREATED - _DAY + 1_000,
        amount=Decimal("10"), status="CLOSED", rate=RATE, period_days=2,
        mts_opening=CREATED - _DAY, mts_last_payout=CREATED - _DAY + 1_000,
    )


async def test_mirror_credit_is_not_revived_from_a_stale_legacy_open_row(sf):
    async with sf() as s:
        s.add_all([_history_anchor(), _mirror("501", terminal=True), _legacy_open("501")])
        await s.commit()
    result = await _computed(sf)
    assert result.cells is not None
    assert set(result.cells.cell_by_credit) == {"1", "501"}
    # ended at its mirror end (mts_updated == opening): no accrual into later weeks
    assert all(r.week_start_ms < _MON + 7 * _DAY for r in result.rows
               if r.cell == "unattributed" and r.gross_interest_usdt > 0)


async def test_an_incomplete_mirror_row_does_not_hide_the_legacy_open_row(sf):
    incomplete = _mirror("503")
    incomplete.rate = None
    async with sf() as s:
        s.add_all([_history_anchor(), incomplete, _legacy_open("503")])
        await s.commit()
    result = await _computed(sf)
    assert result.cells is not None
    assert set(result.cells.cell_by_credit) == {"1", "503"}


async def test_terminal_mirror_credit_not_yet_in_history_counts_to_its_end(sf):
    async with sf() as s:
        s.add_all([_history_anchor(), _trade(), *_attempt(str(OFFER), "fUST_p2"),
                   _mirror("502", terminal=True, updated=REPAID)])
        await s.commit()
    result = await _computed(sf)
    row = next(r for r in result.rows if r.cell == "fUST_p2" and r.week_start_ms == _MON)
    assert row.gross_interest_usdt == GROSS_842S  # held CREATED..REPAID, ended not open


async def test_mirror_credit_gone_from_the_snapshot_without_terminal_evidence_stays_open(sf):
    async with sf() as s:
        s.add_all([_history_anchor(), _mirror("503", present=False)])
        await s.commit()
    result = await _computed(sf)
    assert result.cells is not None and "503" in result.cells.cell_by_credit


async def test_mirror_opening_wins_over_legacy_created_for_a_loan_derived_credit(sf):
    """A credit that came from a loan: created a day after its venue opening. The legacy open
    row only had ``mts_created`` and matched no trade; the mirror's ``mts_opening`` matches the
    originating trade, so the credit now lands in the trade's cell and week (as it will once
    it is in funding_credit_history)."""
    created = CREATED + _DAY
    async with sf() as s:
        s.add_all([_history_anchor(), _trade(), *_attempt(str(OFFER), "fUST_p2"),
                   _legacy_open("600", created=created)])
        await s.commit()
    legacy_only = await _computed(sf)
    assert legacy_only.cells is not None
    assert legacy_only.cells.cell_by_credit["600"] == "unattributed"
    async with sf() as s:
        s.add(_mirror("600", created=created, opening=CREATED, updated=created))
        await s.commit()
    result = await _computed(sf)
    assert result.cells is not None
    assert result.cells.cell_by_credit["600"] == "fUST_p2"
    weeks = {r.week_start_ms: r.n_fills for r in result.rows if r.cell == "fUST_p2"}
    assert min(weeks) == _MON and weeks[_MON] == 1  # the fill is the opening week's
    assert "600" not in result.cells.without_trade
