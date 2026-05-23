"""Unit tests for BitfinexLiveExecutor.cancel — pure fns + I/O shell."""
from __future__ import annotations

import pytest

from bfx_funding_bot.external.bitfinex.live_executor import (
    classify_cancel_response,
)


def test_classify_success() -> None:
    raw = [
        1700000000000, "foc-req", None, None,
        ["123", "fUSD", "rate", "amount"],  # offer_array placeholder
        "0",
        "SUCCESS",
        None,
        "Submitting cancel request",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "success"
    assert text == "Submitting cancel request"


def test_classify_already_terminal_not_found() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", None,
        "Offer not found.",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "already_terminal"
    assert text == "Offer not found."


def test_classify_already_terminal_not_active() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", None,
        "Offer is not active.",
    ]
    status, _text = classify_cancel_response(raw)
    assert status == "already_terminal"


def test_classify_other_error() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", None,
        "Internal error.",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "other"
    assert text == "Internal error."


def test_classify_failure_status_treated_as_other() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "FAILURE", None,
        "Unknown failure",
    ]
    status, _text = classify_cancel_response(raw)
    assert status == "other"


def test_classify_malformed_response_raises() -> None:
    from bfx_funding_bot.modules.execution.errors import InvariantViolation
    with pytest.raises(InvariantViolation):
        classify_cancel_response({"not": "a list"})
    with pytest.raises(InvariantViolation):
        classify_cancel_response([1, 2])  # too short
