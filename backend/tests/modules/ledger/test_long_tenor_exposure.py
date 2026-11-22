"""Long-tenor exposure: what counts as a filled offer of ours, what is lent, what has ended."""
from __future__ import annotations

from decimal import Decimal

from bfx_funding_bot.modules.ledger.read.long_tenor import (
    LongTenorCredit,
    LongTenorSubmission,
    OfferTerminal,
    long_tenor_exposure,
)

SINCE = 1_800_000_000_000
D = Decimal


def _terminal(*, kind: str = "canceled", status: str = "active", original: str | None = "200",
              remaining: str | None = "200") -> OfferTerminal:
    return OfferTerminal(
        terminal_kind=kind, status=status,
        amount_original=D(original) if original is not None else None,
        amount_remaining=D(remaining) if remaining is not None else None,
    )


def _submission(*, period: int = 7, started: int = SINCE + 1, terminal: OfferTerminal | None = None,
                has_trade: bool = False) -> LongTenorSubmission:
    return LongTenorSubmission(period_days=period, started_at_ms=started, terminal=terminal,
                               has_trade=has_trade)


def _credit(*, kind: str = "credit", period: int | None = 7, amount: str = "200",
            present: bool = True, opening: int | None = SINCE + 1) -> LongTenorCredit:
    return LongTenorCredit(source_kind=kind, period_days=period, amount=D(amount),
                           present=present, mts_opening=opening)


def _filled(*submissions: LongTenorSubmission) -> int:
    return long_tenor_exposure(submissions, (), period_days=7, since_ms=SINCE).offers_filled


def test_an_offer_counts_until_the_venue_says_it_ended_unfilled() -> None:
    # Still resting, or gone with no history row yet: no evidence it did not fill.
    assert _filled(_submission(terminal=None)) == 1
    # The venue's terminal row says cancelled with nothing taken: the one case not counted.
    assert _filled(_submission(terminal=_terminal())) == 0


def test_an_offer_that_filled_before_any_snapshot_saw_it_counts() -> None:
    # Posted and fully taken inside one cycle: no mirror row ever existed. The count is
    # built from our own submissions, so it is there, and its terminal row says executed.
    assert _filled(_submission(terminal=_terminal(kind="executed", remaining="0"))) == 1
    # The venue reports the original amount as the amount when it has no separate value,
    # so an executed offer can show both equal: the terminal kind decides, not the amounts.
    assert _filled(_submission(terminal=_terminal(kind="executed"))) == 1


def test_any_sign_of_a_fill_keeps_a_cancelled_offer_in_the_count() -> None:
    assert _filled(_submission(terminal=_terminal(remaining="120"))) == 1  # part taken
    assert _filled(_submission(terminal=_terminal(status="partially_filled"))) == 1
    assert _filled(_submission(terminal=_terminal(), has_trade=True)) == 1
    assert _filled(_submission(terminal=_terminal(original=None))) == 1  # amount not reported
    assert _filled(_submission(terminal=_terminal(remaining=None))) == 1


def test_only_submissions_of_the_tenor_and_since_the_start_are_counted() -> None:
    assert _filled(_submission(period=2)) == 0
    assert _filled(_submission(started=SINCE - 1)) == 0
    assert _filled(_submission(started=SINCE)) == 1
    assert _filled(_submission(), _submission(), _submission(terminal=_terminal())) == 2


def test_lent_amount_is_what_is_out_now_at_the_tenor() -> None:
    exposure = long_tenor_exposure((), [
        _credit(amount="200"),
        _credit(kind="loan", amount="150"),          # taken but unused: still our money out
        _credit(amount="999", present=False),         # ended
        _credit(amount="999", period=2),              # another tenor
        _credit(amount="999", period=None),           # tenor not reported
        _credit(amount="999", opening=SINCE - 1),     # before the experiment
    ], period_days=7, since_ms=SINCE)
    assert exposure.lent_amount == D("350")


def test_a_credit_out_now_with_no_opening_time_counts_as_lent() -> None:
    # Money out at the tenor that cannot be dated is still money out: the amount limit
    # must see it. It is not a sample of the experiment, so it never counts as ended.
    exposure = long_tenor_exposure((), [
        _credit(amount="200", opening=None),
        _credit(amount="999", opening=None, present=False),
    ], period_days=7, since_ms=SINCE)
    assert exposure.lent_amount == D("200")
    assert exposure.ended_credits == 0


def test_ended_credits_count_credit_rows_only() -> None:
    exposure = long_tenor_exposure((), [
        _credit(present=False),
        _credit(present=False),
        _credit(kind="loan", present=False),          # the same lend listed as a loan first
        _credit(present=True),                        # still out
        _credit(present=False, period=2),
        _credit(present=False, opening=SINCE - 1),
    ], period_days=7, since_ms=SINCE)
    assert exposure.ended_credits == 2
