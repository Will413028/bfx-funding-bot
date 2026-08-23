import inspect
from typing import get_type_hints

import pytest

from bfx_funding_bot.external.bitfinex.live_executor import (
    BitfinexLiveExecutor,
    build_offer_payload,
    format_venue_decimal,
    parse_offer_response,
)
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit
from bfx_funding_bot.modules.execution.errors import InvariantViolation
from bfx_funding_bot.modules.execution.protocols import SubmittedOrder


def test_live_executor_submit_accepts_only_ready_to_submit() -> None:
    assert get_type_hints(BitfinexLiveExecutor.submit)["ready"] is ReadyToSubmit
    assert list(inspect.signature(BitfinexLiveExecutor.submit).parameters)[1] == "ready"


def test_format_venue_decimal_no_scientific_notation() -> None:
    # Bitfinex funding API rejects scientific-notation rate strings. str(5.531e-05)
    # would give "5.531e-05"; the helper must emit fixed-point.
    assert format_venue_decimal(5.531e-05) == "0.00005531"
    assert format_venue_decimal(1.2e-06) == "0.0000012"
    assert "e" not in format_venue_decimal(5.531e-05).lower()


def test_format_venue_decimal_preserves_normal_values() -> None:
    assert format_venue_decimal(0.0005) == "0.0005"
    assert format_venue_decimal(100.0) == "100.0"
    assert format_venue_decimal(150.0) == "150.0"


def test_build_offer_payload_small_rate_is_fixed_point() -> None:
    # Regression: fUST mean-reversion rate (~5.5e-05) must not serialize as
    # scientific notation (caused live submit 500s, 2026-05-26).
    payload = build_offer_payload(
        symbol="fUST", amount_usdt=150.0, rate=5.531e-05, period_days=2,
    )
    assert payload["rate"] == "0.00005531"
    assert "e" not in payload["rate"].lower()
    assert payload["symbol"] == "fUST"


def test_build_offer_payload_structure() -> None:
    payload = build_offer_payload(
        symbol="fUSD", amount_usdt=100.0, rate=0.0005, period_days=2,
    )
    assert payload["type"] == "LIMIT"
    assert payload["symbol"] == "fUSD"
    assert payload["amount"] == "100.0"
    assert payload["rate"] == "0.0005"
    assert payload["period"] == 2
    assert payload["flags"] == 0


def test_build_offer_payload_has_no_cid_field() -> None:
    # Bitfinex funding offers have no cid field — the internal cid must never
    # leak into the venue payload (would imply a venue dedup that doesn't exist).
    payload = build_offer_payload(symbol="fUSD", amount_usdt=100.0, rate=0.0005, period_days=2)
    assert "cid" not in payload


def test_parse_offer_response_submitted() -> None:
    """Real Bitfinex /v2/auth/w/funding/offer/submit response shape."""
    raw = [
        1716383500000, "fon-req", None, None,
        [
            42, "fUSD", 1716383500000, 1716383500000,
            100.0, 0, "REQ", None, None, 0, "ACTIVE", None, None, None,
            0.0005, 2, 0, 0, None, 0, None, None, None, 12345,
        ],
        None, "SUCCESS", None, "Submitting offer #42",
    ]
    result = parse_offer_response(raw)
    assert isinstance(result, SubmittedOrder)
    assert result.status == "submitted"
    assert result.venue_offer_id == "42"


def test_parse_offer_response_failed() -> None:
    raw = [
        1716383500000, "fon-req", None, None,
        None, None, "ERROR", None, "Funds insufficient",
    ]
    result = parse_offer_response(raw)
    assert result.status == "failed"
    assert result.venue_offer_id is None


def test_parse_offer_response_malformed_raises() -> None:
    """Per spec §8.2 — malformed = InvariantViolation."""
    with pytest.raises(InvariantViolation):
        parse_offer_response({"not": "expected"})  # type: ignore[arg-type]
