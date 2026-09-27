"""Per-cell interest from venue credits, reconciled against the ledger (pure).

Replaces the inference "interest = fill size x fill rate x held-to-term days,
capped by CREDIT_CLOSED": every number here comes from the venue's own credit
and trade records (see ``credit_history``), so a credit repaid after 14 minutes
earns 14 minutes, not 0 days (pre-fix CREDIT_CLOSED close time) or 2 days
(held-to-term).

Matching (credits and loans alike; both are lent money)
  credit -> trade: by (symbol, period, MTS_OPENING) against trades with
  MTS_CREATE == that opening. Rate, amount, id and MTS_CREATE are not keys:
  the venue stores the credit's rate at more precision than the trade's
  (0.0001239463 vs 0.00012395), and lent money first appears as a loan that
  turns into one or more credits with new ids, new MTS_CREATE, split amounts
  and sometimes another rate -- all keeping the trade's instant as
  MTS_OPENING (live account, 2026-08-09 .. 09-22). Interest still uses each
  record's own rate: that is what the venue pays.
  group -> cell: when every trade at the instant leads to one cell (the usual
  case), the whole group is that cell's. When they lead to different cells,
  the group is allocated by amount conservation: round-robin exact subset
  sums (each trade takes, per pass, the smallest set of remaining records
  whose amounts add up to its own; passes repeat because the same money
  appears once per loan/credit generation), and what is left is split
  proportionally over the trades large enough to have produced it. Such a
  group is marked ambiguous unless every record matched a trade on its own and
  equal-sized records are interchangeable (same rate and lifetime).
  trade -> cell: the trade's OFFER_ID is the venue id of our offer, which the
  loader resolves to the execution decision's cell. A group without a trade,
  or whose offer is not ours, goes to ``unattributed``.

Accrual
  held time = [max(MTS_OPENING, MTS_CREATE), MTS_LAST_PAYOUT) for an ended
  record and [start, now) for one still open, clipped to each calendar week,
  so a credit spanning a week boundary is split between the weeks rather than
  booked to the week it filled. A credit converted from a loan starts at its
  own MTS_CREATE (the loan earned until then), so the pair is not counted
  twice. gross = amount x rate x held days; net = gross x (1 - fee).

Reconciliation
  The ledger pays each UTC day's interest in one payout at ~01:30Z the next
  day, net of the fee. Credit week [Mon 00:00, next Mon 00:00) is therefore
  compared with the payouts stamped in [Tue 00:00, next Tue 00:00) - the window
  shifted by one day - and a week counts as complete only once that shifted
  window has closed plus a settle margin for the hourly ledger sync.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations

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
_MAX_SUBSET_PIECES = 16   # exact subset search is 2^n; a larger group goes proportional


@dataclass(frozen=True)
class CreditLifetime:
    credit_id: str           # venue id; loans carry LOAN_ID_PREFIX (separate sequence)
    symbol: str
    amount: Decimal
    rate: Decimal            # per day
    period_days: int
    mts_create: int          # when this record came into existence
    opened_ms: int           # venue MTS_OPENING: the originating trade's instant
    closed_ms: int | None    # venue MTS_LAST_PAYOUT; None while the credit is open

    @property
    def start_ms(self) -> int:
        """When this record began to earn: a credit converted from a loan keeps
        the loan's MTS_OPENING but exists only from its own MTS_CREATE."""
        return max(self.opened_ms, self.mts_create)

    def held_ms(self, lo: int, hi: int, *, now_ms: int) -> int:
        """Milliseconds this credit was lent inside [lo, hi)."""
        end = self.closed_ms if self.closed_ms is not None else now_ms
        return max(0, min(end, hi) - max(self.start_ms, lo))

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
    cell_by_credit: dict[str, str]   # the cell holding the largest share
    without_trade: frozenset[str]    # no funding trade at the credit's opening
    foreign_offer: frozenset[str]    # (part of) its trades' offers resolve to no cell
    ambiguous: frozenset[str]        # its group's split between cells is a choice
    # Fraction per cell (sums to 1) of each credit split between cells; any
    # other credit belongs wholly to ``cell_by_credit``.
    shares: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    # Fills (trades) per (cell, opening instant), for the weekly fill count; a
    # group without a trade is one fill of ``unattributed``.
    fills: dict[tuple[str, int], int] = field(default_factory=dict)

    def share_of(self, credit_id: str) -> Mapping[str, Decimal]:
        split = self.shares.get(credit_id)
        if split is not None:
            return split
        return {self.cell_by_credit.get(credit_id, UNATTRIBUTED): Decimal(1)}


NO_CELLS = CreditCells({}, frozenset(), frozenset(), frozenset())


def _id_order(credit_id: str) -> tuple[bool, int]:
    is_loan = credit_id.startswith(LOAN_ID_PREFIX)
    return is_loan, int(credit_id.removeprefix(LOAN_ID_PREFIX))


