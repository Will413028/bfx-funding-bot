"""Fail-closed contracts for persisted UNKNOWN-attempt matching."""

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import FundingOfferHistoryCoverage
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.unknown_matching import (
    UnknownSubmitAttempt,
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
