from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Envelope,
    EventType,
    HealthCheckPayload,
    Level,
    Phase,
    SignalPayload,
)


def _envelope_dict(**overrides):
    base = dict(
        timestamp=datetime.now(UTC).isoformat(),
        level="info",
        phase="paper",
        strategy="mean_reversion",
        cell="fUSD_a30",
        event_type="signal",
        correlation_id=str(uuid4()),
        payload={
            "signal_score": 0.5,
            "signal_direction": "post",
            "strategy_attributes": {"rate": 0.0001, "mean": 0.00009, "sigma": 0.00002},
        },
    )
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
