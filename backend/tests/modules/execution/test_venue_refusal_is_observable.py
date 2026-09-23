"""A venue that explains its refusal must not be recorded as an outage.

Bitfinex answers a business rejection with HTTP 5xx and states the reason in the
body. `classify_submit_response` reads only the status for 5xx, so on
2026-09-21 and again on 2026-09-22 a canary submit produced `http_5xx` with the
reason discarded -- twice, identically, with nothing left afterwards but a
digest that could not be reversed. Each occurrence halted fUST and cost an
operator adjudication to clear.

Classifying those 5xx bodies as durable rejections needs the venue's error
codes, which is a separate decision. What must be true first is that the codes
survive at all.
"""
from decimal import ROUND_CEILING, Decimal

import pytest

from bfx_funding_bot.external.bitfinex.funding_rules import (
    RULE,
    FundingAmountEvidence,
    minimum_amount,
    submit_amount,
    validate_amount,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitOutcomeKind,
    classify_submit_response,
    venue_error,
)


def test_the_venue_error_survives_a_5xx() -> None:
    outcome = classify_submit_response(
        http_status=500, parsed_body=["error", 10020, "amount: invalid"], transport_started=True,
    )
    assert outcome.outcome_kind is SubmitOutcomeKind.UNKNOWN
    assert outcome.reason == "http_5xx"
    assert outcome.venue_error_code == 10020
    assert outcome.venue_error_message == "amount: invalid"


def test_an_outage_carries_no_venue_error() -> None:
    """The distinction this exists to preserve: a 5xx with no reason in it."""
    outcome = classify_submit_response(
        http_status=502, parsed_body="<html>502 Bad Gateway</html>", transport_started=True,
    )
    assert outcome.outcome_kind is SubmitOutcomeKind.UNKNOWN
    assert outcome.venue_error_code is None and outcome.venue_error_message is None


def test_a_timeout_carries_no_venue_error() -> None:
    import httpx

    outcome = classify_submit_response(
        transport_started=True, exception=httpx.ReadTimeout("timed out"),
    )
    assert outcome.reason == "timeout"
    assert outcome.venue_error_code is None


@pytest.mark.parametrize("body", [
    None, [], ["error"], ["error", 10020], ["ok", 10020, "x"],
    ["error", "10020", "x"], ["error", True, "x"], ["error", 10020, 5],
    {"error": 10020}, "error", 10020,
])
def test_anything_not_the_documented_shape_is_not_a_venue_error(body: object) -> None:
    """Shape-checked before anything is kept -- an echoed credential is not an
    int in slot 1 and a string in slot 2, so it cannot arrive through here."""
    assert venue_error(body) is None


def test_the_message_is_bounded() -> None:
    code, message = venue_error(["error", 10001, "x" * 5000])  # type: ignore[misc]
    assert code == 10001 and len(message) == 200


def _evidence(usd_per_unit: str) -> FundingAmountEvidence:
    return FundingAmountEvidence(RULE.digest, "fUST", Decimal(usd_per_unit), 1000, 1000)


def test_submitted_amount_clears_the_floor_the_venue_reconverts() -> None:
    """Sized to our conversion alone, an offer lands on either side of theirs."""
    evidence = _evidence("0.9998")
    floor = minimum_amount(evidence, symbol="fUST", now_ms=1000)
    sent = submit_amount(evidence, symbol="fUST", now_ms=1000)
    assert sent > floor
    assert sent == (floor * (Decimal(1) + RULE.submit_margin)).quantize(
        RULE.amount_quantum, rounding=ROUND_CEILING
    )


def test_the_margin_survives_an_adverse_reconversion() -> None:
    """The venue valuing UST 0.4% lower must still see at least 150 USD."""
    evidence = _evidence("0.9998")
    sent = submit_amount(evidence, symbol="fUST", now_ms=1000)
    assert sent * Decimal("0.9958") >= RULE.minimum_usd


def test_the_rule_itself_is_not_relaxed() -> None:
    """validate_amount still measures against the exact floor, not the margin."""
    evidence = _evidence("0.9998")
    floor = minimum_amount(evidence, symbol="fUST", now_ms=1000)
    validate_amount(submit_amount(evidence, symbol="fUST", now_ms=1000),
                    evidence, symbol="fUST", now_ms=1000)
    with pytest.raises(ValueError):
        validate_amount(floor - RULE.amount_quantum, evidence, symbol="fUST", now_ms=1000)


def test_submitted_amount_stays_inside_the_consumption_band() -> None:
    """The reconciler may revise up to RELEASE_MINIMUM_TOLERANCE and consume()
    accepts exactly that band; the margin must not eat it."""
    from bfx_funding_bot.modules.execution.deployment.reconciler import (
        RELEASE_MINIMUM_TOLERANCE,
    )

    assert RULE.submit_margin < RELEASE_MINIMUM_TOLERANCE
