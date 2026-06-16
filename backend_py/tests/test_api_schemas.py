from bfx_funding_bot.modules.api.schemas import (
    ApiKeyResponse,
    CreateApiKeyRequest,
    VerifyResultResponse,
)


def test_create_request_accepts_camelcase():
    req = CreateApiKeyRequest.model_validate(
        {"label": "main", "apiKey": "PUB", "apiSecret": "SEC"}
    )
    assert req.api_key == "PUB"
    assert req.api_secret == "SEC"


def test_response_serializes_camelcase_and_masks_secret():
    resp = ApiKeyResponse(
        id="abc", label="main", api_key="PUB",
        exchange_status="verified", created_at="2026-06-07T00:00:00Z",
        verified_at="2026-06-07T00:00:01Z",
    )
    dumped = resp.model_dump(by_alias=True)
    assert dumped["apiKey"] == "PUB"
    assert dumped["apiSecret"] == "****"
    assert dumped["exchangeStatus"] == "verified"
    assert dumped["createdAt"] == "2026-06-07T00:00:00Z"
    assert dumped["verifiedAt"] == "2026-06-07T00:00:01Z"
    # #5: failure reason exposed under the FE contract key.
    assert dumped["lastVerifyError"] is None


def test_response_serializes_last_verify_error():
    # #5: the verify-failure reason uses the camelCase contract key.
    resp = ApiKeyResponse(
        id="abc", label="main", api_key="PUB",
        exchange_status="failed", created_at="2026-06-07T00:00:00Z",
        last_verify_error="withdraw_must_be_disabled",
    )
    dumped = resp.model_dump(by_alias=True)
    assert dumped["lastVerifyError"] == "withdraw_must_be_disabled"
    assert "last_verify_error" not in dumped


def test_create_request_accepts_snake_case():
    req = CreateApiKeyRequest.model_validate(
        {"label": "main", "api_key": "PUB", "api_secret": "SEC"}
    )
    assert req.api_key == "PUB"
    assert req.api_secret == "SEC"


def test_verify_result():
    assert VerifyResultResponse(status="failed", error="withdraw_must_be_disabled").model_dump() == {
        "status": "failed", "error": "withdraw_must_be_disabled",
    }
