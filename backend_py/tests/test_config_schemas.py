import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.api.schemas import (
    StrategyConfigBody,
    UserConfigResponse,
)

_VALID = {
    "currency": "USD",
    "amount": {"min": 50, "max": 10000},
    "rate": {"min": 0.0001, "max": 0.001},
    "period": {"min": 2, "max": 30},
    "autoRenew": True,
}


def test_valid_body_parses_and_dumps_camelcase():
    body = StrategyConfigBody.model_validate(_VALID)
    dumped = body.model_dump(by_alias=True)
    assert dumped["currency"] == "USD"
    assert dumped["amount"] == {"min": 50.0, "max": 10000.0}
    assert dumped["autoRenew"] is True


def test_negative_amount_rejected():
    bad = {**_VALID, "amount": {"min": -1, "max": 10}}
    with pytest.raises(ValidationError):
        StrategyConfigBody.model_validate(bad)


def test_min_greater_than_max_rejected():
    bad = {**_VALID, "rate": {"min": 0.002, "max": 0.001}}
    with pytest.raises(ValidationError):
        StrategyConfigBody.model_validate(bad)


def test_empty_currency_rejected():
    bad = {**_VALID, "currency": ""}
    with pytest.raises(ValidationError):
        StrategyConfigBody.model_validate(bad)


def test_min_equals_max_allowed():
    # min == max is valid (mirrors the FE rangeSchema `min <= max`); locks the
    # `>` boundary in Range._min_le_max against a future `>=` regression.
    ok = {**_VALID, "amount": {"min": 100, "max": 100}}
    body = StrategyConfigBody.model_validate(ok)
    assert body.amount.min == body.amount.max == 100.0


def test_response_serializes_camelcase():
    resp = UserConfigResponse(
        id="11111111-1111-1111-1111-111111111111",
        user_id="user_abc",
        config=_VALID,
        created_at="2026-06-17T00:00:00+00:00",
        updated_at="2026-06-17T00:00:00+00:00",
    ).model_dump(by_alias=True)
    assert resp["userId"] == "user_abc"
    assert resp["createdAt"] == "2026-06-17T00:00:00+00:00"
    assert resp["config"]["autoRenew"] is True
