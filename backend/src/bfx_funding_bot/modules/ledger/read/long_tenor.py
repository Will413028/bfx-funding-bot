"""Long-tenor exposure: how much of one tenor this bot has placed, has out, and has seen end.

The deployment layer limits offers of a tenor longer than the strategy's with these
numbers (plan `docs/plans/2026-11-22-seven-day-tenor-measurement.md`, step 1). They are
counted from two sources of different reliability:

- **Our own submissions** (an attempt the venue acknowledged or that was bound to a
  venue offer). Written when the order is sent, so an offer that fills before any
  observation ever sees it is still there. The filled-offer count starts here.
- **The venue's credit mirror**, for what is lent now and what has ended. Its
  `period_days` is the venue's own; whether a long offer taken by a shorter bid is
  recorded at the offer's tenor is exactly what the experiment's first step finds out,
  so nothing that must hold before then depends on it.

A submission counts as a filled offer unless the venue's terminal row for it says it
ended with nothing taken. Everything uncertain counts: still resting, no terminal row
yet, an amount the venue did not report. The offer mirror's amounts are never used for
this: they freeze at the last live snapshot and miss a fill that lands after it.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OfferTerminal:
    """The venue's terminal report of one offer (a row of the observed offer history)."""

    terminal_kind: str  # "executed" | "canceled"
    status: str  # "active" | "partially_filled" (what it was when it ended)
    amount_original: Decimal | None
    amount_remaining: Decimal | None


@dataclass(frozen=True, slots=True)
class LongTenorSubmission:
    """One offer of ours that reached the venue."""

    period_days: int
    started_at_ms: int
    terminal: OfferTerminal | None  # None: resting, or ended with no terminal row observed yet
    has_trade: bool  # an observed funding trade names this offer


@dataclass(frozen=True, slots=True)
class LongTenorCredit:
    """One row of the venue credit mirror."""

    source_kind: str  # "credit" (in use) | "loan" (taken, unused)
    period_days: int | None
    amount: Decimal
    present: bool  # in the latest accepted snapshot: the money is out now
    mts_opening: int | None


@dataclass(frozen=True, slots=True)
class LongTenorExposure:
    offers_filled: int  # our offers of the tenor not known to have ended unfilled
    lent_amount: Decimal  # out now at the tenor, credits and loans
    ended_credits: int  # credit rows of the tenor no longer out

    @classmethod
    def none(cls) -> LongTenorExposure:
        return cls(offers_filled=0, lent_amount=Decimal(0), ended_credits=0)


def _ended_unfilled(submission: LongTenorSubmission) -> bool:
    terminal = submission.terminal
    return (
        terminal is not None
        and terminal.terminal_kind == "canceled"
        and terminal.status != "partially_filled"
        and terminal.amount_original is not None
        and terminal.amount_remaining is not None
        and terminal.amount_original == terminal.amount_remaining
        and not submission.has_trade
    )


def long_tenor_exposure(
    submissions: Iterable[LongTenorSubmission],
    credits: Iterable[LongTenorCredit],
    *,
    period_days: int,
    since_ms: int,
) -> LongTenorExposure:
    """Count the tenor's exposure from rows already read; nothing older than `since_ms`."""
    offers_filled = sum(
        1 for submission in submissions
        if submission.period_days == period_days
        and submission.started_at_ms >= since_ms
        and not _ended_unfilled(submission)
    )
    ours = [
        credit for credit in credits
        if credit.period_days == period_days
        and credit.mts_opening is not None
        and credit.mts_opening >= since_ms
    ]
    return LongTenorExposure(
        offers_filled=offers_filled,
        lent_amount=sum((credit.amount for credit in ours if credit.present), Decimal(0)),
        ended_credits=sum(
            1 for credit in ours if not credit.present and credit.source_kind == "credit"
        ),
    )
