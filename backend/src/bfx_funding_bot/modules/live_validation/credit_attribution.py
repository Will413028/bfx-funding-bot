"""Per-cell interest from venue credits, reconciled against the ledger (pure).

Replaces the inference "interest = fill size x fill rate x held-to-term days,
capped by CREDIT_CLOSED": every number here comes from the venue's own credit
and trade records (see ``credit_history``), so a credit repaid after 14 minutes
earns 14 minutes, not 0 days (pre-fix CREDIT_CLOSED close time) or 2 days
(held-to-term).

Matching
  credit -> trade: same symbol, amount, rate, period and MTS_CREATE (the venue
  stamps a credit with its trade's creation time). Several credits sharing one
  key are indistinguishable; they are paired with the key's trades by ascending
  venue id. That pairing is arbitrary but harmless unless the trades lead to
  different cells *and* the credits are not interchangeable (held for different
  times, or unequal counts on the two sides); only then are the key's credits
  marked ambiguous (still attributed, and counted in the report).
  trade -> cell: the trade's OFFER_ID is the venue id of our offer, which the
  loader resolves to the execution decision's cell. A credit without a trade,
  or whose offer is not ours, goes to ``unattributed``.

Accrual
  held time = [MTS_OPENING, MTS_LAST_PAYOUT) for an ended credit and
  [opening, now) for one still open, clipped to each calendar week, so a credit
  spanning a week boundary is split between the weeks rather than booked to the
  week it filled. gross = amount x rate x held days; net = gross x (1 - fee).

Reconciliation
  The ledger pays each UTC day's interest in one payout at ~01:30Z the next
  day, net of the fee. Credit week [Mon 00:00, next Mon 00:00) is therefore
  compared with the payouts stamped in [Tue 00:00, next Tue 00:00) - the window
  shifted by one day - and a week counts as complete only once that shifted
  window has closed plus a settle margin for the hourly ledger sync.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.external.bitfinex.auth_rest import LOAN_ID_PREFIX, InterestPayment
from bfx_funding_bot.modules.live_validation.interest_ledger import MS_PER_DAY, funding_currency
from bfx_funding_bot.modules.live_validation.weekly_attribution import (
    FEE_RATE,
    WEEK_MS,
    CellWeekTotals,
    calendar_week_start,
)

UNATTRIBUTED = "unattributed"
PAYOUT_LAG_MS = MS_PER_DAY            # a day's interest is paid early the next day
PAYOUT_SETTLE_MS = 6 * 60 * 60 * 1000  # payout ~01:30Z + hourly sync, with margin
RECONCILE_REL_TOL = Decimal("0.05")
RECONCILE_ABS_TOL = Decimal("0.01")
_ONE_MINUS_FEE = Decimal("1") - FEE_RATE
_MS_PER_DAY = Decimal(MS_PER_DAY)


@dataclass(frozen=True)
class CreditLifetime:
    credit_id: str           # venue id; loans carry LOAN_ID_PREFIX (separate sequence)
    symbol: str
    amount: Decimal
    rate: Decimal            # per day
    period_days: int
    mts_create: int
    opened_ms: int           # venue MTS_OPENING
    closed_ms: int | None    # venue MTS_LAST_PAYOUT; None while the credit is open

    def held_ms(self, lo: int, hi: int, *, now_ms: int) -> int:
        """Milliseconds this credit was lent inside [lo, hi)."""
        end = self.closed_ms if self.closed_ms is not None else now_ms
        return max(0, min(end, hi) - max(self.opened_ms, lo))

    def gross_interest(self, lo: int, hi: int, *, now_ms: int) -> Decimal:
        return self.amount * self.rate * Decimal(self.held_ms(lo, hi, now_ms=now_ms)) / _MS_PER_DAY


@dataclass(frozen=True)
class TradeRecord:
    trade_id: int
    symbol: str
    mts_create: int
    offer_id: str
    amount: Decimal
    rate: Decimal
    period_days: int


@dataclass(frozen=True)
class CreditCells:
    cell_by_credit: dict[str, str]
    without_trade: frozenset[str]    # no funding trade shares the credit's key
    foreign_offer: frozenset[str]    # trade found, offer not resolvable to a cell
    ambiguous: frozenset[str]        # pairing within a shared key could change the cell


def _id_order(credit_id: str) -> tuple[bool, int]:
    is_loan = credit_id.startswith(LOAN_ID_PREFIX)
    return is_loan, int(credit_id.removeprefix(LOAN_ID_PREFIX))


def _key(symbol: str, amount: Decimal, rate: Decimal, period: int, mts: int) -> tuple[object, ...]:
    return (symbol, amount, rate, period, mts)


def assign_cells(
    credits: Iterable[CreditLifetime],
    trades: Iterable[TradeRecord],
    offer_cells: Mapping[str, str],
) -> CreditCells:
    credits_by_key: dict[tuple[object, ...], list[CreditLifetime]] = {}
    for c in credits:
        credits_by_key.setdefault(
            _key(c.symbol, c.amount, c.rate, c.period_days, c.mts_create), []).append(c)
    trades_by_key: dict[tuple[object, ...], list[TradeRecord]] = {}
    for t in trades:
        trades_by_key.setdefault(
            _key(t.symbol, t.amount, t.rate, t.period_days, t.mts_create), []).append(t)

    cell_by_credit: dict[str, str] = {}
    without_trade: set[str] = set()
    foreign: set[str] = set()
    ambiguous: set[str] = set()
    for key, group in credits_by_key.items():
        group.sort(key=lambda c: _id_order(c.credit_id))
        matched = sorted(trades_by_key.get(key, []), key=lambda t: t.trade_id)
        cells = [offer_cells.get(t.offer_id) for t in matched]
        for i, credit in enumerate(group):
            if i >= len(matched):
                without_trade.add(credit.credit_id)
                cell_by_credit[credit.credit_id] = UNATTRIBUTED
                continue
            cell = cells[i]
            if cell is None:
                foreign.add(credit.credit_id)
            cell_by_credit[credit.credit_id] = cell or UNATTRIBUTED
        # Swapping partners matters only if the trades lead to different cells
        # and the credits are not interchangeable (held differently, or a
        # partner is missing on one side).
        if len(matched) > 1 and len({c or UNATTRIBUTED for c in cells}) > 1 and (
                len(group) != len(matched)
                or len({(c.opened_ms, c.closed_ms) for c in group}) > 1):
            ambiguous.update(c.credit_id for c in group)
    return CreditCells(cell_by_credit, frozenset(without_trade), frozenset(foreign),
                       frozenset(ambiguous))


def weekly_totals(
    credits: Iterable[CreditLifetime],
    cell_by_credit: Mapping[str, str],
    *,
    now_ms: int,
) -> dict[str, dict[int, CellWeekTotals]]:
    """Per cell, per calendar week: credits opened, capital-days and gross interest."""
    acc: dict[str, dict[int, list[Decimal]]] = {}
    for c in credits:
        weeks = acc.setdefault(cell_by_credit.get(c.credit_id, UNATTRIBUTED), {})
        weeks.setdefault(calendar_week_start(c.opened_ms), [Decimal(0)] * 3)[0] += 1
        end = c.closed_ms if c.closed_ms is not None else now_ms
        wk = calendar_week_start(c.opened_ms)
        while wk < end:
            held = c.held_ms(wk, wk + WEEK_MS, now_ms=now_ms)
            if held > 0:
                slot = weeks.setdefault(wk, [Decimal(0)] * 3)
                days = Decimal(held) / _MS_PER_DAY
                slot[1] += c.amount * days
                slot[2] += c.amount * c.rate * days
            wk += WEEK_MS
    return {
        cell: {wk: CellWeekTotals(int(v[0]), v[1], v[2]) for wk, v in weeks.items()}
        for cell, weeks in acc.items()
    }


@dataclass(frozen=True)
class WeeklyReconciliation:
    currency: str
    week_start_ms: int
    credit_gross: Decimal
    credit_net: Decimal              # credit_gross x (1 - fee)
    ledger_net: Decimal              # payouts in the one-day-shifted window
    payouts: int
    diff: Decimal                    # credit_net - ledger_net
    diff_pct: Decimal | None         # diff / ledger_net x 100; None without payouts
    complete: bool                   # the shifted payout window has closed and settled
    flagged: bool                    # complete and |diff| > max(5 % of ledger, 0.01)


def reconcile_week(
    credits: Sequence[CreditLifetime],
    payments: Sequence[InterestPayment],
    *,
    currency: str,
    week_start_ms: int,
    now_ms: int,
) -> WeeklyReconciliation:
    week_end = week_start_ms + WEEK_MS
    gross = sum((c.gross_interest(week_start_ms, week_end, now_ms=now_ms) for c in credits
                 if funding_currency(c.symbol) == currency), Decimal("0"))
    net = gross * _ONE_MINUS_FEE
    lo, hi = week_start_ms + PAYOUT_LAG_MS, week_end + PAYOUT_LAG_MS
    paid = [p for p in payments if p.currency == currency and lo <= p.mts < hi]
    ledger = sum((p.amount for p in paid), Decimal("0"))
    diff = net - ledger
    complete = now_ms >= hi + PAYOUT_SETTLE_MS
    tolerance = max(RECONCILE_REL_TOL * abs(ledger), RECONCILE_ABS_TOL)
    return WeeklyReconciliation(
        currency=currency, week_start_ms=week_start_ms, credit_gross=gross, credit_net=net,
        ledger_net=ledger, payouts=len(paid), diff=diff,
        diff_pct=diff / ledger * 100 if ledger != 0 else None,
        complete=complete, flagged=complete and abs(diff) > tolerance,
    )
