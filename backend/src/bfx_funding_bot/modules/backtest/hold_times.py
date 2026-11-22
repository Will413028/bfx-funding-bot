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

Summaries are per (kind, term): a 7-day credit is never pooled with a 2-day
one. Two more measures compare the terms, on venue credits only:
`repaid_after_drop` (did the borrower leave within a day of the market
falling below the rule's trigger) and `rate_gap` (a long credit's rate
against the 2-day credits lent over the same hours).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

import numpy as np

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.engine import held_ms_until_refinance
from bfx_funding_bot.modules.backtest.incomplete import BacktestIncomplete

_MS_PER_HOUR = 3_600_000
# A close within an hour of the term end is the term running out, not a repayment.
EARLY_TOLERANCE_MS = _MS_PER_HOUR
# The borrower's view of the market at the close: the last hourly close, if recent.
_MAX_REFERENCE_AGE_MS = 3 * _MS_PER_HOUR
# The share of a credit's term the hourly series must cover for the rule to be judged on it.
MIN_TERM_COVERAGE = Decimal("0.9")
# A repayment this soon after the market fell under the trigger is read as following it.
FOLLOW_WINDOW_MS = 24 * _MS_PER_HOUR
# Ended long credits below which the rate gap is not reported.
MIN_LONG_SAMPLE = 20
BASE_PERIOD_DAYS = 2


@dataclass(frozen=True)
class Bootstrap:
    resamples: int = 10_000
    seed: int = 20261122


DEFAULT_BOOTSTRAP = Bootstrap()


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
    # Time from the opening to the first close under the rule's trigger that came before
    # the credit actually ended; None when there was none or the series cannot show it.
    drop_ms: int | None = None


@dataclass(frozen=True)
class HoldSummary:
    n: int
    n_early: int
    median_held_hours: Decimal | None
    median_held_share: Decimal | None  # median of each credit's held time over its term
    held_fraction: Decimal | None  # amount x term weighted, over every credit
    n_compared: int  # credits the refinance rule could be run on
    compared_held_fraction: Decimal | None
    predicted_fraction: Decimal | None
    position: Decimal | None
    early_with_market: int  # early credits with a recent market print at the close
    early_with_drop: int  # of those, the market was more than the margin below the rate


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
        predicted = _rule_prediction(series, credit, margin, term_ms)
        rows.append(HoldRow(
            credit=credit, term_ms=term_ms, held_ms=held_ms,
            early=held_ms < term_ms - EARLY_TOLERANCE_MS,
            drop_at_close=_drop_at(series, credit.closed_mts, credit.rate),
            predicted_ms=predicted,
            drop_ms=_drop_before_close(series, credit, margin, held_ms),
        ))
    return rows


def _rule_prediction(
    series: tuple[list[int], list[Decimal | None]], credit: EndedCredit, margin: Decimal,
    term_ms: int,
) -> int | None:
    """The refinance rule's hold for this credit, or None when it cannot be run.

    It cannot when the rate is not a real fixed rate (0 marks an FRR credit: no
    close can be under its trigger), or when the candles do not cover the term:
    the series ends early, or has a hole. A missing hour could have been the one
    that fires the rule, so a credit with less than `MIN_TERM_COVERAGE` of its
    term on the series is left out whether or not the rule fired on the rest.
    """
    return _rule_hold(series, credit, margin, term_ms)


def _rule_hold(
    series: tuple[list[int], list[Decimal | None]], credit: EndedCredit, margin: Decimal,
    span_ms: int,
) -> int | None:
    """The rule run over the first `span_ms` of the credit; `span_ms` when it did not fire."""
    if credit.rate <= 0 or span_ms <= 0:
        return None
    mts = series[0]
    end_mts = credit.opened_mts + span_ms
    # Candles whose close falls inside the span, the hour the credit opened in included.
    covered = bisect_left(mts, end_mts - _MS_PER_HOUR + 1) - bisect_right(
        mts, credit.opened_mts - _MS_PER_HOUR)
    if covered * _MS_PER_HOUR < MIN_TERM_COVERAGE * span_ms:
        return None
    try:
        return held_ms_until_refinance(
            series, lock_start_mts=credit.opened_mts, locked_rate=credit.rate,
            margin=margin, term_ms=span_ms)
    except BacktestIncomplete:
        return None


