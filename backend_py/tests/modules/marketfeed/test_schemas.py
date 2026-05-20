from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Envelope,
    EventType,
    HealthCheckPayload,
    Phase,
    SignalPayload,
)


def _envelope_dict(**overrides):
    base = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "info",
        "phase": "paper",
        "strategy": "mean_reversion",
        "cell": "fUSD_a30",
        "event_type": "signal",
        "correlation_id": str(uuid4()),
        "payload": {
            "signal_score": 0.5,
            "signal_direction": "post",
            "strategy_attributes": {"rate": 0.0001, "mean": 0.00009, "sigma": 0.00002},
        },
    }
    base.update(overrides)
    return base


def test_envelope_accepts_valid_signal_event():
    env = Envelope.model_validate(_envelope_dict())
    assert env.event_type == EventType.SIGNAL
    assert env.phase == Phase.PAPER


def test_envelope_rejects_invalid_phase():
    with pytest.raises(ValidationError):
        Envelope.model_validate(_envelope_dict(phase="prod"))


def test_envelope_health_check_allows_nullable_strategy_cell():
    env = Envelope.model_validate(
        _envelope_dict(
            event_type="health_check",
            strategy=None,
            cell=None,
            payload={"check_target": "bitfinex_ws", "status": "healthy",
                     "last_msg_age_ms": 1000, "reconnect_count_last_hour": 0},
        )
    )
    assert env.strategy is None and env.cell is None


def test_envelope_signal_event_requires_strategy_cell():
    with pytest.raises(ValidationError):
        Envelope.model_validate(_envelope_dict(strategy=None))


def test_signal_payload_strategy_attributes_required():
    with pytest.raises(ValidationError):
        SignalPayload.model_validate({"signal_score": 0.5, "signal_direction": "post"})


def test_decision_payload_post_requires_offer_fields():
    with pytest.raises(ValidationError):
        DecisionPayload.model_validate({
            "decision_outcome": "post",
            "signal_correlation_id": str(uuid4()),
            # missing offer_rate / amount / duration
        })


def test_decision_payload_skip_requires_skip_reason():
    with pytest.raises(ValidationError):
        DecisionPayload.model_validate({
            "decision_outcome": "skip",
            "signal_correlation_id": str(uuid4()),
            # missing skip_reason
        })


def test_health_check_ws_requires_age_and_reconnect():
    with pytest.raises(ValidationError):
        HealthCheckPayload.model_validate({
            "check_target": "bitfinex_ws",
            "status": "healthy",
            # missing last_msg_age_ms / reconnect_count_last_hour
        })


def test_health_check_degraded_requires_error_message():
    with pytest.raises(ValidationError):
        HealthCheckPayload.model_validate({
            "check_target": "db",
            "status": "degraded",
            "latency_ms": 1000,
            # missing error_message
        })


def test_health_check_signal_pipeline_degraded_minimal_ok():
    """SIGNAL_PIPELINE target accepts degraded with only error_message —
    no last_msg_age_ms / latency_ms / reconnect_count_last_hour required.

    Bug A fix: candle_missing was previously emitted as BITFINEX_WS degraded,
    which forced bogus last_msg_age_ms=999_999. Now lives under its own
    target with a leaner contract.
    """
    payload = HealthCheckPayload.model_validate({
        "check_target": "signal_pipeline",
        "status": "degraded",
        "error_message": "candle_missing_at_scheduled_observe mts=1747584000000",
    })
    assert payload.check_target.value == "signal_pipeline"
    assert payload.status.value == "degraded"


def test_health_check_signal_pipeline_healthy_no_error_required():
    payload = HealthCheckPayload.model_validate({
        "check_target": "signal_pipeline",
        "status": "healthy",
    })
    assert payload.check_target.value == "signal_pipeline"


def test_health_check_signal_pipeline_degraded_without_error_rejected():
    """Generic degraded-requires-error_message rule still applies."""
    with pytest.raises(ValidationError):
        HealthCheckPayload.model_validate({
            "check_target": "signal_pipeline",
            "status": "degraded",
        })


def test_signal_payload_accepts_staleness_metadata() -> None:
    """SignalPayload accepts new optional fields: is_stale, stale_seconds, budget_seconds.

    Defaults: is_stale=False, stale_seconds=0.
    budget_seconds is required: every signal carries its cell's budget.
    Backward compat: existing payload with strategy_attributes still parses with budget_seconds.
    """
    from bfx_funding_bot.modules.marketfeed.schemas import SignalPayload

    # Backward compat: parse existing shape, only budget_seconds added as required
    payload_old = SignalPayload(
        signal_score=0.5,
        signal_direction="post",
        strategy_attributes={"rate": 0.0001, "mean": 0.00009, "sigma": 0.00002},
        budget_seconds=43200,  # 12h, required field
    )
    assert payload_old.is_stale is False
    assert payload_old.stale_seconds == 0

    # New shape: with stale metadata
    payload_new = SignalPayload(
        signal_score=0.5,
        signal_direction="post",
        strategy_attributes={"rate": 0.0001, "mean": 0.00009, "sigma": 0.00002},
        is_stale=True,
        stale_seconds=21600,  # 6h
        budget_seconds=43200,
    )
    assert payload_new.is_stale is True
    assert payload_new.stale_seconds == 21600
    assert payload_new.budget_seconds == 43200


def test_health_check_payload_accepts_reason_taxonomy() -> None:
    """HealthCheckPayload accepts reason field + reason-specific optional fields.

    Reasons in scope: stale_exceeded (SIGNAL_PIPELINE), connection_lost
    (BITFINEX_WS/REST), db_unavailable (DB), task_hung (any task).
    """
    from bfx_funding_bot.modules.marketfeed.schemas import (
        HealthCheckPayload,
        HealthStatus,
        HealthTarget,
    )

    # New shape: SIGNAL_PIPELINE stale_exceeded (degraded requires error_message)
    p1 = HealthCheckPayload(
        check_target=HealthTarget.SIGNAL_PIPELINE,
        status=HealthStatus.DEGRADED,
        error_message="stale_exceeded fUSD_p30_rate_percentile",
        reason="stale_exceeded",
        stale_seconds=46800,  # 13h
        budget_seconds=43200,  # 12h
    )
    assert p1.reason == "stale_exceeded"
    assert p1.stale_seconds == 46800

    # Backward compat: existing BITFINEX_WS healthy payload without reason still parses
    # BITFINEX_WS requires last_msg_age_ms + reconnect_count_last_hour
    p2 = HealthCheckPayload(
        check_target=HealthTarget.BITFINEX_WS,
        status=HealthStatus.HEALTHY,
        last_msg_age_ms=1000,
        reconnect_count_last_hour=0,
    )
    assert p2.reason is None
    assert p2.stale_seconds is None
