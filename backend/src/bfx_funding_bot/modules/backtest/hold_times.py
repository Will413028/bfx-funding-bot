"""How long borrowers actually held our funding credits (pure, no DB).

The replay scores every arm under two repayment scenarios: locks held to
maturity, and locks repaid once the 2-day market closes a margin below the
locked rate (`engine.held_ms_until_refinance`, ADR 2026-09-27 D3). Which one
is closer to reality was unmeasured. This module measures it from our own
ended credits: each credit's actual life against its term, the market the
borrower saw when it closed, and what the refinance rule would have predicted
for the same credit.

`position` puts the measured held fraction on the line between the two
scenarios: 0 = the refinance scenario, 1 = held to maturity.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.engine import held_ms_until_refinance
from bfx_funding_bot.modules.backtest.incomplete import BacktestIncomplete

_MS_PER_HOUR = 3_600_000
# A close within an hour of the term end is the term running out, not a repayment.
EARLY_TOLERANCE_MS = _MS_PER_HOUR
# The borrower's view of the market at the close: the last hourly close, if recent.
_MAX_REFERENCE_AGE_MS = 3 * _MS_PER_HOUR


@dataclass(frozen=True)
class EndedCredit:
    credit_id: int
    kind: str  # "credit" (in use) or "loan" (taken, unused), as the venue reports it
    symbol: str
    rate: Decimal
    period_days: int
    amount: Decimal
    opened_mts: int
    closed_mts: int


@dataclass(frozen=True)
class HoldRow:
    credit: EndedCredit
    term_ms: int
    held_ms: int  # capped at the term
    early: bool
    # 1 - (last 2-day close before the credit closed) / locked rate; None without a recent print.
    drop_at_close: Decimal | None
    # What the refinance rule gives for this credit; None when the reference does not cover it.
    predicted_ms: int | None


@dataclass(frozen=True)
class HoldSummary:
    n: int
    n_early: int
    median_held_hours: Decimal | None
    held_fraction: Decimal | None  # amount x term weighted, over every credit
    n_compared: int  # credits the refinance rule could be run on
    compared_held_fraction: Decimal | None
    predicted_fraction: Decimal | None
    position: Decimal | None
    early_with_market: int  # early credits with a recent market print at the close
    early_with_drop: int  # of those, the market was at least the margin below the rate


def measure_holds(
    credits: Sequence[EndedCredit], reference: Sequence[FundingCandle], margin: Decimal
) -> list[HoldRow]:
    """One row per ended credit; `reference` is the hourly 2-day market series."""
    ordered = sorted(reference, key=lambda c: c.mts)
    series = ([c.mts for c in ordered], [c.close for c in ordered])
    rows = []
    for credit in credits:
        if credit.closed_mts < credit.opened_mts:
            raise ValueError(f"credit {credit.credit_id} closes before it opens")
        term_ms = credit.period_days * 24 * _MS_PER_HOUR
        held_ms = min(credit.closed_mts - credit.opened_mts, term_ms)
        try:
            predicted: int | None = held_ms_until_refinance(
                series, lock_start_mts=credit.opened_mts, locked_rate=credit.rate,
                margin=margin, term_ms=term_ms)
        except BacktestIncomplete:
            predicted = None
        rows.append(HoldRow(
            credit=credit, term_ms=term_ms, held_ms=held_ms,
            early=held_ms < term_ms - EARLY_TOLERANCE_MS,
            drop_at_close=_drop_at(series, credit.closed_mts, credit.rate),
            predicted_ms=predicted,
        ))
    return rows


def _drop_at(
    series: tuple[list[int], list[Decimal | None]], at_mts: int, rate: Decimal
) -> Decimal | None:
    mts, closes = series
    # The last candle whose hour had ended by `at_mts`.
    index = bisect_right(mts, at_mts - _MS_PER_HOUR) - 1
    if index < 0 or at_mts - (mts[index] + _MS_PER_HOUR) > _MAX_REFERENCE_AGE_MS:
        return None
    close = closes[index]
    if close is None or close <= 0 or rate <= 0:
        return None
    return 1 - close / rate


def _weighted_fraction(rows: Sequence[HoldRow], held: Sequence[int]) -> Decimal | None:
    weight = sum((row.credit.amount * row.term_ms for row in rows), Decimal(0))
    if weight == 0:
        return None
    return sum((row.credit.amount * ms for row, ms in zip(rows, held, strict=True)),
               Decimal(0)) / weight


def summarize_holds(rows: Sequence[HoldRow], margin: Decimal) -> HoldSummary:
    compared = [row for row in rows if row.predicted_ms is not None]
    compared_held = _weighted_fraction(compared, [row.held_ms for row in compared])
    predicted = _weighted_fraction(compared, [row.predicted_ms or 0 for row in compared])
    position = None
    if compared_held is not None and predicted is not None and predicted < 1:
        position = (compared_held - predicted) / (1 - predicted)
    early_market = [row for row in rows if row.early and row.drop_at_close is not None]
    return HoldSummary(
        n=len(rows),
        n_early=sum(row.early for row in rows),
        median_held_hours=(Decimal(median(row.held_ms for row in rows)) / _MS_PER_HOUR
                           if rows else None),
        held_fraction=_weighted_fraction(rows, [row.held_ms for row in rows]),
        n_compared=len(compared),
        compared_held_fraction=compared_held,
        predicted_fraction=predicted,
        position=position,
        early_with_market=len(early_market),
        early_with_drop=sum(1 for row in early_market
                            if row.drop_at_close is not None and row.drop_at_close >= margin),
    )