def _drop_before_close(
    series: tuple[list[int], list[Decimal | None]], credit: EndedCredit, margin: Decimal,
    held_ms: int,
) -> int | None:
    """When the market first closed under the trigger while the credit was still out.

    Judged on the hours the credit was actually held, with the same coverage share:
    a long credit repaid on its first day needs candles to that day, not to a term
    end that may lie after the report runs. Requiring the whole term would drop
    exactly the early repayments and keep the credits that ran to term.
    """
    fired = _rule_hold(series, credit, margin, held_ms)
    return fired if fired is not None and fired < held_ms else None


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


def loans_that_became_credits(credits: Sequence[EndedCredit], *, within_ms: int = 60_000) -> int:
    """Loans that opened together with a credit of the same size, rate and term.

    A heuristic for one lend the venue listed first as a loan (taken, unused) and
    then as a credit (in use): such a loan's end is a conversion, not a repayment.
    The credit keeps the lend's opening time, so the two are matched on when they
    opened; the loan row ends at the conversion, no later than the credit does.
    """
    opened_by_shape: dict[tuple[Decimal, Decimal, int], list[tuple[int, int]]] = {}
    for credit in credits:
        if credit.kind == "credit":
            key = (credit.amount, credit.rate, credit.period_days)
            opened_by_shape.setdefault(key, []).append((credit.opened_mts, credit.closed_mts))
    converted = 0
    for loan in credits:
        if loan.kind != "loan":
            continue
        openings = opened_by_shape.get((loan.amount, loan.rate, loan.period_days), [])
        # The loan listing comes first: it cannot outlast the credit it turned into.
        match = next((pair for pair in openings
                      if abs(pair[0] - loan.opened_mts) <= within_ms
                      and loan.closed_mts <= pair[1]), None)
        if match is not None:
            openings.remove(match)  # one credit explains one loan
            converted += 1
    return converted


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
        median_held_share=(median(Decimal(row.held_ms) / row.term_ms for row in rows)
                           if rows else None),
        held_fraction=_weighted_fraction(rows, [row.held_ms for row in rows]),
        n_compared=len(compared),
        compared_held_fraction=compared_held,
        predicted_fraction=predicted,
        position=position,
        early_with_market=len(early_market),
        early_with_drop=sum(1 for row in early_market
                            if row.drop_at_close is not None and row.drop_at_close > margin),
    )


def group_holds(rows: Sequence[HoldRow]) -> dict[tuple[str, int], list[HoldRow]]:
    """Rows by (kind, term in days), in a stable order: terms are never pooled."""
    groups: dict[tuple[str, int], list[HoldRow]] = {}
    for row in sorted(rows, key=lambda r: (r.credit.kind, r.credit.period_days)):
        groups.setdefault((row.credit.kind, row.credit.period_days), []).append(row)
    return groups


@dataclass(frozen=True)
class DropFollow:
    n_dropped: int  # credits the market fell under the trigger for, before they ended
    n_repaid_after: int  # of those, repaid early within `FOLLOW_WINDOW_MS` of that close
    share: Decimal | None  # None when no credit saw a drop


def repaid_after_drop(rows: Sequence[HoldRow]) -> DropFollow:
    """Of the venue credits that saw the market fall under the trigger, how many then left.

    The fall is the refinance rule's own: the first hourly 2-day close under
    `rate * (1 - margin)`, on the series `measure_holds` used (`HoldRow.drop_ms`).
    A credit counts only if that close came before it actually ended; one that
    then ran to its term is in the denominator, never the numerator.
    """
    dropped = repaid = 0
    for row in rows:
        if row.credit.kind != "credit" or row.drop_ms is None:
            continue
        dropped += 1
        if row.early and row.held_ms - row.drop_ms <= FOLLOW_WINDOW_MS:
            repaid += 1
    return DropFollow(dropped, repaid, Decimal(repaid) / dropped if dropped else None)


@dataclass(frozen=True)
class RateGap:
    period_days: int
    n_long: int  # ended venue credits of the long term with a real fixed rate
    n_included: int  # of those, lent alongside at least one 2-day credit
    n_excluded: int  # no 2-day credit overlapped their life
    min_sample: int
    # Long rate minus the 2-day rate over the same hours, per unit per day,
    # weighted by amount x time held. None under the sample threshold or with none included.
    gap: Decimal | None
    ci_low: Decimal | None
    ci_high: Decimal | None
    # False when the newest long credit ended too recently for the 2-day credits lent
    # alongside it to have ended and be in the data: the gap is then not reported.
    settled: bool = True


