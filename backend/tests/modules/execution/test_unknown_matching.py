"""Fail-closed contracts for persisted UNKNOWN-attempt matching."""

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    FundingOfferHistoryCoverage,
)
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.unknown_matching import (
    UnknownSubmitAttempt,
    amount_seen_since_start,
    match_attempt_to_snapshot,
    match_unknown_attempt,
)


def _attempt() -> UnknownSubmitAttempt:
    signal_id = UUID("11111111-1111-1111-1111-111111111111")
    return UnknownSubmitAttempt(
        attempt_id=UUID("22222222-2222-2222-2222-222222222222"),
        execution_decision_id="decision-1",
        account_id="550e8400-e29b-41d4-a716-446655440000",
        symbol="fUST",
        cid=7,
        amount=Decimal("100"),
        rate=Decimal("0.001"),
        period_days=2,
        offer_type="LIMIT",
        flags=0,
        started_at_ms=1_000,
        signal_correlation_id=signal_id,
        reservation_ref=ReservationRef(
            execution_decision_id="decision-1",
            cid=7,
            signal_correlation_id=signal_id,
        ),
    )


def _coverage() -> FundingOfferHistoryCoverage:
    return FundingOfferHistoryCoverage(
        requested_start_ms=900,
        requested_end_ms=1_990,
        oldest_mts_created=None,
        newest_mts_created=None,
        pages=1,
        complete=True,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"pages": 0},
        {"pages": -1},
        {"pages": True},
        {"complete": 1},
        {"requested_start_ms": True},
        {"requested_end_ms": 1_990.5},
        {"requested_start_ms": 2_000, "requested_end_ms": 1_990},
        {"oldest_mts_created": 1_500},
        {"newest_mts_created": 1_500},
        {"oldest_mts_created": 1_600, "newest_mts_created": 1_500},
        {"oldest_mts_created": True, "newest_mts_created": 1_500},
    ],
    ids=[
        "complete-zero-pages",
        "negative-pages",
        "bool-pages",
        "non-bool-complete",
        "bool-start",
        "non-int-end",
        "inverted-query-fence",
        "missing-newest",
        "missing-oldest",
        "inverted-observed-range",
        "bool-oldest",
    ],
)
def test_match_unknown_attempt_rejects_malformed_runtime_coverage(
    changes: dict[str, Any],
) -> None:
    """The core matcher must validate coverage even without the raw adapter."""
    coverage = replace(_coverage(), **changes)

    result = match_unknown_attempt(_attempt(), (), (), coverage)

    assert result.kind == "incomplete"


def test_match_unknown_attempt_preserves_valid_empty_history_zero_match() -> None:
    result = match_unknown_attempt(_attempt(), (), (), _coverage())

    assert result.kind == "zero_match"


def test_complete_history_with_zero_pages_is_incomplete() -> None:
    """Malformed raw coverage must never prove that no offer was accepted."""
    result = match_attempt_to_snapshot(
        _attempt(),
        {
            "query_started_at_ms": 1_990,
            "query_finished_at_ms": 2_000,
            "offers": [],
            "offer_history": [],
            "coverage": {
                "offer_history_complete": True,
                "offer_history_pages": 0,
                "offer_history_start_ms": 900,
                "offer_history_end_ms": 1_990,
                "offer_history_oldest_mts": None,
                "offer_history_newest_mts": None,
            },
        },
    )

    assert result.kind == "incomplete"


def _venue_offer(mts_created: int) -> ActiveFundingOffer:
    return ActiveFundingOffer(
        venue_offer_id="venue-1", symbol="fUST", amount=Decimal("100"),
        amount_original=Decimal("100"), rate=0.001, rate_decimal=Decimal("0.001"),
        period_days=2, mts_created=mts_created, mts_updated=mts_created,
        status="ACTIVE", offer_type="LIMIT", flags=0,
    )


# 2026-09-29: the venue stamps offers to the whole second on its own clock, so
# an offer accepted for an attempt started at 1_000 can read mts_created 0.
def test_offer_stamped_before_the_attempt_by_venue_granularity_still_matches() -> None:
    offer = _venue_offer(mts_created=0)
    coverage = replace(_coverage(), requested_start_ms=0,
                       oldest_mts_created=0, newest_mts_created=0)

    result = match_unknown_attempt(_attempt(), (offer,), (), coverage)

    assert result.kind == "exact_match"
    assert result.offer == offer


def test_offer_stamped_before_the_attempt_blocks_an_automatic_not_sent() -> None:
    # Were the amount not seen, a zero_match would resolve the attempt as never
    # sent while its offer is live at the venue.
    payload = {"offers": [{"symbol": "fUST", "amount_original": "100", "mts_created": 0}]}

    assert amount_seen_since_start(_attempt(), payload)


def test_offer_well_before_the_attempt_is_not_its_evidence() -> None:
    offer = _venue_offer(mts_created=-60_000)
    coverage = replace(_coverage(), requested_start_ms=-60_000,
                       oldest_mts_created=-60_000, newest_mts_created=-60_000)

    assert match_unknown_attempt(_attempt(), (offer,), (), coverage).kind == "zero_match"