def _exact_subsets(
    trades: Sequence[TradeRecord], pieces: Sequence[CreditLifetime],
) -> tuple[dict[str, int], list[CreditLifetime]]:
    """Round-robin exact-sum matching of records to trades.

    Each pass gives every trade (ascending id) one subset of the remaining
    records whose amounts sum exactly to the trade's -- smallest subset first,
    then lowest ids. Passes repeat, because one trade's money appears once per
    generation (a loan, then the credits it turned into). Returns record id ->
    trade index, and the records left over.
    """
    remaining = list(pieces)
    owner: dict[str, int] = {}
    if len(remaining) > _MAX_SUBSET_PIECES:
        return owner, remaining
    progressed = True
    while progressed and remaining:
        progressed = False
        for i, trade in enumerate(trades):
            found = next((combo for size in range(1, len(remaining) + 1)
                          for combo in combinations(remaining, size)
                          if sum((p.amount for p in combo), Decimal(0)) == trade.amount), None)
            if found is None:
                continue
            for p in found:
                owner[p.credit_id] = i
                remaining.remove(p)
            progressed = True
    return owner, remaining


def assign_cells(
    credits: Iterable[CreditLifetime],
    trades: Iterable[TradeRecord],
    offer_cells: Mapping[str, str],
) -> CreditCells:
    """Credits and loans -> cells, one (symbol, period, MTS_OPENING) group at a time."""
    groups: dict[tuple[str, int, int], list[CreditLifetime]] = {}
    for c in credits:
        groups.setdefault((c.symbol, c.period_days, c.opened_ms), []).append(c)
    trades_at: dict[tuple[str, int, int], list[TradeRecord]] = {}
    for t in trades:
        trades_at.setdefault((t.symbol, t.period_days, t.mts_create), []).append(t)

    cell_by_credit: dict[str, str] = {}
    shares: dict[str, dict[str, Decimal]] = {}
    fills: dict[tuple[str, int], int] = {}
    without_trade: set[str] = set()
    foreign: set[str] = set()
    ambiguous: set[str] = set()
    for key, group in groups.items():
        group.sort(key=lambda c: _id_order(c.credit_id))
        ids = [c.credit_id for c in group]
        opening = key[2]
        matched = sorted(trades_at.get(key, []), key=lambda t: t.trade_id)
        if not matched:
            without_trade.update(ids)
            cell_by_credit.update(dict.fromkeys(ids, UNATTRIBUTED))
            fills[(UNATTRIBUTED, opening)] = fills.get((UNATTRIBUTED, opening), 0) + 1
            continue
        cells = [offer_cells.get(t.offer_id) for t in matched]
        outcome = [cell or UNATTRIBUTED for cell in cells]
        for cell in outcome:
            fills[(cell, opening)] = fills.get((cell, opening), 0) + 1
        if len(set(outcome)) == 1:
            # Every trade at this instant leads to the same place: the group is
            # that cell's money, whatever loan/credit shape it has taken since.
            cell_by_credit.update(dict.fromkeys(ids, outcome[0]))
            if cells[0] is None:
                foreign.update(ids)
            continue
        owner, leftovers = _exact_subsets(matched, group)
        for c in group:
            if c.credit_id in owner:
                via = [owner[c.credit_id]]
                split = {outcome[via[0]]: Decimal(1)}
            else:
                # Proportional over the trades large enough to have produced it.
                via = [i for i, t in enumerate(matched) if t.amount >= c.amount] or list(
                    range(len(matched)))
                total = sum((matched[i].amount for i in via), Decimal(0))
                split = {}
                for i in via:
                    split[outcome[i]] = split.get(outcome[i], Decimal(0)) + matched[i].amount / total
            cell_by_credit[c.credit_id] = min(split, key=lambda cell: (-split[cell], cell))
            if len(split) > 1:
                shares[c.credit_id] = split
            if any(cells[i] is None for i in via):
                foreign.add(c.credit_id)
        # Unambiguous only when each trade took at most one record, on its own,
        # and records of equal amount are interchangeable (same rate and life).
        per_trade = [sum(1 for i in owner.values() if i == n) for n in range(len(matched))]
        alike: dict[Decimal, set[tuple[object, ...]]] = {}
        for c in group:
            alike.setdefault(c.amount, set()).add((c.rate, c.start_ms, c.closed_ms))
        if leftovers or max(per_trade) > 1 or any(len(v) > 1 for v in alike.values()):
            ambiguous.update(ids)
    return CreditCells(cell_by_credit, frozenset(without_trade), frozenset(foreign),
                       frozenset(ambiguous), shares, fills)


def weekly_totals(
    credits: Iterable[CreditLifetime],
    cells: CreditCells,
    *,
    now_ms: int,
) -> dict[str, dict[int, CellWeekTotals]]:
    """Per cell, per calendar week: fills opened, capital-days and gross interest.

    A fill is counted once in the week of its opening however many loans and
    credits it became; a credit split between cells adds its share to each."""
    acc: dict[str, dict[int, list[Decimal]]] = {}
    counted: set[tuple[str, int]] = set()
    for c in credits:
        opened_wk = calendar_week_start(c.opened_ms)
        end = c.closed_ms if c.closed_ms is not None else now_ms
        for cell, share in cells.share_of(c.credit_id).items():
            weeks = acc.setdefault(cell, {})
            slot = weeks.setdefault(opened_wk, [Decimal(0)] * 3)
            if (cell, c.opened_ms) not in counted:
                counted.add((cell, c.opened_ms))
                slot[0] += cells.fills.get((cell, c.opened_ms), 1)
            wk = opened_wk
            while wk < end:
                held = c.held_ms(wk, wk + WEEK_MS, now_ms=now_ms)
                if held > 0:
                    slot = weeks.setdefault(wk, [Decimal(0)] * 3)
                    capital = c.amount * share * Decimal(held) / _MS_PER_DAY
                    slot[1] += capital
                    slot[2] += capital * c.rate
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
