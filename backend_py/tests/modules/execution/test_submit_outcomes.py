"""Deterministic contracts for ambiguous venue submit outcomes."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.modules.execution.protocols import SubmittedOrder
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmissionAttemptPayload,
    SubmitAcknowledged,
    SubmitNotSent,
    SubmitOutcomeKind,
    SubmitOutcomeUnknown,
    SubmitRejected,
    classify_submit_response,
    fingerprint_submit_payload,
    normalize_submit_payload,
    response_digest,
)


def _success_body(venue_offer_id: int = 42) -> list[object]:
    return [
        1716383500000,
        "fon-req",
        None,
        None,
        [venue_offer_id, "fUST", 0, 0, 100.0, 0, "REQ"],
        None,
        "SUCCESS",
        None,
        "Submitting",
    ]


def _error_body(text: str = "Funds insufficient") -> list[object]:
    return [1716383500000, "fon-req", None, None, None, None, "ERROR", None, text]


def test_success_with_venue_id_is_acknowledged() -> None:
    outcome = classify_submit_response(200, _success_body(), True, None)

    assert isinstance(outcome, SubmitAcknowledged)
    assert outcome.kind is SubmitOutcomeKind.ACKNOWLEDGED
    assert outcome.venue_offer_id == "42"
    assert outcome.raw_response is not None
    assert outcome.raw_response["response_digest"] == response_digest(_success_body())
    assert "body" not in outcome.raw_response


def test_structured_2xx_error_is_rejected() -> None:
    outcome = classify_submit_response(200, _error_body(), True, None)

    assert isinstance(outcome, SubmitRejected)
    assert outcome.kind is SubmitOutcomeKind.REJECTED
    assert "Funds insufficient" in outcome.reason
    assert outcome.raw_response is not None
    assert len(str(outcome.raw_response)) < 1_000
    assert "Funds insufficient" in str(outcome.raw_response)
    assert _error_body() != outcome.raw_response


def test_allowlisted_structured_4xx_is_rejected() -> None:
    outcome = classify_submit_response(422, _error_body("invalid period"), True, None)

    assert isinstance(outcome, SubmitRejected)
    assert outcome.kind is SubmitOutcomeKind.REJECTED


def test_unstructured_4xx_is_unknown() -> None:
    outcome = classify_submit_response(422, "request failed", True, None)

    assert isinstance(outcome, SubmitOutcomeUnknown)
    assert outcome.kind is SubmitOutcomeKind.UNKNOWN
    assert outcome.transport_started is True


def test_local_validation_before_transport_is_not_sent() -> None:
    outcome = classify_submit_response(
        None,
        None,
        False,
        ValueError("symbol is not configured"),
    )

    assert isinstance(outcome, SubmitNotSent)
    assert outcome.kind is SubmitOutcomeKind.NOT_SENT


def test_timeout_connection_reset_5xx_and_cancel_after_start_are_unknown() -> None:
    timeout = classify_submit_response(
        None, None, True, httpx.ReadTimeout("timed out")
    )
    reset = classify_submit_response(
        None, None, True, httpx.ConnectError("connection reset")
    )
    server_error = classify_submit_response(503, _error_body(), True, None)
    cancelled = classify_submit_response(
        None, None, True, asyncio.CancelledError()
    )

    assert all(
        isinstance(item, SubmitOutcomeUnknown)
        for item in (timeout, reset, server_error, cancelled)
    )
    assert timeout.reason == "timeout"
    assert reset.reason == "connection_error"
    assert server_error.reason == "http_5xx"
    assert cancelled.reason == "cancelled_after_start"


def test_malformed_or_unrecognized_response_defaults_to_unknown_with_digest() -> None:
    outcome = classify_submit_response(200, {"unexpected": object()}, True, None)

    assert isinstance(outcome, SubmitOutcomeUnknown)
    assert outcome.reason == "unrecognized_response"
    assert outcome.raw_response_digest is not None
    assert len(outcome.raw_response_digest) == 64


def test_payload_normalization_is_fixed_point_and_order_independent() -> None:
    left = normalize_submit_payload(
        {
            "rate": 5.531e-05,
            "amount": Decimal("150.00"),
            "flags": {"raw": 0},
            "nested": [Decimal("2.50"), 1.0],
        }
    )
    right = normalize_submit_payload(
        {
            "nested": [Decimal("2.50"), 1.0],
            "flags": {"raw": 0},
            "amount": "150.00",
            "rate": "0.00005531",
        }
    )

    assert left == right
    assert left["rate"] == "0.00005531"
    assert left["amount"] == "150.00"
    assert fingerprint_submit_payload(left) == fingerprint_submit_payload(right)


def test_payload_fingerprint_changes_when_economic_field_changes() -> None:
    base = {"symbol": "fUST", "amount": "150.0", "rate": "0.0001", "period": 2}

    assert fingerprint_submit_payload(base) != fingerprint_submit_payload({**base, "period": 3})


def test_submitted_order_derives_compatibility_status_from_typed_outcome() -> None:
    acknowledged = SubmittedOrder(
        cid=1,
        venue_offer_id="42",
        outcome=SubmitAcknowledged("42"),
        raw_response=None,
    )
    unknown = SubmittedOrder(
        cid=2,
        venue_offer_id=None,
        outcome=SubmitOutcomeUnknown("timeout", True),
        raw_response=None,
    )

    assert acknowledged.outcome_kind is SubmitOutcomeKind.ACKNOWLEDGED
    assert acknowledged.status == "submitted"
    assert unknown.outcome_kind is SubmitOutcomeKind.UNKNOWN
    assert unknown.status == "unknown"
    assert unknown.status != "failed"


def test_typed_outcome_rejects_contradictory_legacy_status() -> None:
    with pytest.raises(ValueError, match="status"):
        SubmittedOrder(
            cid=2,
            venue_offer_id=None,
            status="filled",
            outcome=SubmitOutcomeUnknown("timeout", True),
        )
    with pytest.raises(ValueError, match="status"):
        SubmittedOrder(
            cid=3,
            venue_offer_id="42",
            status="failed",
            outcome=SubmitAcknowledged("42"),
        )


def test_legacy_filled_status_remains_a_compatibility_view() -> None:
    order = SubmittedOrder(
        cid=3,
        venue_offer_id="paper_3",
        status="filled",
        raw_response=None,
    )

    assert order.outcome_kind is SubmitOutcomeKind.ACKNOWLEDGED
    assert order.status == "filled"


def test_legacy_filled_without_venue_id_fails_closed_to_unknown() -> None:
    order = SubmittedOrder(
        cid=4,
        venue_offer_id=None,
        status="filled",
        raw_response=None,
    )

    assert order.outcome_kind is SubmitOutcomeKind.UNKNOWN
    assert order.status == "unknown"


def test_non_acknowledged_legacy_status_cannot_carry_venue_id() -> None:
    with pytest.raises(ValueError, match="venue_offer_id"):
        SubmittedOrder(
            cid=5,
            venue_offer_id="42",
            status="unknown",
            raw_response=None,
        )


def test_submission_attempt_payload_freezes_normalized_identity_and_digest() -> None:
    account_id = uuid4()
    payload = {"symbol": "fUST", "amount": Decimal("100.0"), "rate": 0.0001}
    normalized = normalize_submit_payload(payload)
    attempt = SubmissionAttemptPayload(
        execution_decision_id="decision-1",
        account_id=account_id,
        environment="ci",
        symbol="fUST",
        cid=123,
        normalized_payload=normalized,
        payload_sha256=fingerprint_submit_payload(normalized),
        started_at_ms=100,
    )

    assert attempt.account_id == account_id
    assert attempt.payload_sha256 == fingerprint_submit_payload(normalized)
    assert attempt.normalized_payload["amount"] == "100.0"
    try:
        attempt.normalized_payload["new"] = "value"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("normalized payload must be immutable")


def test_submission_attempt_payload_rejects_fingerprint_mismatch() -> None:
    try:
        SubmissionAttemptPayload(
            execution_decision_id="decision-1",
            account_id=uuid4(),
            environment="ci",
            symbol="fUST",
            cid=123,
            normalized_payload={"amount": "100.0"},
            payload_sha256="0" * 64,
            started_at_ms=100,
        )
    except ValueError as exc:
        assert "payload_sha256" in str(exc)
    else:
        raise AssertionError("mismatched payload fingerprint must fail closed")


def test_submission_attempt_payload_enforces_outcome_identity_invariants() -> None:
    common = {
        "execution_decision_id": "decision-1",
        "account_id": uuid4(),
        "environment": "ci",
        "symbol": "fUST",
        "cid": 123,
        "normalized_payload": {"amount": "100.0"},
        "started_at_ms": 100,
    }
    with pytest.raises(ValueError, match="venue_offer_id"):
        SubmissionAttemptPayload(
            **common,
            outcome_kind=SubmitOutcomeKind.ACKNOWLEDGED,
        )
    with pytest.raises(ValueError, match="must not carry"):
        SubmissionAttemptPayload(
            **common,
            outcome_kind=SubmitOutcomeKind.REJECTED,
            outcome_reason="bad request",
            venue_offer_id="42",
        )
    with pytest.raises(ValueError, match="transport_started"):
        SubmitOutcomeUnknown("ambiguous", False)


def test_submission_attempt_payload_exposes_json_safe_storage_shape() -> None:
    attempt = SubmissionAttemptPayload(
        execution_decision_id="decision-1",
        account_id=uuid4(),
        environment="ci",
        symbol="fUST",
        cid=123,
        normalized_payload={"amount": Decimal("100.0"), "levels": [Decimal("1.2")]},
        started_at_ms=100,
        outcome_kind=SubmitOutcomeKind.REJECTED,
        outcome_reason="bad request",
    )

    storage = attempt.as_storage_dict()
    assert storage["account_id"] == str(attempt.account_id)
    assert storage["normalized_payload"] == {
        "amount": "100.0", "levels": ["1.2"],
    }
