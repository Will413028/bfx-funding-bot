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
from decimal import Decimal

from bfx_funding_bot.modules.ledger import (
    LongTenorCredit,
    LongTenorExposure,
    LongTenorSubmission,
    OfferTerminal,
)


def terminal_shows_unfilled(terminal: OfferTerminal) -> bool:
    """The venue's terminal row says the offer was canceled with nothing taken."""
    return (
        terminal.terminal_kind == "canceled"
        and terminal.status != "partially_filled"
        and terminal.amount_original is not None
        and terminal.amount_remaining is not None
        and terminal.amount_original == terminal.amount_remaining
    )


def _ended_unfilled(submission: LongTenorSubmission) -> bool:
    terminal = submission.terminal
    return (
        terminal is not None
        and terminal_shows_unfilled(terminal)
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
    at_tenor = [credit for credit in credits if credit.period_days == period_days]
    return LongTenorExposure(
        offers_filled=offers_filled,
        # A credit the venue did not date cannot be shown to predate the start: it is
        # money out at the tenor, so the amount limit counts it.
        lent_amount=sum(
            (credit.amount for credit in at_tenor
             if credit.present
             and (credit.mts_opening is None or credit.mts_opening >= since_ms)),
            Decimal(0),
        ),
        # A sample of the experiment must be dated inside it.
        ended_credits=sum(
            1 for credit in at_tenor
            if not credit.present and credit.source_kind == "credit"
            and credit.mts_opening is not None and credit.mts_opening >= since_ms
        ),
    )


__all__ = [
    "LongTenorCredit",
    "LongTenorExposure",
    "LongTenorSubmission",
    "OfferTerminal",
    "long_tenor_exposure",
    "terminal_shows_unfilled",
]
