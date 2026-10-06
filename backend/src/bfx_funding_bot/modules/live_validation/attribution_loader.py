"""E3 (b) — per-cell weekly fee-adjusted realized interest → attribution_weekly,
reconciled against the venue ledger.

Since 2026-09-27 the per-cell numbers come from venue credit records, not from
ORDER_FILL × held-to-term capped by CREDIT_CLOSED (whose rate/period/close were
parsed one slot late until then). Inputs, all read-only:
- funding_credit_history (ended credits/loans: rate, period, opening, actual
  close) + attribution_legacy_open_credits (what the legacy authority last saw open)
  and venue_credit_mirror (ledger) for the credits/loans still open: per-credit truth;
- funding_trades (credit → our offer id) + attribution_legacy_offer_links (offer →
  execution decision / signal correlation id, as the legacy offer_claims /
  venue_offer_state / ORDER_FILL recorded them; copied once by migration a0b1c2d3e4f5
  because those tables are frozen and leave public in S1-8) + execution_decisions /
  diagnostics DECISION (→ cell); after the authority switch new offers exist only in the
  ledger journal (acknowledged or bound-to-venue attempts, cell from the attempt), read
  through the ledger's attribution port and unioned with the legacy links (a disagreement
  is reported, never picked);
- funding_interest_payments (the ledger) for the weekly reconciliation;
- funding_candles / funding_stats for the baselines.
Matching, accrual and the reconciliation window: modules/live_validation/
credit_attribution.py. The only write is attribution_weekly (full recompute +
replace; a read model).

The CLI is scripts/run_weekly_attribution.py; the G3 report
(live_validation/g3.py) shares load_credit_inputs and the market loaders.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import case, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.auth_rest import LOAN_ID_PREFIX, InterestPayment
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_uuid_or_none,
    account_scope_clause,
)
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.attribution_reads import (
    JournalOfferCell,
    MirrorCredit,
    journal_offer_cells,
    mirror_credits,
)
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
    LEGACY_LINK_SOURCES,
    AttributionLegacyOfferLinkRow,
    AttributionLegacyOpenCreditRow,
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

_DECISION_KIND = "decision"
_MARKET_SYMBOL = "fUST"
_MARKET_PERIOD_AGG = "p2"
MARKET_TIMEFRAME = "1h"


async def load_market_points(
    session: AsyncSession, *, symbol: str, period_agg: str, start_ms: int, end_ms: int,
) -> list[MarketRatePoint]:
    """Per-day market funding rate (final funding_candles.close) of one
    symbol/period cell; candles are shared market data, not realm-scoped."""
    candles = await get_candles_in_range(
        session, symbol=symbol, timeframe=MARKET_TIMEFRAME, period_agg=period_agg,
        start_mts=start_ms, end_mts=end_ms,
    )
    return [MarketRatePoint(mts=c.mts, rate=c.close) for c in candles if c.close is not None]


async def load_funding_stat_rows(
    session: AsyncSession, *, symbol: str, start_ms: int, end_ms: int,
) -> list[FundingStatRow]:
    return list((await session.scalars(
        select(FundingStatRow).where(
            FundingStatRow.symbol == symbol,
            FundingStatRow.mts >= start_ms, FundingStatRow.mts <= end_ms,
        ).order_by(FundingStatRow.mts)
    )).all())


def funding_stats_of(rows: Iterable[FundingStatRow]) -> list[FundingStat]:
    return [
        FundingStat(
            symbol=r.symbol, mts=r.mts,
            frr=Decimal(str(r.frr)) if r.frr is not None else None,
            avg_period=Decimal(str(r.avg_period)) if r.avg_period is not None else None,
        )
        for r in rows
    ]


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
class OfferCellConflict:
    """One venue offer two sources place in different cells."""

    venue_offer_id: str
    legacy_cells: tuple[str, ...]
    journal_cells: tuple[str, ...]


def merge_offer_cells(
    legacy: dict[str, str], journal: Iterable[JournalOfferCell],
) -> tuple[dict[str, str], list[OfferCellConflict]]:
    """Union of the legacy and journal offer -> cell maps.

    An offer in one source takes that source's cell; an offer both sources know (a seeded
    attempt carries legacy provenance) must map to the same cell. A disagreement (also
    between two journal attempts of one offer) is returned as a conflict and the offer
    stays out of the map, so its credits are 'unattributed' rather than silently assigned.
    Several journal attempts naming one offer (ack and bound_to_venue of different attempts)
    agree when they share a cell and merge into it; the ledger's own provenance calls any
    second attempt a conflict, but the weekly only needs the cell, so agreement is not lost."""
    by_journal: dict[str, set[str]] = {}
    for link in journal:
        by_journal.setdefault(link.venue_offer_id, set()).add(link.cell_id)
    merged = dict(legacy)
    conflicts: list[OfferCellConflict] = []
    for offer in sorted(by_journal):
        cells = by_journal[offer]
        legacy_cell = legacy.get(offer)
        known = cells | ({legacy_cell} if legacy_cell is not None else set())
        if len(known) > 1:
            merged.pop(offer, None)
            conflicts.append(OfferCellConflict(
                offer, (legacy_cell,) if legacy_cell is not None else (), tuple(sorted(cells))))
        else:
            merged[offer] = next(iter(known))
    return merged, conflicts


@dataclass(frozen=True)
class AttributionResult:
    rows: list[WeeklyCellRow]
    reconciliations: list[WeeklyReconciliation]
    credits: int
    cells: CreditCells | None
    has_credit_history: bool
    offer_conflicts: tuple[OfferCellConflict, ...] = ()


def credit_from_history(r: FundingCreditHistoryRow) -> CreditLifetime:
    # An ended row without MTS_LAST_PAYOUT has not been seen; MTS_UPDATE is the
    # best remaining bound for its close.
    closed = r.mts_last_payout if r.mts_last_payout is not None else r.mts_update
    prefix = LOAN_ID_PREFIX if r.kind == "loan" else ""
    return CreditLifetime(
        credit_id=f"{prefix}{r.credit_id}", symbol=r.symbol, amount=Decimal(r.amount),
        rate=Decimal(r.rate), period_days=int(r.period_days), mts_create=int(r.mts_create),
        opened_ms=int(r.mts_opening), closed_ms=int(closed),
    )


async def legacy_offer_links(
    session: AsyncSession, account_uuid: UUID, deployment_environment: str,
) -> list[OfferLink]:
    """The legacy authority's offer links, in its source precedence (claims, venue offers,
    fills; ``LEGACY_LINK_SOURCES``) and then a fixed order within a source."""
    t = AttributionLegacyOfferLinkRow
    rank = case({source: i for i, source in enumerate(LEGACY_LINK_SOURCES)}, value=t.source)
    rows = await session.execute(select(
        t.venue_offer_id, t.execution_decision_id, t.signal_correlation_id,
    ).where(
        t.exchange_account_id == account_uuid,
        t.deployment_environment == deployment_environment,
    ).order_by(rank, t.venue_offer_id, t.execution_decision_id.nulls_first(),
               t.signal_correlation_id.nulls_first()))
    return [OfferLink(voi, edid, scid) for voi, edid, scid in rows.tuples()]


async def legacy_open_credits(
    session: AsyncSession, account_uuid: UUID, deployment_environment: str,
) -> list[CreditLifetime]:
    """The credits the legacy authority last saw open (complete terms only), as lifetimes
    opened at their creation (legacy rows have no venue opening)."""
    t = AttributionLegacyOpenCreditRow
    rows = (await session.scalars(select(t).where(
        t.exchange_account_id == account_uuid,
        t.deployment_environment == deployment_environment,
    ).order_by(t.credit_id))).all()
    return [CreditLifetime(
        credit_id=r.credit_id, symbol=r.symbol, amount=abs(Decimal(r.amount)),
        rate=Decimal(r.rate), period_days=int(r.period_days), mts_create=int(r.mts_created),
        opened_ms=int(r.mts_created), closed_ms=None,
    ) for r in rows]


def mirror_credit(m: MirrorCredit) -> CreditLifetime | None:
    """A ledger-mirror credit as a lifetime; None when its terms are not observed.

    Differs from a legacy open row on purpose: the opening is the venue's MTS_OPENING
    (``mts_opening``; legacy open rows only had ``mts_created``), the key trades are matched
    on. For a loan-derived credit (created after its opening) the group, the opening week and
    ``n_fills`` of a credit open across the switch therefore move to the originating trade's
    instant, which is what the same credit gets once it is in ``funding_credit_history``.
    An ended credit ends at ``mts_updated`` until the history sync lands its real close."""
    if m.rate is None or m.period_days is None:
        return None
    created = m.mts_created if m.mts_created is not None else m.mts_opening
    opened = m.mts_opening if m.mts_opening is not None else m.mts_created
    if created is None or opened is None:
        return None
    closed = None
    if m.terminal:
        closed = max(int(m.mts_updated if m.mts_updated is not None else opened), int(opened))
    prefix = LOAN_ID_PREFIX if m.source_kind == "loan" else ""
    return CreditLifetime(
        credit_id=f"{prefix}{m.venue_credit_id}", symbol=m.symbol, amount=abs(m.amount),
        rate=m.rate, period_days=m.period_days, mts_create=int(created),
        opened_ms=int(opened), closed_ms=closed,
    )


def reconciliation_weeks(now_ms: int, weeks: int) -> list[int]:
    """The last `weeks` calendar weeks whose payouts are all in, oldest first.

    Week W is paid by Tuesday ~01:30Z after it ends, so the Monday 04:17Z
    weekly run reconciles up to the week before last."""
    latest = calendar_week_start(now_ms - PAYOUT_LAG_MS - PAYOUT_SETTLE_MS) - WEEK_MS
    return [latest - i * WEEK_MS for i in reversed(range(weeks))]


@dataclass(frozen=True)
class CreditInputs:
    """The venue credit model's inputs for one account: every credit (ended
    and open), its cell, and the ledger payouts it reconciles against."""

    credits: list[CreditLifetime]
    cells: CreditCells
    payments: list[InterestPayment]
    has_credit_history: bool
    offer_conflicts: tuple[OfferCellConflict, ...] = ()


async def load_credit_inputs(
    session: AsyncSession, *, account_id: str, deployment_environment: str,
) -> CreditInputs | None:
    """Credits, credit -> cell and ledger payouts. None when the account has no
    ExchangeAccount UUID (the venue read models are keyed by it only).
    Shared by the weekly attribution and the G3 report."""
    account_uuid = account_id_uuid_or_none(account_id)
    if account_uuid is None:
        return None
    env = deployment_environment

    def scoped(table: Any) -> Any:
        return account_scope_clause(
            session, account_id=account_id,
            exchange_account_column=table.exchange_account_id,
            legacy_account_column=table.account_id,
        )

    history = (await session.scalars(select(FundingCreditHistoryRow).where(
        FundingCreditHistoryRow.exchange_account_id == account_uuid,
        FundingCreditHistoryRow.deployment_environment == env,
    ))).all()
    legacy_open = await legacy_open_credits(session, account_uuid, env)
    trade_rows = (await session.scalars(select(FundingTradeRow).where(
        FundingTradeRow.exchange_account_id == account_uuid,
        FundingTradeRow.deployment_environment == env,
    ))).all()
    links = await legacy_offer_links(session, account_uuid, env)
    decisions = (await session.execute(select(
        ExecutionDecisionRow.decision_id, ExecutionDecisionRow.signal_correlation_id,
        ExecutionDecisionRow.cell_id,
    ).where(
        scoped(ExecutionDecisionRow),
        ExecutionDecisionRow.deployment_environment == env,
    ))).all()
    diagnostics = (await session.scalars(select(DiagnosticsRow).where(
        DiagnosticsRow.kind == _DECISION_KIND, scoped(DiagnosticsRow),
        DiagnosticsRow.deployment_environment == env,
    ))).all()
    ledger_rows = (await session.scalars(select(FundingInterestPaymentRow).where(
        FundingInterestPaymentRow.exchange_account_id == account_uuid,
        FundingInterestPaymentRow.deployment_environment == env,
    ))).all()

    scope = Scope(account_uuid, env)
    journal_links = await journal_offer_cells(
        session, scope, {str(t.offer_id) for t in trade_rows})
    mirror = await mirror_credits(session, scope)

    credits = [credit_from_history(r) for r in history]
    seen = {c.credit_id for c in credits}
    # Credits not yet in the history: the ledger mirror is current after the switch, so it
    # wins over a legacy open row (which stops being maintained), and it also carries the
    # ones it knows ended until CreditHistorySync lands them. Before the switch it is empty.
    known_to_mirror: set[str] = set()
    for m in mirror:
        lifetime = mirror_credit(m)
        # An incomplete mirror row (no rate/period) must not hide a usable legacy open row;
        # a terminal one still does, so a stale legacy row never revives an ended credit.
        if lifetime is not None or m.terminal:
            known_to_mirror.add(f"{LOAN_ID_PREFIX if m.source_kind == 'loan' else ''}"
                                f"{m.venue_credit_id}")
        if lifetime is not None and lifetime.credit_id not in seen:
            credits.append(lifetime)
            seen.add(lifetime.credit_id)
    for still_open in legacy_open:
        if still_open.credit_id in known_to_mirror:
            continue
        if still_open.credit_id not in seen:
            credits.append(still_open)

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
    offer_cells, offer_conflicts = merge_offer_cells(
        resolve_offer_cells(
            links, cell_by_decision={did: cell for did, _scid, cell in decisions},
            cell_by_scid=cell_by_scid,
        ),
        journal_links,
    )
    for conflict in offer_conflicts:
        log.warning("attribution_offer_cell_conflict offer=%s legacy=%s journal=%s",
                    conflict.venue_offer_id, conflict.legacy_cells, conflict.journal_cells)
    payments = [InterestPayment(r.ledger_id, r.currency, None, r.mts, Decimal(r.amount),
                                Decimal(r.balance), r.description) for r in ledger_rows]
    return CreditInputs(credits, assign_cells(credits, trades, offer_cells), payments,
                        has_credit_history=bool(history),
                        offer_conflicts=tuple(offer_conflicts))


async def load_and_compute(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: str,
    deployment_environment: str,
    now_ms: int | None = None,
    reconcile_weeks: int = 8,
) -> AttributionResult:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    async with session_factory() as session:
        inputs = await load_credit_inputs(
            session, account_id=account_id, deployment_environment=deployment_environment,
        )
        if inputs is None:
            return AttributionResult([], [], 0, None, has_credit_history=False)
        credits = inputs.credits
        if not credits:
            return AttributionResult([], [], 0, None,
                                     has_credit_history=inputs.has_credit_history,
                                     offer_conflicts=inputs.offer_conflicts)

        start = min(c.opened_ms for c in credits)
        close_points = await load_market_points(
            session, symbol=_MARKET_SYMBOL, period_agg=_MARKET_PERIOD_AGG,
            start_ms=start, end_ms=now,
        )
        frr_rows = await load_funding_stat_rows(
            session, symbol=_MARKET_SYMBOL, start_ms=start, end_ms=now)

    cells = inputs.cells
    rows = compute_weekly_rows(
        totals_by_cell=weekly_totals(credits, cells, now_ms=now),
        close_points=close_points,
        frr_points=frr_points_from_stats(funding_stats_of(frr_rows)),
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
    reconciliations = [
        reconcile_week(credits, inputs.payments, currency=currency, week_start_ms=week,
                       now_ms=now)
        for currency in sorted({funding_currency(c.symbol) for c in credits})
        for week in reconciliation_weeks(now, reconcile_weeks)
    ]
    return AttributionResult(rows, reconciliations, len(credits), cells,
                             has_credit_history=inputs.has_credit_history,
                             offer_conflicts=inputs.offer_conflicts)


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
                      f"ambiguous cell split: {len(c.ambiguous)} (credits/loans sharing an "
                      "opening instant whose trades lead to different cells; allocated by "
                      f"amount, {len(c.shares)} split proportionally; no trade -> "
                      "'unattributed')"]
    if result.offer_conflicts:
        lines += ["", f"OFFER CELL CONFLICTS ({len(result.offer_conflicts)}): legacy records and "
                      "the ledger journal place the same venue offer in different cells; "
                      "its credits are 'unattributed' until resolved.", "",
                  "| venue offer | legacy cell | journal cell |", "|---|---|---|"]
        lines += [f"| {c.venue_offer_id} | {', '.join(c.legacy_cells) or '—'} "
                  f"| {', '.join(c.journal_cells)} |" for c in result.offer_conflicts]
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