def _base_rate_over(base: Sequence[EndedCredit], start_mts: int, end_mts: int) -> Decimal | None:
    """The amount x overlap weighted rate of the 2-day credits lent during [start, end)."""
    weight = total = Decimal(0)
    for credit in base:
        overlap = min(credit.closed_mts, end_mts) - max(credit.opened_mts, start_mts)
        if overlap > 0:
            weight += credit.amount * overlap
            total += credit.amount * overlap * credit.rate
    return total / weight if weight else None


def _bootstrap_interval(
    gaps: Sequence[Decimal], weights: Sequence[Decimal], bootstrap: Bootstrap,
) -> tuple[Decimal, Decimal]:
    """The 2.5 and 97.5 percentiles of the weighted mean over resampled credits."""
    gap = np.array([float(g) for g in gaps])
    weight = np.array([float(w) for w in weights])
    resamples = bootstrap.resamples
    picks = np.random.default_rng(bootstrap.seed).integers(
        0, len(gap), size=(resamples, len(gap)))
    means = np.sort((gap[picks] * weight[picks]).sum(axis=1) / weight[picks].sum(axis=1))
    # The same number of resampled means is left outside on each side.
    cut = int(resamples * 0.025)
    low = means[max(0, cut - 1)]
    high = means[min(resamples - 1, resamples - cut)]
    return Decimal(repr(float(low))), Decimal(repr(float(high)))


def _long_gaps(
    long_rows: Sequence[HoldRow], base: Sequence[EndedCredit],
) -> tuple[list[Decimal], list[Decimal]]:
    """Each included long credit's rate over its 2-day comparison, and its weight."""
    gaps: list[Decimal] = []
    weights: list[Decimal] = []
    for row in long_rows:
        base_rate = _base_rate_over(base, row.credit.opened_mts, row.credit.closed_mts)
        if base_rate is not None and row.held_ms > 0:
            gaps.append(row.credit.rate - base_rate)
            weights.append(row.credit.amount * row.held_ms)
    return gaps, weights


def rate_gap(
    rows: Sequence[HoldRow], *, period_days: int, as_of_mts: int | None = None,
    min_sample: int = MIN_LONG_SAMPLE, bootstrap: Bootstrap = DEFAULT_BOOTSTRAP,
) -> RateGap:
    """What a long credit earned over the 2-day credits lent during the same hours.

    A rate difference only: it leaves out the hours 2-day money sits idle between
    two credits, so it is slightly unfavourable to the long term. For each ended
    long venue credit, the comparison is the 2-day venue credits with a real fixed
    rate (above 0: 0 marks FRR), each counted for the part of its life inside the
    long credit's own, weighted by amount x overlap. A long credit nothing
    overlaps is left out and counted. The sample threshold is on ended long
    credits, included or not. Long credits without a real fixed rate are not long
    credits here at all. Only ended credits are in the data, so with `as_of_mts`
    (the time of the run) the gap is withheld until the newest long credit has
    been over for a full 2-day term: before that the 2-day credits lent during
    its last hours may still be out and missing from the comparison.
    """
    if period_days <= BASE_PERIOD_DAYS:
        raise ValueError(f"the long term must be over {BASE_PERIOD_DAYS} days, got {period_days}")
    credits = [row for row in rows if row.credit.kind == "credit"]
    base = [row.credit for row in credits
            if row.credit.period_days == BASE_PERIOD_DAYS and row.credit.rate > 0]
    long_rows = [row for row in credits
                 if row.credit.period_days == period_days and row.credit.rate > 0]
    settle_ms = BASE_PERIOD_DAYS * 24 * _MS_PER_HOUR
    settled = as_of_mts is None or all(
        as_of_mts - row.credit.closed_mts >= settle_ms for row in long_rows)
    gaps, weights = _long_gaps(long_rows, base)
    gap = low = high = None
    if settled and len(long_rows) >= min_sample and gaps and sum(weights) > 0:
        gap = sum((g * w for g, w in zip(gaps, weights, strict=True)), Decimal(0)) / sum(weights)
        low, high = _bootstrap_interval(gaps, weights, bootstrap)
    return RateGap(
        period_days=period_days, n_long=len(long_rows), n_included=len(gaps),
        n_excluded=len(long_rows) - len(gaps), min_sample=min_sample,
        gap=gap, ci_low=low, ci_high=high, settled=settled,
    )
