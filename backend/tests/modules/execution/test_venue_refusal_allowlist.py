"""A 5xx the venue explained as a validation refusal settles as REJECTED.

ADR 2026-09-23 (venue refusal kept before classified) D2. Bitfinex answers a
funding-submit validation failure with HTTP 500 and ``["error", 10001,
MESSAGE]``; 10001 is its generic code, so the message is the only thing that
tells "the venue refused this offer" from "the venue fell over". Only a
(status, code, message) triple captured verbatim from the live venue settles;
anything else stays UNKNOWN, so the allowlist can widen only by observation.
"""
import pytest

from bfx_funding_bot.modules.execution.submit_outcomes import (
    VENUE_SUBMIT_REFUSALS_5XX,
    SubmitOutcomeKind,
    SubmitOutcomeUnknown,
    SubmitRejected,
    classify_submit_response,
)

# Recorded 2026-10-18 from the live venue: a fUST offer exceeding the
# available funding balance, answered HTTP 500.
_BALANCE = "Invalid offer: not enough UST balance available in deposit wallet"


def test_a_recorded_balance_refusal_settles_as_rejected() -> None:
    outcome = classify_submit_response(
        http_status=500, parsed_body=["error", 10001, _BALANCE], transport_started=True,
    )
    assert isinstance(outcome, SubmitRejected)
    assert outcome.reason == f"venue_rejected:10001:{_BALANCE}"
    assert outcome.raw_response is not None
    assert outcome.raw_response["status"] == "HTTP_500"
    assert len(outcome.raw_response["response_digest"]) == 64


def test_the_allowlist_holds_only_recorded_pairs() -> None:
    recorded = frozenset({(10001, _BALANCE)})
    assert recorded == VENUE_SUBMIT_REFUSALS_5XX


@pytest.mark.parametrize(("status", "body"), [
    # The generic code with a message nobody has recorded.
    (500, ["error", 10001, "simulated: venue error"]),
    # A recorded message must match whole, not as a prefix or substring.
    (500, ["error", 10001, _BALANCE[:-1]]),
    (500, ["error", 10001, _BALANCE + " (retry later)"]),
    # The recorded message under another code is not the recorded refusal.
    (500, ["error", 10020, _BALANCE]),
    # Only HTTP 500 has been observed carrying it; a gateway status may be a
    # proxy that never saw the venue's decision.
    (502, ["error", 10001, _BALANCE]),
    (503, ["error", 10001, _BALANCE]),
])
def test_everything_else_stays_unknown(status: int, body: list[object]) -> None:
    outcome = classify_submit_response(
        http_status=status, parsed_body=body, transport_started=True,
    )
    assert isinstance(outcome, SubmitOutcomeUnknown)
    assert outcome.outcome_kind is SubmitOutcomeKind.UNKNOWN
    assert outcome.reason == "http_5xx"
