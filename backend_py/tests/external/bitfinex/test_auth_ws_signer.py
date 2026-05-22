from bfx_funding_bot.external.bitfinex.auth_ws import (
    build_auth_payload,
    sign_request,
)


def test_build_auth_payload_structure() -> None:
    payload = build_auth_payload(
        api_key="testkey", api_secret="testsecret", nonce_ms=1716383500000,
    )
    assert payload["event"] == "auth"
    assert payload["apiKey"] == "testkey"
    assert payload["authPayload"].startswith("AUTH")
    assert payload["authNonce"] == 1716383500000
    assert "authSig" in payload
    assert len(payload["authSig"]) == 96  # HMAC-SHA384 hex = 96 chars


def test_build_auth_payload_deterministic() -> None:
    """Same inputs → same signature (pure)."""
    p1 = build_auth_payload(api_key="k", api_secret="s", nonce_ms=1000)
    p2 = build_auth_payload(api_key="k", api_secret="s", nonce_ms=1000)
    assert p1 == p2


def test_build_auth_payload_signature_differs_on_nonce() -> None:
    p1 = build_auth_payload(api_key="k", api_secret="s", nonce_ms=1000)
    p2 = build_auth_payload(api_key="k", api_secret="s", nonce_ms=2000)
    assert p1["authSig"] != p2["authSig"]


def test_sign_request_returns_headers_dict() -> None:
    headers = sign_request(
        body=b'{"key":"value"}', nonce=1716383500000,
        api_secret="testsecret", path="v2/auth/w/funding/offer/submit",
    )
    assert "bfx-nonce" in headers
    assert "bfx-signature" in headers
    assert headers["bfx-nonce"] == "1716383500000"
    assert len(headers["bfx-signature"]) == 96


def test_sign_request_pure_deterministic() -> None:
    h1 = sign_request(body=b'{"x":1}', nonce=1000, api_secret="s", path="p")
    h2 = sign_request(body=b'{"x":1}', nonce=1000, api_secret="s", path="p")
    assert h1 == h2
