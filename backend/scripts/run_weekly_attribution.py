"""E3 (b) — per-cell weekly fee-adjusted realized interest → attribution_weekly,
reconciled against the venue ledger.

Since 2026-09-27 the per-cell numbers come from venue credit records, not from
ORDER_FILL × held-to-term capped by CREDIT_CLOSED (whose rate/period/close were
parsed one slot late until then). Inputs, all read-only:
- funding_credit_history (ended credits/loans: rate, period, opening, actual
  close) + venue_credit_state (credits/loans still open): per-credit truth;
- funding_trades (credit → our offer id) + offer_claims / venue_offer_state /
  ORDER_FILL (offer → execution decision / signal correlation id) +
  execution_decisions / diagnostics DECISION (→ cell);
- funding_interest_payments (the ledger) for the weekly reconciliation;
- funding_candles / funding_stats for the baselines.
Matching, accrual and the reconciliation window: modules/live_validation/
credit_attribution.py. The only write is attribution_weekly (full recompute +
replace; a read model).

Run from backend/ (env: DATABASE_URL / BFX_EXCHANGE_ACCOUNT_ID /
BFX_DEPLOYMENT_ENV):
  uv run python -m scripts.run_weekly_attribution [--weeks 8] [--out FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings, require_deployment_environment
from bfx_funding_bot.external.bitfinex.auth_rest import LOAN_ID_PREFIX, InterestPayment
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_canonical,
    account_id_uuid_or_none,
    account_scope_clause,
)
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.live_validation.credit_attribution import (
    PAYOUT_LAG_MS,
    PAYOUT_SETTLE_MS,
    CreditCells,
    CreditLifetime,
    TradeRecord,
    WeeklyReconciliation,
    assign_cells,
    reconcile_week,
    weekly_totals,
)
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    funding_currency,
)
from bfx_funding_bot.modules.live_validation.live_attribution import (
    MarketRatePoint,
    frr_points_from_stats,
)
from bfx_funding_bot.modules.live_validation.tables import (
    AttributionWeeklyRow,
    FundingCreditHistoryRow,
    FundingInterestPaymentRow,
    FundingTradeRow,
)
from bfx_funding_bot.modules.live_validation.weekly_attribution import (
    WEEK_MS,
    WeeklyCellRow,
    calendar_week_start,
    compute_weekly_rows,
)

log = logging.getLogger(__name__)

_FILL_TYPE = "ORDER_FILL"
_DECISION_KIND = "decision"
_MARKET_SYMBOL = "fUST"
_MARKET_TIMEFRAME = "1h"
_MARKET_PERIOD_AGG = "p2"


@dataclass(frozen=True)
class OfferLink:
    """What our own records say about one venue offer."""

    venue_offer_id: str
    execution_decision_id: str | None
    signal_correlation_id: str | None


def resolve_offer_cells(
    links: Iterable[OfferLink],
    *,
    cell_by_decision: dict[str, str],
    cell_by_scid: dict[str, str],
) -> dict[str, str]:
    """venue offer id → cell. The audited execution decision wins; the signal
    correlation id (execution_decisions, then best-effort diagnostics) is the
    fallback for offers placed before decisions were recorded."""
    out: dict[str, str] = {}
    for link in links:
        by_decision = cell_by_decision.get(link.execution_decision_id or "")
        cell = by_decision or cell_by_scid.get(link.signal_correlation_id or "")
        if cell and (by_decision or link.venue_offer_id not in out):
            out[link.venue_offer_id] = cell
    return out


@dataclass(frozen=True)
class AttributionResult:
    rows: list[WeeklyCellRow]
    reconciliations: list[WeeklyReconciliation]
    credits: int
    cells: CreditCells | None
    has_credit_history: bool


def _credit_from_history(r: FundingCreditHistoryRow) -> CreditLifetime:
    # An ended row without MTS_LAST_PAYOUT has not been seen; MTS_UPDATE is the
    # best remaining bound for its close.
    closed = r.mts_last_payout if r.mts_last_payout is not None else r.mts_update
    prefix = LOAN_ID_PREFIX if r.kind == "loan" else ""
    return CreditLifetime(
        credit_id=f"{prefix}{r.credit_id}", symbol=r.symbol, amount=Decimal(r.amount),
        rate=Decimal(r.rate), period_days=int(r.period_days), mts_create=int(r.mts_create),
        opened_ms=int(r.mts_opening), closed_ms=int(closed),
    )


def _open_credit(r: VenueCreditStateRow) -> CreditLifetime | None:
    if r.mts_created is None or r.rate is None or r.period_days is None:
        return None
    return CreditLifetime(
        credit_id=r.credit_id, symbol=r.symbol, amount=abs(Decimal(r.amount)),
        rate=Decimal(r.rate), period_days=int(r.period_days), mts_create=int(r.mts_created),
        opened_ms=int(r.mts_created), closed_ms=None,
    )


def reconciliation_weeks(now_ms: int, weeks: int) -> list[int]:
    """The last `weeks` calendar weeks whose payouts are all in, oldest first.

    Week W is paid by Tuesday ~01:30Z after it ends, so the Monday 04:17Z
    weekly run reconciles up to the week before last."""
    latest = calendar_week_start(now_ms - PAYOUT_LAG_MS - PAYOUT_SETTLE_MS) - WEEK_MS
    return [latest - i * WEEK_MS for i in reversed(range(weeks))]


async def load_and_compute(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: str,
    deployment_environment: str,
    now_ms: int | None = None,
    reconcile_weeks: int = 8,
) -> AttributionResult:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    account_uuid = account_id_uuid_or_none(account_id)
    if account_uuid is None:
        # Venue read models are keyed by the ExchangeAccount UUID only.
        return AttributionResult([], [], 0, None, has_credit_history=False)
    env = deployment_environment

    def scoped(session: AsyncSession, table: Any) -> Any:
        return account_scope_clause(
            session, account_id=account_id,
            exchange_account_column=table.exchange_account_id,
            legacy_account_column=table.account_id,
        )

    async with session_factory() as session:
        history = (await session.scalars(select(FundingCreditHistoryRow).where(
            FundingCreditHistoryRow.exchange_account_id == account_uuid,
            FundingCreditHistoryRow.deployment_environment == env,
        ))).all()
        open_rows = (await session.scalars(select(VenueCreditStateRow).where(
            VenueCreditStateRow.exchange_account_id == account_uuid,
            VenueCreditStateRow.deployment_environment == env,
            VenueCreditStateRow.is_terminal.is_(False),
        ))).all()
        trade_rows = (await session.scalars(select(FundingTradeRow).where(
            FundingTradeRow.exchange_account_id == account_uuid,
            FundingTradeRow.deployment_environment == env,
        ))).all()
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
            EventLogRow.event_type == _FILL_TYPE, scoped(session, EventLogRow),
            EventLogRow.deployment_environment == env,
        ))).all()
        decisions = (await session.execute(select(
            ExecutionDecisionRow.decision_id, ExecutionDecisionRow.signal_correlation_id,
            ExecutionDecisionRow.cell_id,
        ).where(
            scoped(session, ExecutionDecisionRow),
            ExecutionDecisionRow.deployment_environment == env,
        ))).all()
        diagnostics = (await session.scalars(select(DiagnosticsRow).where(
            DiagnosticsRow.kind == _DECISION_KIND, scoped(session, DiagnosticsRow),
            DiagnosticsRow.deployment_environment == env,
        ))).all()
        ledger_rows = (await session.scalars(select(FundingInterestPaymentRow).where(
            FundingInterestPaymentRow.exchange_account_id == account_uuid,
            FundingInterestPaymentRow.deployment_environment == env,
        ))).all()

        credits = [_credit_from_history(r) for r in history]
        seen = {c.credit_id for c in credits}
        for r in open_rows:
            open_credit = _open_credit(r)
            if open_credit is not None and open_credit.credit_id not in seen:
                credits.append(open_credit)
        if not credits:
            return AttributionResult([], [], 0, None, has_credit_history=bool(history))

        start = min(c.opened_ms for c in credits)
        candles = await get_candles_in_range(
            session, symbol=_MARKET_SYMBOL, timeframe=_MARKET_TIMEFRAME,
            period_agg=_MARKET_PERIOD_AGG, start_mts=start, end_mts=now,
        )
        frr_rows = (await session.scalars(
            select(FundingStatRow).where(
                FundingStatRow.symbol == _MARKET_SYMBOL,
                FundingStatRow.mts >= start, FundingStatRow.mts <= now,
            ).order_by(FundingStatRow.mts)
        )).all()

    trades = [TradeRecord(
        trade_id=int(t.trade_id), symbol=t.symbol, mts_create=int(t.mts_create),
        offer_id=str(t.offer_id), amount=abs(Decimal(t.amount)), rate=Decimal(t.rate),
        period_days=int(t.period_days),
    ) for t in trade_rows]
    cell_by_scid = {
        str(d.payload.get("correlation_id")): str(d.payload.get("cell"))
        for d in diagnostics
        if d.payload.get("correlation_id") and d.payload.get("cell")
    }
    cell_by_scid.update({scid: cell for _id, scid, cell in decisions})
    links = [OfferLink(str(voi), edid, scid) for voi, edid, scid in claims]
    links += [OfferLink(voi, edid, scid) for voi, edid, scid in venue_offers]
    links += [OfferLink(
        str(f.payload.get("venue_offer_id") or f.venue_offer_id or ""), None,
        str(f.payload.get("signal_correlation_id") or "") or None,
    ) for f in fills]
    offer_cells = resolve_offer_cells(
        links, cell_by_decision={did: cell for did, _scid, cell in decisions},
        cell_by_scid=cell_by_scid,
    )
    cells = assign_cells(credits, trades, offer_cells)

    frr_stats = [
        FundingStat(
            symbol=r.symbol, mts=r.mts,
            frr=Decimal(str(r.frr)) if r.frr is not None else None,
            avg_period=Decimal(str(r.avg_period)) if r.avg_period is not None else None,
        )
        for r in frr_rows
    ]
    rows = compute_weekly_rows(
        totals_by_cell=weekly_totals(credits, cells.cell_by_credit, now_ms=now),
        close_points=[MarketRatePoint(mts=c.mts, rate=c.close)
                      for c in candles if c.close is not None],
        frr_points=frr_points_from_stats(frr_stats),
        utilization_points=[
            MarketRatePoint(
                mts=r.mts,
                rate=Decimal(str(r.funding_amount_used)) / Decimal(str(r.funding_amount)),
            )
            for r in frr_rows
            if r.funding_amount is not None and r.funding_amount_used is not None
            and r.funding_amount > 0
        ],
    )
    payments = [InterestPayment(r.ledger_id, r.currency, None, r.mts, Decimal(r.amount),
                                Decimal(r.balance), r.description) for r in ledger_rows]
    reconciliations = [
        reconcile_week(credits, payments, currency=currency, week_start_ms=week, now_ms=now)
        for currency in sorted({funding_currency(c.symbol) for c in credits})
        for week in reconciliation_weeks(now, reconcile_weeks)
    ]
    return AttributionResult(rows, reconciliations, len(credits), cells,
                             has_credit_history=bool(history))


def render_reconciliation(result: AttributionResult) -> str:
    lines = [
        "Per-cell interest from venue credits (net of the 15% fee) vs the venue "
        "ledger. Ledger window = credit week shifted one day (each UTC day is "
        "paid ~01:30Z the next day). Flag: |diff| > max(5% of ledger, 0.01).",
        "",
        "| currency | week (UTC) | credits net | ledger net | payouts | diff | diff % | status |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in result.reconciliations:
        week = time.strftime("%Y-%m-%d", time.gmtime(r.week_start_ms / 1000))
        pct = f"{r.diff_pct:+.1f}%" if r.diff_pct is not None else "—"
        status = "FLAG" if r.flagged else ("ok" if r.complete else "incomplete")
        lines.append(f"| {r.currency} | {week} | {r.credit_net:.6f} | {r.ledger_net:.6f} "
                     f"| {r.payouts} | {r.diff:+.6f} | {pct} | {status} |")
    if result.cells is not None:
        c = result.cells
        lines += ["", f"credits: {result.credits}; without trade: {len(c.without_trade)}; "
                      f"offer not ours/unresolved: {len(c.foreign_offer)}; "
                      f"ambiguous pairing: {len(c.ambiguous)} (all go to their paired cell, "
                      "unmatched to 'unattributed')"]
    return "\n".join(lines) + "\n"


async def persist_rows(
    session_factory: async_sessionmaker[AsyncSession],
    rows: list[WeeklyCellRow],
    *,
    account_id: str,
    deployment_environment: str,
) -> int:
    """Full-recompute replace：單一 transaction 內先刪本 realm 既有 rows 再插新的。

    本表是純 read model，故 replace 語意讓表永遠等於「以現在的 SoT 重算」。
    merge-only 只能新增/覆蓋、不能縮減：一旦某 credit 重歸 unattributed，舊
    (cell,week) row 會殘留舊 interest（read model 漏）。delete+insert 同 transaction
    是 atomic（失敗 rollback，表不變）；空 rows 正確清空該 realm。delete 以
    (env,account) 為界，不動其他 realm。
    """
    async with session_factory() as session:
        await session.execute(
            delete(AttributionWeeklyRow).where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=AttributionWeeklyRow.exchange_account_id,
                    legacy_account_column=AttributionWeeklyRow.account_id,
                ),
                AttributionWeeklyRow.deployment_environment == deployment_environment,
            )
        )
        # Insert one row at a time.  SQLite's insertmanyvalues RETURNING path
        # cannot correlate nullable transitional composite-PK rows; production
        # PostgreSQL is still a single transaction and the weekly result set is
        # small enough that this avoids a fixture-only ORM identity failure.
        for r in rows:
            session.add(AttributionWeeklyRow(
                deployment_environment=deployment_environment,
                account_id=account_id,
                exchange_account_id=account_id_uuid_or_none(account_id),
                cell=r.cell,
                week_start_ms=r.week_start_ms,
                week_end_ms=r.week_end_ms,
                n_fills=r.n_fills,
                gross_interest_usdt=r.gross_interest_usdt,
                net_interest_usdt=r.net_interest_usdt,
                capital_days=r.capital_days,
                realized_apr_net_pct=r.realized_apr_net_pct,
                baseline_close_apr_net_pct=r.baseline_close_apr_net_pct,
                baseline_frr_apr_net_pct=r.baseline_frr_apr_net_pct,
                baseline_frr_util_apr_net_pct=r.baseline_frr_util_apr_net_pct,
            ))
            await session.flush()
        await session.commit()
    return len(rows)


async def _amain(args: argparse.Namespace) -> int:
    settings = Settings()
    engine = make_engine(settings)
    sf = make_session_factory(engine)
    raw_account_id = os.environ.get("BFX_EXCHANGE_ACCOUNT_ID", "").strip()
    if not raw_account_id:
        raise RuntimeError("BFX_EXCHANGE_ACCOUNT_ID is required")
    account_id = account_id_canonical(raw_account_id)
    env = require_deployment_environment()
    try:
        result = await load_and_compute(
            sf, account_id=account_id, deployment_environment=env,
            reconcile_weeks=args.weeks,
        )
        if not result.has_credit_history:
            # The bot's CreditHistorySync has not filled the table yet: keep the
            # previous rows instead of replacing them with nothing.
            print("WARN attribution_weekly unchanged: funding_credit_history is empty "
                  "(CreditHistorySync not run yet)")
            return 0
        n = await persist_rows(sf, result.rows, account_id=account_id,
                               deployment_environment=env)
    finally:
        await engine.dispose()
    unattributed = sum(1 for r in result.rows if r.cell == "unattributed")
    print(f"attribution_weekly upserted={n} unattributed_rows={unattributed}")
    report = render_reconciliation(result)
    if args.out:
        Path(args.out).write_text(f"# Attribution vs ledger\n\n{report}")
    print(report, end="")
    flagged = [r for r in result.reconciliations if r.flagged]
    if flagged:
        log.warning("attribution_reconciliation_flagged weeks=%d", len(flagged))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--weeks", type=int, default=8,
                        help="complete weeks to reconcile against the ledger")
    parser.add_argument("--out", help="write the reconciliation as markdown here")
    sys.exit(asyncio.run(_amain(parser.parse_args())))
